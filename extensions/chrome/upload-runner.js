import { decideUpload, isUploadActionable } from "./upload-core.js";

export async function runCreateWithAttempt({ session, persistAttempt, postCreate, persistResult }) {
  const attempted = await persistAttempt(session);
  const response = await postCreate(attempted);
  return persistResult(attempted, response);
}

export async function runReconcileCreateThenCancel({
  session,
  create,
  cancel
}) {
  const resolved = await create(session);
  return cancel(resolved);
}

export async function recheckUploadPass({ sessionId, getSession, startUpload, onWait, source = "recovery", now }) {
  const fresh = await getSession(sessionId);
  const decision = decideUpload(fresh, now, { source });
  if (decision.action === "wait" && onWait) await onWait(decision);
  if (isUploadActionable(decision)) return startUpload(sessionId, source);
  return fresh;
}

async function settleUploadPass({ sessionId, source, getSession, startUpload, onWait, now }, outcome) {
  if (!outcome?.recheck) return outcome?.session || (await getSession(sessionId));
  return recheckUploadPass({
    sessionId,
    getSession,
    startUpload,
    onWait,
    source,
    now: typeof now === "function" ? now() : now
  });
}

export function runLockedUploadPass({
  sessionId,
  source = "recovery",
  locks,
  operation,
  getSession,
  startUpload,
  onWait,
  now
}) {
  const existing = locks.get(sessionId);
  if (existing) {
    const settleJoiner = () => recheckUploadPass({
      sessionId,
      getSession,
      startUpload,
      onWait,
      source,
      now: typeof now === "function" ? now() : now
    });
    return existing.then(settleJoiner, settleJoiner);
  }

  const operationPromise = Promise.resolve().then(operation);
  const settled = operationPromise.then(
    (outcome) => {
      locks.delete(sessionId);
      return outcome;
    },
    (error) => {
      locks.delete(sessionId);
      throw error;
    }
  );
  locks.set(sessionId, settled);
  return settled.then((outcome) => settleUploadPass({
    sessionId, source, getSession, startUpload, onWait, now
  }, outcome));
}

export async function runBoundedDiscardCancel({
  cancel,
  discard,
  timeoutMs = 5000,
  now = () => Date.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout
}) {
  const deadlineAt = now() + timeoutMs;
  let active = true;
  let timerId;
  const cancelPromise = Promise.resolve().then(() => cancel({
    deadlineAt,
    isActive: () => active
  }));
  const cancelOutcome = cancelPromise.then(() => "cancel", () => "cancel");
  const timeoutOutcome = new Promise((resolve) => {
    timerId = setTimer(() => resolve("timeout"), Math.max(0, deadlineAt - now()));
  });
  const winner = await Promise.race([cancelOutcome, timeoutOutcome]);
  active = false;
  if (timerId !== undefined) clearTimer(timerId);
  await discard();
  return { cancelFinished: winner === "cancel", timedOut: winner === "timeout" };
}
