import { latestBundle, RELEASE_ORIGIN, bundleFor } from "./release.js";
import {
  API_VERSION,
  canonicalManifest,
  bindingFromCapabilities,
  endpointUrl,
  EXTENSION_ID,
  isValidDeviceExpiresAt,
  makeError,
  MIME_TYPE,
  normalizeServerUrl,
  permissionPattern,
  redactedBinding,
  resolveMeetingUrl,
  sameBinding,
  unknownStagePolicy,
  validateBackendIdentity,
  validateChunkAck,
  validateReceipt,
  validateSessionResponse
} from "./protocol.js";
import { createPayloadForSession, validateCreateResponse } from "./intent-core.js";
import {
  cleanupReceipt,
  commitRecoveryChoice,
  createCaptureSession,
  discardLocalCapture,
  getChunk,
  getSession,
  getActiveCapture,
  hasPendingLocalAudio,
  isActiveCapture,
  listChunkMetadata,
  listSessions,
  patchSession,
  patchSessionWith,
  purgeDiscardedCaptures
} from "./db.js";
import { isCaptureOwnerLost, pairingGuard, uploadStateForError } from "./recovery-core.js";
import { interruptionCodeFor, ownerLostPatch } from "./capture-core.js";
import {
  awaitsRecoveryChoice,
  cancelTooLateOutcome,
  classifyUploadFailure,
  decideDiscard,
  decideRecoveryChoice,
  decideUpload,
  holdsRemoteSession,
  isCancelRetryableError,
  isCancelSettledError,
  isUploadActionable,
  UPLOAD_RETRY_BACKOFF_MS
} from "./upload-core.js";
import {
  runBoundedDiscardCancel,
  runCreateWithAttempt,
  runLockedUploadPass,
  runReconcileCreateThenCancel
} from "./upload-runner.js";

const STORAGE_NAMESPACE = "asr-extension-production-v1";
const TRUSTED_CONTEXTS = `${STORAGE_NAMESPACE}:trusted-contexts`;
const ACTIVE_CONTEXT = `${STORAGE_NAMESPACE}:active-context`;
const CONFIGURED_SERVER_URL = `${STORAGE_NAMESPACE}:configured-server-url`;
const MIC_SETUP = `${STORAGE_NAMESPACE}:mic-setup`;
const DEVICE_ID = `${STORAGE_NAMESPACE}:device-id`;
const UPLOAD_ALARM_PREFIX = `${STORAGE_NAMESPACE}:upload:`;
const POLL_ALARM_PREFIX = `${STORAGE_NAMESPACE}:receipt:`;
const DISCARD_CANCEL_TIMEOUT_MS = 5000;

const UPDATE_SETTING = `${STORAGE_NAMESPACE}:release-setting`;
const UPDATE_RESULT = `${STORAGE_NAMESPACE}:release-result`;
const UPDATE_ALARM = `${STORAGE_NAMESPACE}:release-check`;

const UPDATE_LAST_ATTEMPT = `${STORAGE_NAMESPACE}:release-last-attempt`;
const UPDATE_INTERVAL_MINUTES = 24 * 60;

async function checkForUpdate() {
  const token = (await storageGet(UPDATE_SETTING))[UPDATE_SETTING];
  if (!token || !await chrome.permissions.contains({ origins: [RELEASE_ORIGIN] })) return;
  try {
    await storageSet({ [UPDATE_LAST_ATTEMPT]: Date.now() });
    const installed = chrome.runtime.getManifest().version;
    const tag = await latestBundle(installed);
    await serializeUpdatePreference(async () => {
      if ((await storageGet(UPDATE_SETTING))[UPDATE_SETTING] === token) {
        await storageSet({ [UPDATE_RESULT]: { token, tag, installed } });
      }
    });
  } catch {
    // Keep the last complete result. Capture and upload have separate retry schedules.
    return;
  }
  await broadcastStatus();
}

async function reconcileUpdateAlarm() {
  const settings = await storageGet([UPDATE_SETTING, UPDATE_LAST_ATTEMPT]);
  const enabled = settings[UPDATE_SETTING];
  if (enabled && await chrome.permissions.contains({ origins: [RELEASE_ORIGIN] })) {
    if (!await chrome.alarms.get(UPDATE_ALARM)) {
      // Chrome can clear alarms on restart. Preserve the deadline across short sessions.
      const now = Date.now();
      const lastAttempt = Math.min(settings[UPDATE_LAST_ATTEMPT] || 0, now);
      const when = Math.max(now, lastAttempt + UPDATE_INTERVAL_MINUTES * 60_000);
      await chrome.alarms.create(UPDATE_ALARM, { when, periodInMinutes: UPDATE_INTERVAL_MINUTES });
    }
  } else {
    if (enabled) await storageSet({ [UPDATE_SETTING]: null });
    await chrome.alarms.clear(UPDATE_ALARM);
  }
}

let updatePreferenceTask = Promise.resolve();
function serializeUpdatePreference(operation) {
  const task = updatePreferenceTask.then(operation);
  updatePreferenceTask = task.catch(() => {});
  return task;
}

function setUpdateChecks(enabled) {
  return serializeUpdatePreference(async () => {
    if (enabled && !await chrome.permissions.contains({ origins: [RELEASE_ORIGIN] })) {
      throw makeError("update_permission", "GitHub permission is required.");
    }
    await storageSet({
      [UPDATE_SETTING]: enabled ? crypto.randomUUID() : null,
      [UPDATE_LAST_ATTEMPT]: enabled ? Date.now() : null
    });
    await reconcileUpdateAlarm();
    if (enabled) void checkForUpdate().catch(() => {});
    await broadcastStatus();
    return { ok: true };
  });
}

const sessionTokens = new Map();
const incompatibleBindings = new Map();
const uploadLocks = new Map();
let startLock = null;
let connectionBusy = false;
let statusSequence = 0;
let badgeSequence = 0;
const statusEpoch = crypto.randomUUID();
const trustedStorageReady = Promise.resolve()
  .then(() => chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }))
  .then(() => true, () => false);

async function requireTrustedStorage() {
  if (!await trustedStorageReady) throw makeError("storage_access", "Chrome could not restrict credential storage to trusted extension pages.");
}

async function connectionTransition(operation) {
  if (connectionBusy || startLock) throw makeError("connection_busy", "Wait for the current connection or capture setup to finish.");
  connectionBusy = true;
  try { return await operation(); }
  finally { connectionBusy = false; }
}

function extensionUrl(path) {
  return chrome.runtime.getURL(path);
}

function safeError(error, fallback = "The production client request failed.") {
  return {
    code: error?.code || "client_error",
    message: error?.publicMessage || error?.message || fallback
  };
}

function errorResponse(error) {
  return { ok: false, error: safeError(error) };
}

function senderIs(sender, path) {
  if (!sender || sender.id !== chrome.runtime.id || !sender.url) return false;
  try {
    const url = new URL(sender.url);
    return url.protocol === "chrome-extension:" && url.hostname === chrome.runtime.id && url.pathname === `/${path}`;
  } catch {
    return false;
  }
}

