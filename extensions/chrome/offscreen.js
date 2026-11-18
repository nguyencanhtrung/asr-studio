import {
  appendCaptureChunk,
  getActiveCapture,
  getSession,
  patchSession
} from "./db.js";
import {
  CAPTURE_STATES,
  canMarkComplete,
  isUnexpectedTrackEnd
} from "./capture-core.js";
import { MIME_TYPE, makeError } from "./protocol.js";

export const MEDIA_RECORDER_TIMESLICE_MS = 5000;

const runtime = {
  sessionId: null,
  starting: false,
  generation: 0,
  recorder: null,
  audioContext: null,
  tabStream: null,
  micStream: null,
  mixedStream: null,
  tabGain: null,
  micGain: null,
  tabPlaybackGain: null,
  tabAnalyser: null,
  micAnalyser: null,
  intentionalStop: false,
  persistenceError: null,
  interrupted: false,
  writeChain: Promise.resolve(),
  queuedBytes: 0,
  latestSession: null,
  levels: { tab: 0, microphone: 0 },
  meterTimer: null,
  stopPromise: null
};

const extensionId = () => chrome.runtime.id;

function errorMessage(error) {
  return error?.message || String(error || "Unknown capture error.");
}

function responseError(error) {
  return { ok: false, error: { code: error?.code || "capture_error", message: errorMessage(error) } };
}

function owns(sessionId, generation) {
  return runtime.sessionId === sessionId && runtime.generation === generation;
}

function resetRuntime(generation) {
  if (generation !== undefined && runtime.generation !== generation) return;
  runtime.generation += 1;
  runtime.sessionId = null;
  runtime.starting = false;
  runtime.latestSession = null;
  runtime.stopPromise = null;
  runtime.writeChain = Promise.resolve();
  runtime.queuedBytes = 0;
  runtime.persistenceError = null;
  runtime.interrupted = false;
  runtime.intentionalStop = false;
  runtime.levels = { tab: 0, microphone: 0 };
}

function captureStatus(session) {
  if (!session) return null;
  const capture = session.capture || {};
  return {
    sessionId: session.clientRecordingId,
    revision: session.revision,
    status: capture.status,
    captureComplete: Boolean(capture.captureComplete),
    interrupted: Boolean(capture.interrupted),
    error: capture.persistenceError || "",
    chunkCount: capture.chunkCount || 0,
    totalBytes: capture.totalBytes || 0,
    gates: { ...(capture.gates || {}) },
    tabMonitor: capture.tabMonitor !== false,
    levels: { ...runtime.levels },
    mimeType: session.mimeType,
    prototype: false
  };
}

function ownerStatus() {
  if (runtime.latestSession) return captureStatus(runtime.latestSession);
  if (!runtime.starting || !runtime.sessionId) return null;
  // Ownership starts before asynchronous media setup, even without a recorder yet.
  return {
    sessionId: runtime.sessionId,
    status: CAPTURE_STATES.STARTING,
    captureComplete: false,
    interrupted: false
  };
}

