export const CAPTURE_STATES = Object.freeze({
  STARTING: "starting",
  RECORDING: "recording",
  STOPPING: "stopping",
  COMPLETE: "complete",
  INTERRUPTED: "interrupted"
});

// One MediaRecorder timeslice per persisted chunk.
export const MEDIA_RECORDER_TIMESLICE_MS = 5000;

// Recorded length shown for a retained capture, from its persisted chunk count.
export function recordedDurationLabel(chunkCount) {
  const total = Number.isSafeInteger(chunkCount) && chunkCount > 0 ? chunkCount * MEDIA_RECORDER_TIMESLICE_MS / 1000 : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const seconds = Math.floor(total % 60);
  const pad = (value) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

export function canStartCapture(capture) {
  return !capture || ![CAPTURE_STATES.STARTING, CAPTURE_STATES.RECORDING, CAPTURE_STATES.STOPPING].includes(capture.status);
}

export function isUnexpectedTrackEnd(capture, intentionalStop) {
  return Boolean(capture && capture.status === CAPTURE_STATES.RECORDING && !intentionalStop);
}

export function canMarkComplete(capture) {
  return Boolean(capture && capture.status === CAPTURE_STATES.STOPPING && !capture.interrupted && capture.totalBytes > 0 && !capture.persistenceError);
}

export function captureLabel(capture) {
  if (!capture) return "idle";
  if (capture.status === CAPTURE_STATES.COMPLETE && capture.captureComplete) return "captured";
  if (capture.status === CAPTURE_STATES.INTERRUPTED) return "interrupted";
  return capture.status;
}

// Interruption codes the server accepts when the user saves a recorded prefix.
export const INTERRUPTION_CODES = Object.freeze([
  "source_ended",
  "source_inactive",
  "write_backlog",
  "recorder_error",
  "capture_incomplete",
  "owner_restarted",
  "unknown"
]);

export function interruptionCodeFor(error) {
  const code = typeof error?.code === "string" ? error.code : "";
  return INTERRUPTION_CODES.includes(code) ? code : "unknown";
}