function senderIsControl(sender) {
  return senderIs(sender, "control.html");
}

function senderIsPopup(sender) {
  return senderIs(sender, "popup.html");
}

function senderIsOffscreen(sender) {
  return senderIs(sender, "offscreen.html");
}

async function storageGet(keys) {
  await requireTrustedStorage();
  return chrome.storage.local.get(keys);
}

function canonicalJson(value) {
  return JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
}

async function storageSet(values) {
  await requireTrustedStorage();
  await chrome.storage.local.set(values);
  const check = await chrome.storage.local.get(Object.keys(values));
  for (const [key, value] of Object.entries(values)) {
    if (canonicalJson(check[key]) !== canonicalJson(value)) throw makeError("storage_failed", `The client could not persist ${key}.`);
  }
}

async function getDeviceId() {
  const stored = await storageGet(DEVICE_ID);
  if (typeof stored[DEVICE_ID] === "string" && stored[DEVICE_ID]) return stored[DEVICE_ID];
  const deviceId = crypto.randomUUID();
  await storageSet({ [DEVICE_ID]: deviceId });
  return deviceId;
}

function contextKey(binding) {
  return `${binding.serverUrl}|${binding.webUrl}|${binding.backendId}|${binding.apiVersion}|${binding.protocol}`;
}

async function getCurrentContext() {
  const stored = await storageGet([TRUSTED_CONTEXTS, ACTIVE_CONTEXT]);
  const contexts = stored[TRUSTED_CONTEXTS] || {};
  const key = stored[ACTIVE_CONTEXT];
  const context = key ? contexts[key] : null;
  return context && context.binding ? context : null;
}

// Stage capability knowledge may change without changing endpoint identity. Keep
// it in the trusted context so the next capture records it, while old session rows
// retain their own endpoint and durable intent snapshots.
async function refreshStoredBinding(context, binding) {
  const key = contextKey(binding);
  const stored = await storageGet(TRUSTED_CONTEXTS);
  const contexts = { ...(stored[TRUSTED_CONTEXTS] || {}) };
  const current = contexts[key] || context;
  if (!current?.binding || !sameBinding(current.binding, binding)) return context;
  const next = { ...current, binding };
  contexts[key] = next;
  context.binding = binding;
  await storageSet({ [TRUSTED_CONTEXTS]: contexts });
  return next;
}

async function getConfiguredServerUrl() {
  const current = await getCurrentContext();
  if (current?.binding?.serverUrl) return current.binding.serverUrl;
  const stored = await storageGet(CONFIGURED_SERVER_URL);
  return typeof stored[CONFIGURED_SERVER_URL] === "string" ? stored[CONFIGURED_SERVER_URL] : "";
}

async function getMicSetup() {
  const stored = await storageGet(MIC_SETUP);
  const value = stored[MIC_SETUP] || {};
  return { ready: value.ready === true, selectedDeviceId: typeof value.selectedDeviceId === "string" ? value.selectedDeviceId : "" };
}

async function setMicSetup(selectedDeviceId) {
  const value = { ready: true, selectedDeviceId: typeof selectedDeviceId === "string" ? selectedDeviceId : "" };
  await storageSet({ [MIC_SETUP]: value });
  return { ready: true };
}

function publicErrorForRemote(status) {
  if (status === 401) return ["auth_expired", "The backend token expired or was revoked."];
  if (status === 403) return ["forbidden", "The backend rejected this extension origin."];
  if (status === 404) return ["not_found", "The backend session is not available to this device. Use Pair again for the existing device in Settings → Devices; retained audio stays local."];
  if (status === 408) return ["timeout", "The backend request timed out."];
  if (status === 409) return ["conflict", "The backend rejected a replay or state conflict."];
  if (status === 429) return ["backend_unavailable", "The ASR Studio backend is unavailable."];
  if (status === 413) return ["byte_limit", "The backend rejected the configured byte or count limit."];
  if (status === 422) return ["media_invalid", "The backend rejected the WebM/Opus media."];
  if (status === 507) return ["backend_storage", "The backend storage limit was reached."];
  if (status >= 500) return ["backend_unavailable", "The ASR Studio backend is unavailable."];
  return ["backend_error", "The ASR Studio backend rejected the request."];
}