async function sendStatus(session = runtime.latestSession) {
  if (!session) return;
  runtime.latestSession = session;
  const message = { target: "worker", type: "CAPTURE_STATUS", status: captureStatus(session) };
  try {
    await chrome.runtime.sendMessage(message);
  } catch {
    // The service worker or control page can be asleep. IndexedDB remains authoritative.
  }
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function persistBlob(blob, sessionId, generation) {
  if (!owns(sessionId, generation) || !blob || blob.size === 0) return;
  let session = await getSession(sessionId);
  if (!owns(sessionId, generation)) return;
  if (!session || session.capture.status === CAPTURE_STATES.INTERRUPTED) return;
  const maxChunkBytes = session.capture.maxChunkBytes;
  let cursor = 0;
  while (cursor < blob.size) {
    if (!owns(sessionId, generation)) return;
    const part = blob.slice(cursor, Math.min(cursor + maxChunkBytes, blob.size));
    const bytes = await part.arrayBuffer();
    if (!owns(sessionId, generation)) return;
    const sequence = session.capture.nextSequence;
    const offset = session.capture.totalBytes;
    session = await appendCaptureChunk(sessionId, {
      sequence,
      offset,
      length: part.size,
      sha256: await sha256Hex(bytes),
      blob: part
    });
    runtime.latestSession = session;
    cursor += part.size;
    if (owns(sessionId, generation)) await sendStatus(session);
  }
}

function enqueueBlob(blob, sessionId, generation) {
  if (!owns(sessionId, generation)) return Promise.resolve();
  const capture = runtime.latestSession?.capture;
  const queueCap = Math.min(capture?.maxLocalBytes || 0, 2 * (capture?.maxChunkBytes || 0));
  if (runtime.queuedBytes + blob.size > queueCap) {
    void markInterrupted(makeError("write_backlog", "Audio persistence could not keep up. Capture stopped; saved audio is retained."), sessionId, generation);
    return runtime.writeChain;
  }
  runtime.queuedBytes += blob.size;
  runtime.writeChain = runtime.writeChain
    .then(() => persistBlob(blob, sessionId, generation))
    .catch(async (error) => {
      if (owns(sessionId, generation)) {
        runtime.persistenceError = error;
        await markInterrupted(error, sessionId, generation);
      }
    })
    .finally(() => {
      if (owns(sessionId, generation)) runtime.queuedBytes -= blob.size;
    });
  return runtime.writeChain;
}

function readLevel(analyser) {
  if (!analyser) return 0;
  const values = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(values);
  let total = 0;
  for (const value of values) {
    const centered = (value - 128) / 128;
    total += centered * centered;
  }
  return Math.min(1, Math.sqrt(total / values.length) * 3);
}

async function refreshMeters() {
  if (!runtime.sessionId || !runtime.latestSession) return;
  runtime.levels = {
    tab: readLevel(runtime.tabAnalyser),
    microphone: readLevel(runtime.micAnalyser)
  };
  await sendStatus(runtime.latestSession);
}

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() || []) {
    try {
      track.stop();
    } catch {
      // The track may already be ended.
    }
  }
}

async function teardownGraph() {
  if (runtime.meterTimer) clearInterval(runtime.meterTimer);
  runtime.meterTimer = null;
  stopTracks(runtime.tabStream);
  stopTracks(runtime.micStream);
  stopTracks(runtime.mixedStream);
  try {
    await runtime.audioContext?.close?.();
  } catch {
    // The context may already be closed.
  }
  runtime.recorder = null;
  runtime.audioContext = null;
  runtime.tabStream = null;
  runtime.micStream = null;
  runtime.mixedStream = null;
  runtime.tabGain = null;
  runtime.micGain = null;
  runtime.tabPlaybackGain = null;
  runtime.tabAnalyser = null;
  runtime.micAnalyser = null;
}

async function markInterrupted(error, sessionId = runtime.sessionId, generation = runtime.generation) {
  if (!owns(sessionId, generation) || runtime.interrupted) return;
  runtime.interrupted = true;
  const message = errorMessage(error);
  try {
    const session = await patchSession(sessionId, "capture", {
      status: CAPTURE_STATES.INTERRUPTED,
      interrupted: true,
      captureComplete: false,
      persistenceError: message
    });
    runtime.latestSession = session;
    await sendStatus(session);
  } catch {
    // Keep the original failure. A later worker restart can inspect the last durable session state.
  }
  runtime.intentionalStop = true;
  try {
    if (runtime.recorder && runtime.recorder.state !== "inactive") runtime.recorder.stop();
  } catch {
    // The recorder can already be stopping.
  }
  await teardownGraph();
  resetRuntime(generation);
}

function attachUnexpectedEnd(stream, sessionId, generation) {
  for (const track of stream.getTracks()) {
    track.addEventListener("ended", () => {
      if (owns(sessionId, generation) && isUnexpectedTrackEnd(runtime.latestSession?.capture, runtime.intentionalStop)) {
        void markInterrupted(makeError("source_ended", "An audio source ended during capture."));
      }
    });
  }
  stream.addEventListener("inactive", () => {
    if (owns(sessionId, generation) && isUnexpectedTrackEnd(runtime.latestSession?.capture, runtime.intentionalStop)) {
      void markInterrupted(makeError("source_inactive", "An audio source became inactive during capture."));
    }
  });
}

