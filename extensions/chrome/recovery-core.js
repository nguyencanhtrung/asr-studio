import { sameBinding } from "./protocol.js";

const ACTIVE_CAPTURE_STATES = new Set(["starting", "recording", "stopping"]);

export function pairingGuard(pending, normalizedServerUrl, binding = null) {
  const sessions = Array.isArray(pending) ? pending : [];
  if (sessions.some((session) => ACTIVE_CAPTURE_STATES.has(session?.capture?.status))) {
    return {
      code: "pair_locked",
      message: "Stop the active capture before pairing again. Retained audio stays local."
    };
  }
  if (sessions.some((session) => session?.endpoint?.serverUrl !== normalizedServerUrl)) {
    return {
      code: "url_locked",
      message: "The server URL cannot change while capture bytes are pending."
    };
  }
  if (binding && sessions.some((session) => !sameBinding(session?.endpoint, binding))) {
    return {
      code: "binding_locked",
      message: "Retained audio is bound to its original backend. Use Pair again for that existing device in Settings → Devices."
    };
  }
  return null;
}

export function uploadStateForError(error, retryable) {
  if (error?.code === "pair_required" || error?.code === "binding_locked" || error?.code === "not_found" || error?.status === 404) {
    return "needs_reconnect";
  }
  if (error?.code === "auth_expired") return "auth_required";
  if (error?.code === "receipt_mismatch" || error?.code === "invalid_result_url") return "receipt_mismatch";
  return retryable ? "retryable" : "failed";
}