async function fetchJson(serverUrl, relativePath, init = {}, timeoutMs = 15000) {
  // CORS may allow a fetch even after Chrome host access is revoked.
  // Check the user's current grant before every credential or audio request.
  if (!await chrome.permissions.contains({ origins: [permissionPattern(serverUrl)] })) {
    throw makeError("host_permission", "Chrome has not granted access to this server.");
  }
  const url = endpointUrl(serverUrl, relativePath);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      ...init,
      credentials: "omit",
      redirect: "error",
      signal: controller.signal
    });
  } catch (error) {
    clearTimeout(timer);
    const wrapped = makeError(error?.name === "AbortError" ? "timeout" : "network_error", error?.name === "AbortError" ? "The backend request timed out." : "The backend could not be reached.");
    throw wrapped;
  }

  try {
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = null;
      }
    }
    if (!response.ok) {
      const [code, message] = publicErrorForRemote(response.status);
      const error = makeError(code, message);
      error.status = response.status;
      error.remoteCode = body?.error?.code;
      throw error;
    }
    return { status: response.status, body };
  } catch (error) {
    if (error?.name === "AbortError") throw makeError("timeout", "The backend request timed out.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function requestJson(serverUrl, relativePath, method, payload, extraHeaders = {}, timeoutMs = 15000) {
  return fetchJson(serverUrl, relativePath, {
    method,
    headers: { "Content-Type": "application/json", ...extraHeaders },
    body: payload === undefined ? undefined : JSON.stringify(payload)
  }, timeoutMs);
}

function contextPublic(context) {
  if (!context?.binding) return null;
  return redactedBinding({ ...context.binding, paired: true, deviceExpiresAt: context.deviceExpiresAt });
}

async function pendingAudioSessions() {
  const sessions = await listSessions();
  return sessions.filter((session) => isActiveCapture(session) || hasPendingLocalAudio(session) || holdsRemoteSession(session));
}

async function pendingAudioExists() {
  return (await pendingAudioSessions()).length > 0;
}

async function pairWithServer(serverUrl, code) {
  const normalized = normalizeServerUrl(serverUrl);
  const pending = await pendingAudioSessions();
  const preliminaryGuard = pairingGuard(pending, normalized);
  if (preliminaryGuard) throw makeError(preliminaryGuard.code, preliminaryGuard.message);

  const capabilitiesResponse = await fetchJson(normalized, "api/extension/v1/capabilities");
  const binding = bindingFromCapabilities(normalized, capabilitiesResponse.body);
  const bindingGuard = pairingGuard(pending, normalized, binding);
  if (bindingGuard) throw makeError(bindingGuard.code, bindingGuard.message);
  const deviceId = await getDeviceId();
  const pairResponse = await requestJson(normalized, "api/extension/v1/pair", "POST", { code, device_id: deviceId });
  const payload = pairResponse.body;
  if (!payload || typeof payload.device_token !== "string" || !payload.device_token ||
      !Object.prototype.hasOwnProperty.call(payload, "device_expires_at") ||
      !isValidDeviceExpiresAt(payload.device_expires_at) || payload.backend_id !== binding.backendId) {
    throw makeError("protocol_mismatch", "The pairing response is invalid.");
  }

  const context = {
    binding,
    deviceToken: payload.device_token,
    deviceExpiresAt: payload.device_expires_at,
    pairedAt: new Date().toISOString()
  };
  const key = contextKey(binding);
  sessionTokens.delete(key);
  const previous = await storageGet([TRUSTED_CONTEXTS, ACTIVE_CONTEXT]);
  const previousContexts = { ...(previous[TRUSTED_CONTEXTS] || {}) };
  if (previousContexts[key]) {
    // Pairing rotated this device's bearer; its previous context is no longer valid.
    delete previousContexts[key];
    await storageSet({
      [TRUSTED_CONTEXTS]: previousContexts,
      [ACTIVE_CONTEXT]: previous[ACTIVE_CONTEXT] === key ? null : previous[ACTIVE_CONTEXT]
    });
    await broadcastStatus();
  }
  // The server activates a paired device only after its bearer proves usable.
  // Complete that exchange before persisting or announcing a connected context.
  await ensureSessionToken(context, { capabilitiesVerified: true });
  incompatibleBindings.delete(key);
  await setServerUrl(normalized);
  const stored = await storageGet(TRUSTED_CONTEXTS);
  const contexts = { ...(stored[TRUSTED_CONTEXTS] || {}), [key]: context };
  await storageSet({ [TRUSTED_CONTEXTS]: contexts, [ACTIVE_CONTEXT]: key });
  void resumeUploads().catch(() => {});
  return contextPublic(context);
}

async function setServerUrl(serverUrl) {
  const normalized = normalizeServerUrl(serverUrl);
  const current = await getCurrentContext();
  const pending = await pendingAudioSessions();
  if (pending.some((session) => session.endpoint.serverUrl !== normalized)) {
    throw makeError("url_locked", "The server URL cannot change while capture bytes are pending.");
  }
  if (current && current.binding.serverUrl !== normalized) {
    const stored = await storageGet(TRUSTED_CONTEXTS);
    const contexts = { ...(stored[TRUSTED_CONTEXTS] || {}) };
    delete contexts[contextKey(current.binding)];
    await storageSet({ [TRUSTED_CONTEXTS]: contexts, [ACTIVE_CONTEXT]: null, [CONFIGURED_SERVER_URL]: normalized });
    sessionTokens.clear();
  } else {
    await storageSet({ [CONFIGURED_SERVER_URL]: normalized });
  }
  return { serverUrl: normalized, paired: false };
}

async function clearPairing() {
  if (await pendingAudioExists()) throw makeError("url_locked", "Pairing cannot be cleared while capture bytes are pending.");
  const current = await getCurrentContext();
  const stored = await storageGet(TRUSTED_CONTEXTS);
  const contexts = { ...(stored[TRUSTED_CONTEXTS] || {}) };
  if (current) delete contexts[contextKey(current.binding)];
  await storageSet({ [TRUSTED_CONTEXTS]: contexts, [ACTIVE_CONTEXT]: null });
  sessionTokens.clear();
  return { paired: false };
}

function remainingTimeout(deadlineAt, fallbackMs) {
  if (deadlineAt === null) return fallbackMs;
  const remaining = deadlineAt - Date.now();
  if (remaining <= 0) throw makeError("timeout", "The backend request timed out.");
  return remaining;
}

function storedApiError(binding) {
  return binding.apiVersion === API_VERSION
    ? null
    : makeError("api_version_mismatch", "The extension and server need compatible API versions.");
}

async function checkPairedApi(context, timeoutMs) {
  const { binding } = context;
  const key = contextKey(binding);
  try {
    const mismatch = storedApiError(binding);
    if (mismatch) throw mismatch;
    const response = await fetchJson(binding.serverUrl, "api/extension/v1/capabilities", { cache: "no-store" }, timeoutMs);
    const refreshed = bindingFromCapabilities(binding.serverUrl, response.body);
    await refreshStoredBinding(context, refreshed);
    if (incompatibleBindings.delete(key)) void broadcastStatus().catch(() => {});
  } catch (error) {
    await refreshStoredBinding(context, Object.freeze({
      ...binding,
      stagePolicy: unknownStagePolicy(error?.code || "capability_unavailable")
    })).catch(() => {});
    if (error.code === "api_version_mismatch") {
      sessionTokens.delete(key);
      if (!incompatibleBindings.has(key)) {
        incompatibleBindings.set(key, safeError(error));
        void broadcastStatus().catch(() => {});
      }
    }
    throw error;
  }
}

async function checkApiAfterRejection(context, error, timeoutMs, deadlineAt = null) {
  if (error.code !== "protocol_mismatch" && !(error.status >= 400 && error.status < 500)) return;
  try {
    await checkPairedApi(context, remainingTimeout(deadlineAt, timeoutMs));
  } catch (checkError) {
    if (checkError.code === "api_version_mismatch") throw checkError;
  }
}

async function ensureSessionToken(context, { timeoutMs = 15000, deadlineAt = null, capabilitiesVerified = false } = {}) {
  const key = contextKey(context.binding);
  const cached = sessionTokens.get(key);
  if (cached && cached.expiresAt > Date.now() + 30000 && !safeApiError(context.binding)) return cached.token;
  if (!context.deviceToken) throw makeError("pair_required", "Pair the extension with ASR Studio first.");
  if (!capabilitiesVerified) await checkPairedApi(context, remainingTimeout(deadlineAt, timeoutMs));
  const response = await requestJson(
    context.binding.serverUrl,
    "api/extension/v1/session",
    "POST",
    {},
    { Authorization: `Bearer ${context.deviceToken}` },
    remainingTimeout(deadlineAt, timeoutMs)
  );
  const payload = response.body;
  if (!payload || typeof payload.session_token !== "string" || !payload.session_token || payload.backend_id !== context.binding.backendId) {
    throw makeError("protocol_mismatch", "The session response is invalid.");
  }
  const expiresAt = Date.parse(payload.expires_at);
  if (!Number.isFinite(expiresAt)) throw makeError("protocol_mismatch", "The session expiry is invalid.");
  sessionTokens.set(key, { token: payload.session_token, expiresAt });
  return payload.session_token;
}

async function authorizedRequest(context, relativePath, init = {}, allowRenew = true, timeoutMs = 15000, deadlineAt = null) {
  const token = await ensureSessionToken(context, { timeoutMs, deadlineAt });
  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  try {
    return await fetchJson(
      context.binding.serverUrl,
      relativePath,
      { ...init, headers },
      remainingTimeout(deadlineAt, timeoutMs)
    );
  } catch (error) {
    if (allowRenew && error.status === 401) {
      sessionTokens.delete(contextKey(context.binding));
      return authorizedRequest(context, relativePath, init, false, timeoutMs, deadlineAt);
    }
    await checkApiAfterRejection(context, error, timeoutMs, deadlineAt);
    throw error;
  }
}

async function ensureOffscreen() {
  try {
    if (await chrome.offscreen.hasDocument()) return;
  } catch {
    // Create below and let Chrome report a real failure if unsupported.
  }
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Keep the tab and microphone MediaRecorder alive while the service worker sleeps."
    });
  } catch (error) {
    try {
      if (await chrome.offscreen.hasDocument()) return;
    } catch {
      // Preserve the original error.
    }
    throw makeError("offscreen_unavailable", error?.message || "Chrome could not create the offscreen capture document.");
  }
}