async function availableLocalBytes() {
  try {
    const estimate = await navigator.storage?.estimate?.();
    if (!estimate || !Number.isFinite(estimate.quota) || !Number.isFinite(estimate.usage)) return null;
    return Math.max(0, Math.floor((estimate.quota - estimate.usage) * 0.8));
  } catch {
    return null;
  }
}

async function startCapture(message) {
  if (runtime.sessionId || runtime.starting) return responseError(makeError("capture_busy", "A capture is already owned by this offscreen document."));

  const sessionId = message.sessionId;
  const generation = runtime.generation + 1;
  runtime.generation = generation;
  runtime.sessionId = sessionId;
  runtime.starting = true;
  runtime.intentionalStop = false;
  runtime.persistenceError = null;
  runtime.interrupted = false;
  runtime.writeChain = Promise.resolve();
  runtime.queuedBytes = 0;

  // Consume the fresh tab stream ID before other asynchronous setup work.
  let tabStreamPromise;
  let tabStream;
  let micStream;
  try {
    tabStreamPromise = navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId } },
      video: false
    });
    const session = await getSession(sessionId);
    if (!owns(sessionId, generation)) throw makeError("stale_capture", "The capture request was replaced.");
    if (!session) throw makeError("capture_missing", "The local capture session was not found.");
    if (session.capture.status !== CAPTURE_STATES.STARTING) {
      throw makeError("duplicate_start", "The local capture session is already active or finished.");
    }
    tabStream = await tabStreamPromise;
    if (!owns(sessionId, generation)) throw makeError("stale_capture", "The capture request was replaced.");
    const audio = {
      channelCount: { ideal: 1 },
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true
    };
    if (message.selectedDeviceId) audio.deviceId = { exact: message.selectedDeviceId };
    micStream = await navigator.mediaDevices.getUserMedia({ audio, video: false });
    if (!owns(sessionId, generation)) throw makeError("stale_capture", "The capture request ended during microphone setup.");
    if (!MediaRecorder.isTypeSupported(MIME_TYPE)) throw makeError("unsupported_media", "This Chrome build cannot record WebM/Opus.");

    const audioContext = new AudioContext({ latencyHint: "interactive" });
    const tabSource = audioContext.createMediaStreamSource(tabStream);
    const micSource = audioContext.createMediaStreamSource(micStream);
    const tabAnalyser = audioContext.createAnalyser();
    const micAnalyser = audioContext.createAnalyser();
    const tabGain = audioContext.createGain();
    const micGain = audioContext.createGain();
    const tabPlaybackGain = audioContext.createGain();
    const mixBus = audioContext.createGain();
    const destination = audioContext.createMediaStreamDestination();

    for (const node of [tabAnalyser, micAnalyser]) node.fftSize = 256;
    mixBus.channelCount = 1;
    mixBus.channelCountMode = "explicit";
    destination.channelCount = 1;
    destination.channelCountMode = "explicit";
    tabPlaybackGain.gain.value = 1;
    tabSource.connect(tabAnalyser).connect(tabGain).connect(mixBus);
    micSource.connect(micAnalyser).connect(micGain).connect(mixBus);
    tabSource.connect(tabPlaybackGain).connect(audioContext.destination);
    mixBus.connect(destination);

    runtime.audioContext = audioContext;
    runtime.tabStream = tabStream;
    runtime.micStream = micStream;
    runtime.mixedStream = destination.stream;
    runtime.tabGain = tabGain;
    runtime.micGain = micGain;
    runtime.tabPlaybackGain = tabPlaybackGain;
    runtime.tabAnalyser = tabAnalyser;
    runtime.micAnalyser = micAnalyser;
    runtime.latestSession = session;

    const effectiveAvailable = await availableLocalBytes();
    if (!owns(sessionId, generation)) throw makeError("stale_capture", "The capture request ended during storage setup.");
    const effectiveLocalBytes = effectiveAvailable === null ? session.capture.maxLocalBytes : Math.min(session.capture.maxLocalBytes, effectiveAvailable);
    const micSettings = micStream.getAudioTracks()[0].getSettings();
    const metadata = {
      sources: ["tab", "microphone"],
      mix: "mono",
      channels: 1,
      sample_rate: audioContext.sampleRate,
      tab_monitor: true,
      microphone_monitor: false,
      mime_type: MIME_TYPE,
      local_byte_cap: effectiveLocalBytes
    };
    for (const [key, setting] of [["echo_cancellation", "echoCancellation"], ["noise_suppression", "noiseSuppression"], ["auto_gain_control", "autoGainControl"]]) {
      if (typeof micSettings[setting] === "boolean") metadata[key] = micSettings[setting];
    }
    const createPayload = {
      client_recording_id: session.clientRecordingId,
      capture_epoch: session.capture.captureEpoch,
      mime_type: MIME_TYPE,
      title: session.title,
      metadata
    };
    const initialized = await patchSession(sessionId, "capture", {
      status: CAPTURE_STATES.RECORDING,
      metadata,
      createPayload,
      maxLocalBytes: effectiveLocalBytes,
      gates: { tab: true, microphone: true },
      tabMonitor: true
    });
    if (!owns(sessionId, generation)) throw makeError("stale_capture", "The capture request was replaced.");
    runtime.latestSession = initialized;
    attachUnexpectedEnd(tabStream, sessionId, generation);
    attachUnexpectedEnd(micStream, sessionId, generation);

    const recorder = new MediaRecorder(destination.stream, { mimeType: MIME_TYPE, audioBitsPerSecond: 128000 });
    if (!owns(sessionId, generation) || runtime.latestSession.capture.status !== CAPTURE_STATES.RECORDING) {
      throw makeError("stale_capture", "The capture request was replaced before recording started.");
    }
    runtime.recorder = recorder;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data?.size) void enqueueBlob(event.data, sessionId, generation);
    });
    recorder.addEventListener("error", (event) => {
      void markInterrupted(event.error || makeError("recorder_error", "The media recorder reported an error."), sessionId, generation);
    });
    recorder.start(MEDIA_RECORDER_TIMESLICE_MS);
    runtime.starting = false;
    runtime.meterTimer = setInterval(() => void refreshMeters(), 250);
    await sendStatus(initialized);
    return { ok: true, status: captureStatus(initialized) };
  } catch (error) {
    stopTracks(tabStream);
    stopTracks(micStream);
    if (!tabStream && tabStreamPromise) void tabStreamPromise.then(stopTracks, () => undefined);
    if (owns(sessionId, generation)) await markInterrupted(error, sessionId, generation);
    return responseError(error);
  }
}

