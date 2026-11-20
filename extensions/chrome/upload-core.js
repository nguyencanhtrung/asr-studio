export const UPLOAD_RETRY_BACKOFF_MS = 5000;
export const CANCEL_RETRY_BACKOFF_MS = 5000;
export const NON_RETRYABLE_UPLOAD_STATES = new Set([
  "failed",
  "needs_reconnect",
  "auth_required",
  "receipt_mismatch",
  "server_failed"
]);

export function isCancelSettledError(error) {
  return error?.status === 404 || ["recording_not_found", "too_late", "already_canceled", "canceled"].includes(error?.remoteCode);
}

export function isCancelRetryableError(error) {
  return ["network_error", "timeout", "backend_unavailable"].includes(error?.code) || error?.status >= 500;
}

export function classifyUploadFailure(session, failedOperation, errorFacts = {}, now) {
  if (!Number.isFinite(now)) throw new TypeError("classifyUploadFailure requires a finite time value.");

  const capture = session?.capture || {};
  const upload = session?.upload || {};
  const interruptedRemote = Boolean(capture.interrupted && upload.recoveryChoice === "cancel" && upload.serverSessionId && !upload.cancelSettled);
  const cancelOperationFailed = failedOperation === "cancel";
  const cancelRetryable = cancelOperationFailed && interruptedRemote && Boolean(errorFacts.cancelRetryable);
  const state = cancelOperationFailed && interruptedRemote
    ? cancelRetryable ? "cancel_retryable" : "cancel_failed"
    : errorFacts.uploadState || (errorFacts.retryable ? "retryable" : "failed");
  const retryAt = now + (cancelRetryable ? CANCEL_RETRY_BACKOFF_MS : UPLOAD_RETRY_BACKOFF_MS);

  return {
    patch: {
      state,
      retryAt: cancelOperationFailed ? null : state === "retryable" ? retryAt : null,
      cancelRetryAt: cancelRetryable ? retryAt : null
    },
    recordCancelError: Boolean(cancelOperationFailed && interruptedRemote),
    alarmDelay: state === "retryable" || state === "cancel_retryable" ? retryAt - now : null,
    recheck: Boolean(interruptedRemote && !cancelOperationFailed)
  };
}

function retryAt(value) {
  return Number.isFinite(value) ? value : null;
}

export function isUploadActionable(decision) {
  return decision?.action !== "idle" && decision?.action !== "wait";
}

export function decideUpload(session, now, options = {}) {
  if (!session) return { action: "idle", reason: "missing" };
  if (!Number.isFinite(now)) throw new TypeError("decideUpload requires a finite time value.");

  const source = options?.source || "recovery";
  const capture = session.capture || {};
  const upload = session.upload || {};
  if (upload.cleaned || upload.receipt) return { action: "idle", reason: "receipt" };

  const interrupted = Boolean(capture.interrupted || capture.status === "interrupted");
  if (interrupted && upload.cancelSettled) return { action: "idle", reason: "interrupted_without_server" };
  if (interrupted && upload.recoveryChoice === "cancel") return decideCancel(capture, upload, source, now);
  // Until the user chooses, an interrupted capture keeps its server session open and
  // still delivers persisted chunks, but it is never finalized or created anew.
  const awaitingChoice = interrupted && upload.recoveryChoice !== "save";
  if (awaitingChoice && !upload.serverSessionId) return { action: "idle", reason: "interrupted_choice_required" };
  if (interrupted && !(capture.chunkCount > 0)) return { action: "idle", reason: "nothing_to_save" };

  if (!capture.createPayload) return { action: "idle", reason: "capture_metadata_missing" };
  if (source === "status" && NON_RETRYABLE_UPLOAD_STATES.has(upload.state)) {
    return { action: "idle", reason: "non_retryable_status" };
  }

  const uploadRetryAt = retryAt(upload.retryAt);
  if (uploadRetryAt !== null && uploadRetryAt > now) {
    return { action: "wait", reason: "upload_backoff", retryAt: uploadRetryAt };
  }

  if (!upload.serverSessionId) return { action: "create_session" };
  if (upload.nextSequence < (capture.chunkCount || 0)) {
    return {
      action: "upload_chunks",
      fromSequence: upload.nextSequence,
      throughSequence: capture.chunkCount
    };
  }
  if (awaitingChoice) return { action: "idle", reason: "interrupted_choice_required" };
  // A saved interrupted prefix finalizes as incomplete; it never waits for Stop.
  if (!capture.captureComplete && !interrupted) return { action: "wait", reason: "capture_in_progress" };
  if (!upload.finalizeSubmitted) return { action: "finalize" };
  return { action: "poll_receipt" };
}

function decideCancel(capture, upload, source, now) {
  if (!upload.serverSessionId) {
    if (!upload.createAttempted || !capture.createPayload) {
      return { action: "idle", reason: "interrupted_without_server" };
    }
    if (source === "status" && NON_RETRYABLE_UPLOAD_STATES.has(upload.state)) {
      return { action: "idle", reason: "non_retryable_status" };
    }
    const reconcileRetryAt = retryAt(upload.retryAt);
    if (reconcileRetryAt !== null && reconcileRetryAt > now) {
      return { action: "wait", reason: "upload_backoff", retryAt: reconcileRetryAt };
    }
    return { action: "reconcile_session" };
  }
  const cancelRetryAt = retryAt(upload.cancelRetryAt);
  if (source === "status" && upload.cancelError && cancelRetryAt === null) {
    return { action: "idle", reason: "cancel_non_retryable" };
  }
  if (cancelRetryAt !== null && cancelRetryAt > now) {
    return { action: "wait", reason: "cancel_backoff", retryAt: cancelRetryAt };
  }
  return {
    action: "cancel",
    serverSessionId: upload.serverSessionId,
    epoch: upload.serverEpoch
  };
}

export const RECOVERY_CHOICES = Object.freeze(["save", "cancel"]);

// Validates the user's choice for an interrupted capture before it is persisted.
export function decideRecoveryChoice(session, choice) {
  const capture = session?.capture || {};
  const upload = session?.upload || {};
  if (!RECOVERY_CHOICES.includes(choice)) return { ok: false, code: "invalid_choice" };
  if (!(capture.interrupted || capture.status === "interrupted")) return { ok: false, code: "not_interrupted" };
  if (upload.cleaned || upload.receipt) return { ok: false, code: "already_saved" };
  if (upload.cancelSettled) return { ok: false, code: "already_canceled" };
  if (choice === "save" && !(capture.chunkCount > 0)) return { ok: false, code: "nothing_to_save" };
  if (choice === "cancel" && upload.finalizeSubmitted) return { ok: false, code: "already_saving" };
  return { ok: true };
}

// True while an interrupted capture still waits for Save or Cancel.
export function awaitsRecoveryChoice(session) {
  return decideRecoveryChoice(session, "save").ok && !session?.upload?.recoveryChoice;
}

export function decideDiscard(session) {
  const capture = session?.capture || {};
  const upload = session?.upload || {};
  if (upload.serverSessionId && !upload.receipt && !upload.cleaned && !upload.cancelSettled) {
    return {
      action: "cancel_once_then_discard",
      serverSessionId: upload.serverSessionId,
      epoch: upload.serverEpoch
    };
  }
  if (upload.createAttempted && capture.createPayload && !upload.receipt && !upload.cleaned && !upload.cancelSettled) {
    return { action: "reconcile_then_cancel_once_then_discard" };
  }
  return { action: "discard_local" };
}