async function sendOffscreen(message) {
  return chrome.runtime.sendMessage({ ...message, target: "offscreen" });
}



function publicSession(session) {
  let meetingUrl = null;
  if (session.upload?.meetingUrl) {
    try {
      meetingUrl = resolveMeetingUrl(session.endpoint, session.upload.meetingUrl);
    } catch {
      meetingUrl = null;
    }
  }
  return {
    id: session.clientRecordingId,
    revision: session.revision,
    endpoint: {
      serverUrl: session.endpoint.serverUrl,
      webUrl: session.endpoint.webUrl,
      backendId: session.endpoint.backendId,
      apiVersion: session.endpoint.apiVersion,
      protocol: session.endpoint.protocol,
      prototype: false
    },
    capture: {
      sessionId: session.clientRecordingId,
      startedAt: session.createdAt,
      status: session.capture.status,
      captureComplete: Boolean(session.capture.captureComplete),
      interrupted: Boolean(session.capture.interrupted),
      interruptionCode: session.capture.interrupted ? interruptionCodeFor({ code: session.capture.interruptionCode }) : null,
      chunkCount: session.capture.chunkCount,
      totalBytes: session.capture.totalBytes,
      error: session.capture.persistenceError || "",
      gates: { ...session.capture.gates },
      tabMonitor: session.capture.tabMonitor !== false
    },
    upload: {
      state: session.upload.state,
      nextSequence: session.upload.nextSequence,
      recoveryChoice: session.upload.recoveryChoice || null,
      awaitingChoice: awaitsRecoveryChoice(session),
      cancelSettled: Boolean(session.upload.cancelSettled),
      receipt: session.upload.receipt ? { ...session.upload.receipt } : null,
      meetingUrl,
      cleaned: Boolean(session.upload.cleaned),
      error: session.upload.error ? { ...session.upload.error } : null
    },
    summary: session.summary ? { ...session.summary } : null,
    prototype: false
  };
}

async function updateActionBadge(status) {
  // A slower status read must not replace the newer recording or upload badge.
  if (status.statusSequence <= badgeSequence) return;
  badgeSequence = status.statusSequence;
  try {
    const capture = status?.capture;
    const hasActiveCapture = Boolean(capture && ["starting", "recording", "stopping"].includes(capture.status));
    const hasPendingUpload = (status?.sessions || []).some((session) => {
      const uploadState = session.upload?.state;
      const sending = session.capture?.captureComplete || session.upload?.recoveryChoice === "save";
      return sending && !session.upload?.cleaned && uploadState !== "discarded";
    });
    const text = hasActiveCapture ? "REC" : hasPendingUpload ? "UP" : status?.availableUpdate ? "NEW" : "";
    await chrome.action.setBadgeText({ text });
    if (text && status.statusSequence === badgeSequence) {
      const color = { REC: "#a63838", UP: "#8e2433", NEW: "#485465" }[text];
      await chrome.action.setBadgeBackgroundColor({ color });
    }
  } catch {
    // Badge support is useful feedback, never a capture dependency.
  }
}

function safeApiError(binding) {
  const mismatch = storedApiError(binding);
  return mismatch ? safeError(mismatch) : incompatibleBindings.get(contextKey(binding)) || null;
}

async function publicStatus(overrideCapture = null, message = "") {
  const sequence = ++statusSequence;
  const context = await getCurrentContext();
  const sessions = await listSessions();
  const active = sessions.find(isActiveCapture);
  const latest = sessions.filter((session) => !session.upload.cleaned).sort((a, b) => b.updatedAt - a.updatedAt)[0];
  const mic = await getMicSetup();
  const overrideOwner = overrideCapture?.sessionId ? sessions.find((session) => session.clientRecordingId === overrideCapture.sessionId) : null;
  const owner = overrideOwner || active || latest;
  const capture = overrideCapture
    ? { ...overrideCapture, sessionId: overrideCapture.sessionId || owner?.clientRecordingId, startedAt: overrideCapture.startedAt || owner?.createdAt }
    : owner ? publicSession(owner).capture : null;
  const update = await storageGet([UPDATE_SETTING, UPDATE_RESULT]).catch(() => ({}));
  const result = update[UPDATE_RESULT];
  const updateSafe = !startLock && !isActiveCapture({ capture }) && !sessions.some((session) =>
    isActiveCapture(session) || hasPendingLocalAudio(session) || holdsRemoteSession(session));
  const tag = update[UPDATE_SETTING] && update[UPDATE_SETTING] === result?.token
    && result.installed === chrome.runtime.getManifest().version ? result.tag : null;
  const status = {
    updateSafe,
    checksEnabled: Boolean(update[UPDATE_SETTING]),
    availableUpdate: updateSafe && bundleFor(tag) ? tag : null,
    statusEpoch,
    statusSequence: sequence,
    compatibilityError: context
      ? safeApiError(context.binding)
      : null,
    connection: contextPublic(context) || (await getConfiguredServerUrl() ? { serverUrl: await getConfiguredServerUrl(), paired: false, prototype: false } : null),
    micReady: mic.ready,
    capture,
    sessions: sessions.map(publicSession),
    message,
    prototype: false
  };
  void updateActionBadge(status);
  return status;
}

async function sendToExtensionUIs(message) {
  try { await chrome.runtime.sendMessage({ ...message, target: "extension-ui" }); }
  catch { /* Extension pages may be closed. Durable state remains in IndexedDB. */ }
}

async function broadcastStatus(overrideCapture = null, message = "") {
  const status = await publicStatus(overrideCapture, message);
  await sendToExtensionUIs({ type: "STATUS_UPDATE", status });
  return status;
}

async function openControlPage(setup = false, message = "") {
  const url = extensionUrl(`control.html${setup ? "?setup=1" : ""}`);
  const tabs = await chrome.tabs.query({ url: `${extensionUrl("control.html")}*` });
  const existing = tabs.find((tab) => Number.isInteger(tab.id));
  if (existing?.id !== undefined) {
    await chrome.tabs.update(existing.id, { active: true, url });
    if (existing.windowId !== undefined) await chrome.windows.update(existing.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url, active: true });
  }
  if (message) await sendToExtensionUIs({ type: "NOTICE", message });
}