function waitForRecorderStop(recorder) {
  if (!recorder || recorder.state === "inactive") return Promise.resolve();
  return new Promise((resolve) => {
    recorder.addEventListener("stop", resolve, { once: true });
    try {
      recorder.stop();
    } catch {
      resolve();
    }
  });
}

async function stopCapture() {
  if (!runtime.sessionId) {
    const active = await getActiveCapture();
    return { ok: true, status: captureStatus(active) };
  }
  if (runtime.stopPromise) return runtime.stopPromise;
  const sessionId = runtime.sessionId;
  const generation = runtime.generation;
  runtime.stopPromise = (async () => {
    const session = await getSession(sessionId);
    if (!session) {
      if (owns(sessionId, generation)) {
        await teardownGraph();
        resetRuntime(generation);
      }
      return responseError(makeError("capture_missing", "The local capture session was not found."));
    }
    if (session.capture.status === CAPTURE_STATES.COMPLETE || session.capture.status === CAPTURE_STATES.INTERRUPTED) {
      await teardownGraph();
      resetRuntime(generation);
      return { ok: true, status: captureStatus(session) };
    }
    runtime.intentionalStop = true;
    runtime.latestSession = await patchSession(sessionId, "capture", { status: CAPTURE_STATES.STOPPING });
    await sendStatus(runtime.latestSession);
    await waitForRecorderStop(runtime.recorder);
    await runtime.writeChain;
    const afterDrain = await getSession(sessionId);
    if (runtime.persistenceError || runtime.interrupted || !canMarkComplete(afterDrain.capture)) {
      if (owns(sessionId, generation) && !runtime.interrupted) await markInterrupted(runtime.persistenceError || makeError("capture_incomplete", "The capture did not reach a clean persistence barrier."), sessionId, generation);
      const final = await getSession(sessionId);
      if (owns(sessionId, generation)) {
        await teardownGraph();
        resetRuntime(generation);
      }
      return { ok: false, status: captureStatus(final) };
    }
    const complete = await patchSession(sessionId, "capture", {
      status: CAPTURE_STATES.COMPLETE,
      captureComplete: true,
      interrupted: false,
      persistenceError: "",
      completedAt: Date.now()
    });
    await teardownGraph();
    await sendStatus(complete);
    resetRuntime(generation);
    return { ok: true, status: captureStatus(complete) };
  })();
  return runtime.stopPromise;
}

