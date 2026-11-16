export const CAPTURE_STATES = Object.freeze({
  STARTING: "starting",
  RECORDING: "recording",
  STOPPING: "stopping",
  COMPLETE: "complete",
  INTERRUPTED: "interrupted"
});

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