async function openWebRoot() {
  const context = await getCurrentContext();
  if (!context?.binding?.webUrl) throw makeError("setup_required", "Pair ASR Studio before opening Settings.");
  const webRoot = `${context.binding.webUrl}/`;
  await chrome.tabs.create({ url: webRoot, active: true });
  return { ok: true, url: webRoot };
}

async function validateSourceTab(tabId) {
  if (!Number.isInteger(tabId)) throw makeError("tab_missing", "Không tìm thấy tab đang hoạt động.");
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    throw makeError("tab_missing", "Không tìm thấy tab đang hoạt động.");
  }
  if (!tab || tab.id !== tabId || typeof tab.url !== "string") {
    throw makeError("invalid_source_tab", "Chỉ ghi được từ tab HTTP hoặc HTTPS đang hoạt động.");
  }
  let sourceUrl;
  try {
    sourceUrl = new URL(tab.url);
  } catch {
    throw makeError("invalid_source_tab", "Chỉ ghi được từ tab HTTP hoặc HTTPS đang hoạt động.");
  }
  // activeTab is intentionally the narrow permission used to inspect the invoked tab
  // and pass its id to tabCapture; the extension does not request the broad tabs permission.
  const ownOrigin = new URL(extensionUrl("")).origin;
  if (sourceUrl.origin === ownOrigin || !["http:", "https:"].includes(sourceUrl.protocol)) {
    throw makeError("invalid_source_tab", "Chỉ ghi được từ tab HTTP hoặc HTTPS đang hoạt động; trang Chrome và trang tiện ích bị từ chối.");
  }
  return tab;
}

async function activeSourceTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs.length !== 1 || !Number.isInteger(tabs[0]?.id)) throw makeError("tab_missing", "Không tìm thấy tab đang hoạt động.");
  return validateSourceTab(tabs[0].id);
}

async function markStartFailure(sessionId, error) {
  try {
    await patchSession(sessionId, "capture", {
      status: "interrupted",
      interrupted: true,
      captureComplete: false,
      persistenceError: error?.message || "Capture could not start."
    });
  } catch {
    // The original start failure is the useful report.
  }
}

async function startFromTab(tabId, source = "action") {
  await recoveryReady;
  if (connectionBusy) throw makeError("connection_busy", "Wait for pairing or the server change to finish.");
  if (startLock) return startLock;
  startLock = (async () => {
    const sourceTab = await validateSourceTab(tabId);
    const current = await getCurrentContext();
    const mic = await getMicSetup();
    if (!current || !mic.ready) {
      return {
        ok: false,
        setupRequired: true,
        error: { code: "setup_required", message: "Hoàn tất thiết lập microphone và server trước khi ghi." }
      };
    }
    // Refresh format knowledge before the capture's durable intent is written.
    // A failed or malformed capability read is retained as unknown by the helper;
    // it must never silently turn a new capture into a legacy one.
    await checkPairedApi(current, 15000).catch(() => {});
    await reconcileCaptureOwner({ duringStart: true }).catch(() => {});
    const existing = await getActiveCapture();
    if (existing) throw makeError("capture_busy", "Đã có một bản ghi đang chạy.");
    await ensureOffscreen();
    const offscreenState = await sendOffscreen({ type: "OFFSCREEN_STATUS" });
    if (offscreenState?.status?.status && ["starting", "recording", "stopping"].includes(offscreenState.status.status)) {
      throw makeError("capture_busy", "Đã có một bản ghi đang chạy.");
    }

    const clientRecordingId = crypto.randomUUID();
    const captureEpoch = crypto.randomUUID();
    const created = await createCaptureSession({
      clientRecordingId,
      captureEpoch,
      endpoint: current.binding,
      title: "ASR Studio capture",
      mimeType: MIME_TYPE,
      metadata: { sources: ["tab", "microphone"], mix: "mono", prototype: false },
      limits: current.binding.limits
    });
    if (!created.created) throw makeError("duplicate_start", "The local capture ID already exists.");

    let streamId;
    try {
      streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: sourceTab.id });
    } catch (error) {
      await markStartFailure(clientRecordingId, error);
      throw makeError("tab_capture_denied", "Chrome did not grant capture for the target tab.");
    }

    const result = await sendOffscreen({
      type: "START_CAPTURE",
      sessionId: clientRecordingId,
      streamId,
      selectedDeviceId: mic.selectedDeviceId,
      source
    });
    if (!result?.ok) {
      await markStartFailure(clientRecordingId, result?.error || makeError("capture_start_failed", "The offscreen capture did not start."));
      return result;
    }
    await broadcastStatus(result.status);
    return result;
  })();
  try {
    return await startLock;
  } finally {
    startLock = null;
  }
}

async function startFromActiveTab(source = "popup") {
  const tab = await activeSourceTab();
  return startFromTab(tab.id, source);
}

function uploadAlarmName(sessionId) {
  return `${UPLOAD_ALARM_PREFIX}${sessionId}`;
}

function pollAlarmName(sessionId) {
  return `${POLL_ALARM_PREFIX}${sessionId}`;
}

async function scheduleUpload(sessionId, delay = UPLOAD_RETRY_BACKOFF_MS) {
  await chrome.alarms.create(uploadAlarmName(sessionId), { when: Date.now() + delay });
}

async function ensureUploadWake(sessionId, decision) {
  if (decision?.action !== "wait" || !Number.isFinite(decision.retryAt)) return;
  const alarmName = uploadAlarmName(sessionId);
  const now = Date.now();
  const existing = await chrome.alarms.get(alarmName);
  if (!existing || !Number.isFinite(existing.scheduledTime) || existing.scheduledTime <= now || existing.scheduledTime > decision.retryAt) {
    await scheduleUpload(sessionId, Math.max(0, decision.retryAt - now));
  }
}

async function schedulePoll(sessionId, delay = 1500) {
  await chrome.alarms.create(pollAlarmName(sessionId), { when: Date.now() + delay });
}

function isRetryable(error) {
  return ["network_error", "timeout", "backend_unavailable", "backend_storage", "byte_limit", "api_version_mismatch"].includes(error?.code) || error?.status >= 500;
}

async function sessionContext(session) {
  const stored = await storageGet(TRUSTED_CONTEXTS);
  const contexts = stored[TRUSTED_CONTEXTS] || {};
  const original = contexts[contextKey(session.endpoint)];
  if (original && sameBinding(original.binding, session.endpoint)) return original;
  const current = await getCurrentContext();
  if (!current) throw makeError("pair_required", "Pair the extension before uploading retained audio.");
  if (!sameBinding(current.binding, session.endpoint)) throw makeError("binding_locked", "Retained audio is bound to its original backend.");
  return current;
}