async function setGate(source, enabled) {
  if (!runtime.sessionId || !runtime.latestSession) return responseError(makeError("capture_idle", "No capture is active."));
  if (source !== "tab" && source !== "microphone") return responseError(makeError("invalid_source", "The source gate is invalid."));
  const gain = source === "tab" ? runtime.tabGain : runtime.micGain;
  if (!gain) return responseError(makeError("capture_idle", "The audio graph is not ready."));
  gain.gain.value = enabled ? 1 : 0;
  const gates = { ...(runtime.latestSession.capture.gates || {}), [source]: Boolean(enabled) };
  const session = await patchSession(runtime.sessionId, "capture", { gates });
  await sendStatus(session);
  return { ok: true, status: captureStatus(session) };
}

async function setTabMonitor(enabled) {
  if (!runtime.sessionId || !runtime.tabPlaybackGain) return responseError(makeError("capture_idle", "No capture is active."));
  runtime.tabPlaybackGain.gain.value = enabled ? 1 : 0;
  const session = await patchSession(runtime.sessionId, "capture", { tabMonitor: Boolean(enabled) });
  await sendStatus(session);
  return { ok: true, status: captureStatus(session) };
}

async function recoverOnLoad() {
  const active = await getActiveCapture();
  if (active && [CAPTURE_STATES.RECORDING, CAPTURE_STATES.STOPPING].includes(active.capture.status)) {
    await patchSession(active.clientRecordingId, "capture", {
      status: CAPTURE_STATES.INTERRUPTED,
      interrupted: true,
      captureComplete: false,
      persistenceError: "The offscreen capture owner restarted before Stop completed."
    });
  }
}

function senderAllowed(sender) {
  if (!sender || sender.id !== extensionId()) return false;
  if (!sender.url) return false;
  const url = new URL(sender.url);
  return url.protocol === "chrome-extension:" && url.hostname === extensionId() && url.pathname === "/sw.js";
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== "offscreen") return false;
  if (!senderAllowed(sender)) {
    sendResponse(responseError(makeError("forbidden_sender", "Only the extension service worker can control capture.")));
    return false;
  }
  const operation = offscreenReady.then(() => {
    if (message.type === "START_CAPTURE") return startCapture(message);
    if (message.type === "STOP_CAPTURE") return stopCapture();
    if (message.type === "SET_GATE") return setGate(message.source, message.enabled);
    if (message.type === "SET_TAB_MONITOR") return setTabMonitor(message.enabled);
    if (message.type === "OFFSCREEN_STATUS") return { ok: true, status: ownerStatus() };
    throw makeError("unknown_message", "Unknown capture command.");
  });
  operation.then(sendResponse).catch((error) => sendResponse(responseError(error)));
  return true;
});

const offscreenReady = recoverOnLoad();
void offscreenReady.catch(() => {});