async function cancelRemoteSession(session, context, options = {}) {
  if (!session.upload.serverSessionId || session.upload.cancelSettled) return session;
  const {
    allowRenew = true,
    canCommit = () => true,
    deadlineAt = null,
    persist = true,
    timeoutMs = 15000
  } = options;
  try {
    const response = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(session.upload.serverSessionId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ epoch: session.upload.serverEpoch })
    }, allowRenew, timeoutMs, deadlineAt);
    const remote = validateBackendIdentity(response.body, session.endpoint);
    if (remote.id !== session.upload.serverSessionId || remote.epoch !== session.upload.serverEpoch) {
      throw makeError("protocol_mismatch", "The server cancellation response is invalid.");
    }
    if (remote.state !== "canceled") {
      throw makeError("protocol_mismatch", "The server did not confirm cancellation.");
    }
  } catch (error) {
    if (!isCancelSettledError(error, { explicit: persist })) throw error;
    // A too_late answer can mean the server already saved the recording. Ask before
    // recording a cancel. Discard persists nothing and drops local audio either way.
    if (persist && error?.remoteCode === "too_late") {
      const remote = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(session.upload.serverSessionId)}`);
      if (cancelTooLateOutcome(remote.body) === "saved_remotely") {
        throw makeError("already_saved", "The server already saved this recording; it was not canceled.");
      }
    }
  }
  if (!persist || !canCommit() || (deadlineAt !== null && Date.now() >= deadlineAt)) return session;
  return patchSession(session.clientRecordingId, "upload", {
    state: "canceled",
    cancelSettled: true,
    retryAt: null,
    cancelRetryAt: null,
    cancelError: null,
    error: null
  });
}

async function createRemoteSession(session, context, options = {}) {
  if (session.upload.serverSessionId) return session;
  createPayloadForSession(session);
  const {
    allowRenew = true,
    canCommit = () => true,
    deadlineAt = null,
    persist = true,
    timeoutMs = 15000
  } = options;
  return runCreateWithAttempt({
    session,
    persistAttempt: (current) => current.upload.createAttempted
      ? current
      : patchSession(current.clientRecordingId, "upload", { createAttempted: true }),
    postCreate: (current) => authorizedRequest(context, "api/extension/v1/recording-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(createPayloadForSession(current))
    }, allowRenew, timeoutMs, deadlineAt),
    persistResult: (current, response) => {
      const remote = validateSessionResponse(response.body, current.endpoint);
      validateCreateResponse(response.body, current.capture.createIntent);
      const uploadPatch = {
        state: "receiving",
        serverSessionId: remote.id,
        serverEpoch: remote.epoch,
        nextSequence: remote.next_sequence || 0,
        nextOffset: remote.next_offset || 0,
        retryAt: null,
        cancelRetryAt: null,
        cancelError: null,
        cancelSettled: false,
        error: null
      };
      const resolved = { ...current, upload: { ...current.upload, ...uploadPatch } };
      if (!persist || !canCommit() || (deadlineAt !== null && Date.now() >= deadlineAt)) return resolved;
      return patchSession(current.clientRecordingId, "upload", uploadPatch);
    }
  });
}

async function recordInterruption(session, context) {
  const response = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(session.upload.serverSessionId)}/interrupt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      epoch: session.upload.serverEpoch,
      interruption_code: interruptionCodeFor({ code: session.capture.interruptionCode })
    })
  });
  const remote = validateBackendIdentity(response.body, session.endpoint);
  if (remote.id !== session.upload.serverSessionId || remote.epoch !== session.upload.serverEpoch) {
    throw makeError("protocol_mismatch", "The server interruption response is invalid.");
  }
  return patchSession(session.clientRecordingId, "upload", { interruptionRecorded: true, retryAt: null, error: null });
}

async function uploadChunks(session, context) {
  let current = session;
  while (current.upload.nextSequence < current.capture.chunkCount) {
    const sequence = current.upload.nextSequence;
    const chunk = await getChunk(current.clientRecordingId, sequence);
    if (!chunk) throw makeError("local_chunk_missing", "A retained audio chunk is missing.");
    const response = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(current.upload.serverSessionId)}/chunks/${sequence}`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Chunk-Offset": String(chunk.offset),
        "X-Chunk-SHA256": chunk.sha256,
        "X-Writer-Epoch": String(current.upload.serverEpoch)
      },
      body: chunk.blob
    });
    const ack = validateChunkAck(response.body, sequence, current.upload.serverEpoch);
    if (ack.next_offset !== chunk.offset + chunk.length) throw makeError("protocol_mismatch", "The server chunk offset acknowledgement is invalid.");
    current = await patchSession(current.clientRecordingId, "upload", {
      state: "receiving",
      nextSequence: ack.next_sequence,
      nextOffset: ack.next_offset,
      retryAt: null,
      error: null
    });
    await broadcastStatus();
  }
  return current;
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function prepareFinalize(session) {
  if (session.upload.finalizePayload) return session;
  const metas = await listChunkMetadata(session.clientRecordingId);
  const manifest = canonicalManifest(metas);
  if (manifest.chunkCount !== session.capture.chunkCount || manifest.totalBytes !== session.capture.totalBytes) {
    throw makeError("manifest_invalid", "The local audio manifest does not match the persisted capture.");
  }
  const manifestHash = await sha256Hex(new TextEncoder().encode(manifest.text));
  const finalizeId = crypto.randomUUID();
  const partial = Boolean(session.capture.interrupted);
  if (partial && session.upload.recoveryChoice !== "save") {
    throw makeError("capture_incomplete", "An interrupted capture is finalized only after Save recorded part.");
  }
  const payload = {
    finalize_id: finalizeId,
    capture_epoch: session.capture.captureEpoch,
    epoch: session.upload.serverEpoch,
    capture_complete: !partial,
    total_bytes: manifest.totalBytes,
    chunk_count: manifest.chunkCount,
    manifest_hash: manifestHash,
    chunks: metas,
    ...(partial ? { interruption_code: interruptionCodeFor({ code: session.capture.interruptionCode }) } : {})
  };
  return patchSession(session.clientRecordingId, "upload", {
    state: "finalize_pending",
    finalizeId,
    finalizePayload: payload,
    manifestHash,
    retryAt: null,
    error: null
  });
}

async function submitFinalize(session, context) {
  if (session.upload.finalizeSubmitted) return session;
  const response = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(session.upload.serverSessionId)}/finalize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(session.upload.finalizePayload)
  });
  if (response.status !== 202) throw makeError("protocol_mismatch", "The server finalize response is invalid.");
  return patchSession(session.clientRecordingId, "upload", {
    state: "finalizing",
    finalizeSubmitted: true,
    retryAt: null,
    error: null
  });
}

async function pollRemoteStatus(session, context) {
  const response = await authorizedRequest(context, `api/extension/v1/recording-sessions/${encodeURIComponent(session.upload.serverSessionId)}`);
  const remote = validateBackendIdentity(response.body, session.endpoint);
  if (remote.id !== session.upload.serverSessionId) throw makeError("receipt_mismatch", "The server returned another recording session.");
  let meetingUrl = session.upload.meetingUrl;
  if (remote.meeting_url) {
    try {
      resolveMeetingUrl(session.endpoint, remote.meeting_url);
      meetingUrl = remote.meeting_url;
    } catch {
      // A result link is optional and never delays a verified audio receipt.
      meetingUrl = null;
    }
  }
  if (remote.receipt) {
    validateReceipt(remote.receipt, session, session.upload.manifestHash);
    const cleaned = await cleanupReceipt(session.clientRecordingId, {
      state: "uploaded", cleaned: true, receipt: remote.receipt, meetingUrl, error: null
    }, {
      recordingId: session.upload.serverSessionId, backendId: session.endpoint.backendId,
      meetingUrl, receipt: remote.receipt, retainedAfterReceipt: true
    });
    await Promise.all([
      chrome.alarms.clear(uploadAlarmName(session.clientRecordingId)),
      chrome.alarms.clear(pollAlarmName(session.clientRecordingId))
    ]);
    await broadcastStatus(null, session.capture.interrupted
      ? "Saved recorded part. Start transcription for the interrupted recording in ASR Studio."
      : "Uploaded. The server has the complete audio; processing continues in ASR Studio.");
    return cleaned;
  }
  const failed = remote.state === "failed" || remote.state === "canceled";
  const updated = await patchSession(session.clientRecordingId, "upload", {
    state: failed ? "server_failed" : "awaiting_receipt", meetingUrl,
    retryAt: null,
    error: failed ? { code: "server_failed", message: "The backend did not confirm a complete archive. Local audio is retained." } : null
  });
  if (!failed) await schedulePoll(session.clientRecordingId, 1500);
  await broadcastStatus();
  return updated;
}

async function uploadSession(sessionId, source = "recovery") {
  return runLockedUploadPass({
    sessionId,
    source,
    locks: uploadLocks,
    getSession,
    startUpload: (id, nextSource) => uploadSession(id, nextSource),
    onWait: (decision) => ensureUploadWake(sessionId, decision),
    now: () => Date.now(),
    operation: async () => {
      let session = await getSession(sessionId);
      if (!session || session.upload.cleaned) return { session, recheck: false };
      let failedOperation = null;
      let context;
      try {
        while (true) {
          const decision = decideUpload(session, Date.now(), { source });
          if (decision.action === "idle") return { session, recheck: false };
          if (decision.action === "wait") {
            failedOperation = "wait";
            await ensureUploadWake(sessionId, decision);
            return { session, recheck: true };
          }

          failedOperation = decision.action;
          context = await sessionContext(session);
          if (decision.action === "cancel") {
            session = await cancelRemoteSession(session, context);
            await broadcastStatus();
            return { session, recheck: false };
          }
          if (decision.action === "create_session") {
            session = await createRemoteSession(session, context);
            continue;
          }
          if (decision.action === "reconcile_session") {
            session = await runReconcileCreateThenCancel({
              session,
              create: (current) => createRemoteSession(current, context),
              cancel: (resolved) => {
                failedOperation = "cancel";
                return cancelRemoteSession(resolved, context);
              }
            });
            await broadcastStatus();
            return { session, recheck: false };
          }
          if (decision.action === "record_interruption") {
            session = await recordInterruption(session, context);
            continue;
          }
          if (decision.action === "upload_chunks") {
            await uploadChunks(session, context);
            // Stop may have completed while the streaming pass held the lock. Re-read
            // captureComplete before deciding whether finalize is now allowed.
            session = await getSession(sessionId);
            continue;
          }
          if (decision.action === "finalize") {
            session = await prepareFinalize(await getSession(sessionId));
            session = await submitFinalize(session, context);
            continue;
          }
          if (decision.action === "poll_receipt") return { session: await pollRemoteStatus(session, context), recheck: false };
          return { session, recheck: false };
        }
      } catch (error) {
        try {
          if (context && error.code === "protocol_mismatch") {
            try {
              await checkApiAfterRejection(context, error, 15000);
            } catch (mismatch) {
              error = mismatch;
            }
          }
          session = await getSession(sessionId);
          const retryable = isRetryable(error);
          const failure = classifyUploadFailure(session, failedOperation, {
            retryable,
            cancelRetryable: isCancelRetryableError(error),
            uploadState: uploadStateForError(error, retryable)
          }, Date.now());
          session = await patchSession(sessionId, "upload", {
            ...failure.patch,
            cancelError: failure.recordCancelError ? safeError(error) : null,
            error: safeError(error)
          });
          if (failure.alarmDelay !== null) await scheduleUpload(sessionId, failure.alarmDelay);
          await broadcastStatus();
          return { session, recheck: failure.recheck };
        } catch {
          // Preserve the original upload failure.
        }
        return { session, recheck: false };
      }
    }
  });
}

async function resumeUploads() {
  const sessions = await listSessions();
  for (const session of sessions) {
    const decision = decideUpload(session, Date.now(), { source: "recovery" });
    if (decision.action === "wait") {
      await ensureUploadWake(session.clientRecordingId, decision);
    } else if (isUploadActionable(decision)) {
      await uploadSession(session.clientRecordingId, "recovery");
    }
  }
}

async function stopCapture() {
  await ensureOffscreen();
  const response = await sendOffscreen({ type: "STOP_CAPTURE" });
  if (response?.status?.captureComplete) void resumeUploads().catch(() => {});
  await broadcastStatus(response?.status || null);
  return response;
}

async function setGate(source, enabled) {
  await ensureOffscreen();
  const response = await sendOffscreen({ type: "SET_GATE", source, enabled: Boolean(enabled) });
  await broadcastStatus(response?.status || null);
  return response;
}

async function setTabMonitor(enabled) {
  await ensureOffscreen();
  const response = await sendOffscreen({ type: "SET_TAB_MONITOR", enabled: Boolean(enabled) });
  await broadcastStatus(response?.status || null);
  return response;
}

async function openResult(sessionId) {
  const session = await getSession(sessionId);
  if (!session?.upload?.meetingUrl || !session.upload.receipt) throw makeError("result_unavailable", "The backend result is not ready.");
  const url = resolveMeetingUrl(session.endpoint, session.upload.meetingUrl);
  await chrome.tabs.create({ url, active: true });
  return { ok: true };
}

async function discardCapture(sessionId) {
  const active = await getActiveCapture();
  if (active?.clientRecordingId === sessionId) throw makeError("capture_busy", "Stop the active capture before discarding local audio.");
  const session = await getSession(sessionId);
  const decision = decideDiscard(session);
  if (["cancel_once_then_discard", "reconcile_then_cancel_once_then_discard"].includes(decision.action)) {
    await runBoundedDiscardCancel({
      timeoutMs: DISCARD_CANCEL_TIMEOUT_MS,
      cancel: async ({ deadlineAt, isActive }) => {
        const context = await sessionContext(session);
        if (decision.action === "reconcile_then_cancel_once_then_discard") {
          return runReconcileCreateThenCancel({
            session,
            create: (current) => createRemoteSession(current, context, {
              allowRenew: false,
              canCommit: isActive,
              deadlineAt,
              persist: false
            }),
            cancel: (resolved) => cancelRemoteSession(resolved, context, {
              allowRenew: false,
              canCommit: isActive,
              deadlineAt,
              persist: false
            })
          });
        }
        return cancelRemoteSession(session, context, {
          allowRenew: false,
          canCommit: isActive,
          deadlineAt,
          persist: false
        });
      },
      discard: () => discardLocalCapture(sessionId)
    });
  } else {
    await discardLocalCapture(sessionId);
  }
  await chrome.alarms.clear(`${UPLOAD_ALARM_PREFIX}${sessionId}`);
  await chrome.alarms.clear(`${POLL_ALARM_PREFIX}${sessionId}`);
  await broadcastStatus();
  return { ok: true };
}

// Save uploads the persisted prefix and finalizes it as interrupted; Cancel keeps
// the earlier semantics: cancel the server session and retain local audio.
// The first choice commits atomically in IndexedDB; the other one is refused, so a
// stale view cannot cancel a save in flight or save a canceled session.
async function chooseRecovery(sessionId, choice) {
  const committed = await commitRecoveryChoice(sessionId, choice, decideRecoveryChoice);
  if (!committed.ok) throw makeError(committed.code, "This recording cannot take that action now.");
  const updated = await uploadSession(sessionId, "manual");
  await broadcastStatus();
  return { ok: true, status: updated ? publicSession(updated) : null };
}

async function processCaptureStatus(status) {
  await broadcastStatus(status || null);
  const sessionId = status?.sessionId;
  if (!sessionId) return;
  const session = await getSession(sessionId);
  if (!session) return;
  const decision = decideUpload(session, Date.now(), { source: "status" });
  if (decision.action === "wait") {
    await ensureUploadWake(sessionId, decision);
  } else if (isUploadActionable(decision)) {
    await uploadSession(sessionId, "status");
    await broadcastStatus();
  }
}

async function messageFromUi(message) {
  await recoveryReady;
  switch (message?.type) {
    case "SET_UPDATE_CHECKS":
      return setUpdateChecks(message.enabled === true);
    case "GET_STATUS":
      await reconcileCaptureOwner().catch(() => {});
      return { ok: true, status: await publicStatus() };
    case "START_CAPTURE":
      return startFromActiveTab("popup");
    case "OPEN_SETUP":
      await openControlPage(true);
      return { ok: true };
    case "OPEN_WEB_ROOT":
      return openWebRoot();
    case "PAIR":
      return { ok: true, connection: await connectionTransition(() => pairWithServer(message.serverUrl, message.code)) };
    case "SET_SERVER_URL":
      return { ok: true, connection: await connectionTransition(() => setServerUrl(message.serverUrl)) };
    case "SET_MIC_SETUP":
      return { ok: true, mic: await setMicSetup(message.selectedDeviceId) };
    case "CLEAR_PAIRING":
      return { ok: true, connection: await connectionTransition(() => clearPairing()) };
    case "EXECUTE_ACTION": {
      return startFromActiveTab("keyboard-diagnostic");
    }
    case "STOP_CAPTURE":
      return stopCapture();
    case "SET_GATE":
      return setGate(message.source, message.enabled);
    case "SET_TAB_MONITOR":
      return setTabMonitor(message.enabled);
    case "RETRY_UPLOAD":
      return { ok: true, status: publicSession(await uploadSession(message.sessionId, "manual")) };
    case "OPEN_RESULT":
      return openResult(message.sessionId);
    case "DISCARD_CAPTURE":
      return discardCapture(message.sessionId);
    case "SAVE_INTERRUPTED":
      return chooseRecovery(message.sessionId, "save");
    case "CANCEL_INTERRUPTED":
      return chooseRecovery(message.sessionId, "cancel");
    default:
      throw makeError("unknown_message", "The control message is not supported.");
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === UPDATE_ALARM) void checkForUpdate().catch(() => {});
  else if (alarm.name.startsWith(UPLOAD_ALARM_PREFIX)) void uploadSession(alarm.name.slice(UPLOAD_ALARM_PREFIX.length));
  else if (alarm.name.startsWith(POLL_ALARM_PREFIX)) void uploadSession(alarm.name.slice(POLL_ALARM_PREFIX.length));
});

chrome.runtime.onStartup.addListener(() => void recoveryReady.then(resumeUploads).catch(() => {}));
chrome.runtime.onInstalled.addListener(() => void recoveryReady.then(resumeUploads).catch(() => {}));

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== "worker") return false;
  if (senderIsOffscreen(sender)) {
    if (message?.type === "CAPTURE_STATUS") {
      void processCaptureStatus(message.status || null).catch(() => {});
      sendResponse({ ok: true });
      return false;
    }
    sendResponse(errorResponse(makeError("forbidden_message", "The offscreen page may only report capture status.")));
    return false;
  }
  if (!senderIsControl(sender) && !senderIsPopup(sender)) {
    sendResponse(errorResponse(makeError("forbidden_sender", "Only the extension popup or control page may send this message.")));
    return false;
  }
  messageFromUi(message)
    .then(sendResponse)
    .catch((error) => sendResponse(errorResponse(error)));
  return true;
});

if (chrome.runtime.id !== EXTENSION_ID) {
  console.warn("Production extension ID differs from the pinned public key.");
}

// Browser/owner restart is an interruption, never continuation of a new encoder.
// Runs at startup, on status reads and before a new start, so an offscreen page lost
// while the worker stays alive is detected too. During start the new row has no owner yet.
async function reconcileCaptureOwner({ duringStart = false } = {}) {
  if (startLock && !duringStart) return;
  const active = (await listSessions()).filter(isActiveCapture);
  if (!active.length) return;
  const hasOwner = await chrome.offscreen.hasDocument();
  let reply = null;
  if (hasOwner) {
    try { reply = await sendOffscreen({ type: "OFFSCREEN_STATUS" }); } catch { return; }
  }
  let changed = false;
  for (const session of active) {
    if (!isCaptureOwnerLost(session, hasOwner, reply)) continue;
    // The owner may have finished between the read and the answer; the write re-checks the row
    // and keeps a reason already recorded for it.
    const { applied } = await patchSessionWith(session.clientRecordingId, "capture", (current) => (
      isActiveCapture(current)
        ? ownerLostPatch(current.capture, "The capture owner ended before a clean Stop. Retained audio was not finalized.")
        : null
    ));
    changed ||= applied;
  }
  if (changed) {
    await broadcastStatus();
    void resumeUploads().catch(() => {});
  }
}
const recoveryReady = requireTrustedStorage().then(purgeDiscardedCaptures).then(reconcileCaptureOwner);
void recoveryReady.then(resumeUploads).catch(() => {});
void recoveryReady.then(getCurrentContext).then((context) => {
  if (context) return checkPairedApi(context, 15000);
}).catch(() => {});

void serializeUpdatePreference(reconcileUpdateAlarm).catch(() => {});
chrome.permissions.onRemoved.addListener(() => {
  void serializeUpdatePreference(reconcileUpdateAlarm).then(() => broadcastStatus()).catch(() => {});
});
