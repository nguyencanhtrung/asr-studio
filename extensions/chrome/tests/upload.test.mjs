import assert from "node:assert/strict";
import test from "node:test";

import {
  CANCEL_RETRY_BACKOFF_MS,
  classifyUploadFailure,
  decideDiscard,
  decideUpload,
  isCancelRetryableError,
  isCancelSettledError,
  UPLOAD_RETRY_BACKOFF_MS
} from "../upload-core.js";
import {
  recheckUploadPass,
  runCreateWithAttempt,
  runBoundedDiscardCancel,
  runLockedUploadPass,
  runReconcileCreateThenCancel
} from "../upload-runner.js";

const NOW = 10_000;

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const session = (overrides = {}) => ({
  capture: {
    status: "recording",
    createPayload: { client_recording_id: "client-1" },
    chunkCount: 3,
    captureComplete: false,
    interrupted: false,
    ...overrides.capture
  },
  upload: {
    state: "receiving",
    createAttempted: false,
    serverSessionId: "server-1",
    serverEpoch: 1,
    nextSequence: 2,
    finalizeSubmitted: false,
    cleaned: false,
    receipt: null,
    ...overrides.upload
  }
});

test("a Stop race re-reads completion and chooses finalize after the last streamed chunk", () => {
  const streaming = session();
  assert.deepEqual(decideUpload(streaming, NOW), {
    action: "upload_chunks",
    fromSequence: 2,
    throughSequence: 3
  });

  const stopped = session({
    capture: { status: "complete", captureComplete: true },
    upload: { nextSequence: 3 }
  });
  assert.equal(decideUpload(session({ upload: { nextSequence: 3 } }), NOW).action, "wait");
  assert.equal(decideUpload(stopped, NOW).action, "finalize");
});

test("upload decisions require an explicit time value", () => {
  assert.throws(() => decideUpload(session()), /finite time value/);
});

test("failure classification requires an explicit time value", () => {
  assert.throws(() => classifyUploadFailure(session(), "upload_chunks", { retryable: true }), /finite time value/);
});

test("a settled pass rechecks fresh completion and starts the follow-up pass", async () => {
  const stale = session({ upload: { nextSequence: 3 } });
  assert.equal(decideUpload(stale, NOW).action, "wait");
  const fresh = session({
    capture: { status: "complete", captureComplete: true },
    upload: { nextSequence: 3 }
  });
  let starts = 0;
  const result = await recheckUploadPass({
    sessionId: "client-1",
    getSession: async () => fresh,
    startUpload: async (sessionId, source) => {
      starts += 1;
      return { sessionId, source };
    },
    now: NOW
  });
  assert.equal(starts, 1);
  assert.deepEqual(result, { sessionId: "client-1", source: "recovery" });
});

test("retryable upload failure waits for a backoff instead of retrying on status chatter", () => {
  assert.ok(UPLOAD_RETRY_BACKOFF_MS >= 5000);
  const retryAt = 10_000;
  const waiting = session({ upload: { retryAt } });
  assert.deepEqual(decideUpload(waiting, retryAt - 1), {
    action: "wait",
    reason: "upload_backoff",
    retryAt
  });
  assert.equal(decideUpload(waiting, retryAt).action, "upload_chunks");
});

test("upload failure on an interrupted session records no cancel error and rechecks", () => {
  const interrupted = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { cancelRetryAt: null, cancelError: null, cancelSettled: false }
  });
  assert.deepEqual(classifyUploadFailure(interrupted, "upload_chunks", {
    retryable: false,
    cancelRetryable: false,
    uploadState: "failed"
  }, NOW), {
    patch: { state: "failed", retryAt: null, cancelRetryAt: null },
    recordCancelError: false,
    alarmDelay: null,
    recheck: true
  });
});

test("retryable cancellation records cancel error and schedules cancel retry", () => {
  const interrupted = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { cancelRetryAt: null, cancelError: null, cancelSettled: false }
  });
  assert.deepEqual(classifyUploadFailure(interrupted, "cancel", {
    retryable: true,
    cancelRetryable: true,
    uploadState: "retryable"
  }, NOW), {
    patch: {
      state: "cancel_retryable",
      retryAt: null,
      cancelRetryAt: NOW + CANCEL_RETRY_BACKOFF_MS
    },
    recordCancelError: true,
    alarmDelay: CANCEL_RETRY_BACKOFF_MS,
    recheck: false
  });
});

test("non-retryable cancellation records cancel error without an alarm", () => {
  const interrupted = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { cancelRetryAt: null, cancelError: null, cancelSettled: false }
  });
  assert.deepEqual(classifyUploadFailure(interrupted, "cancel", {
    retryable: false,
    cancelRetryable: false,
    uploadState: "failed"
  }, NOW), {
    patch: { state: "cancel_failed", retryAt: null, cancelRetryAt: null },
    recordCancelError: true,
    alarmDelay: null,
    recheck: false
  });
});

test("retryable upload failure on a healthy session records an upload retry", () => {
  assert.deepEqual(classifyUploadFailure(session(), "upload_chunks", {
    retryable: true,
    cancelRetryable: false,
    uploadState: "retryable"
  }, NOW), {
    patch: {
      state: "retryable",
      retryAt: NOW + UPLOAD_RETRY_BACKOFF_MS,
      cancelRetryAt: null
    },
    recordCancelError: false,
    alarmDelay: UPLOAD_RETRY_BACKOFF_MS,
    recheck: false
  });
});

test("non-retryable upload failure has no retry timestamp or alarm", () => {
  assert.deepEqual(classifyUploadFailure(session(), "upload_chunks", {
    retryable: false,
    cancelRetryable: false,
    uploadState: "failed"
  }, NOW), {
    patch: { state: "failed", retryAt: null, cancelRetryAt: null },
    recordCancelError: false,
    alarmDelay: null,
    recheck: false
  });
});

test("a retry wait exposes its exact wake time for alarm re-arming", async () => {
  const retryAt = 10_000;
  const waiting = session({ upload: { retryAt } });
  let wake;
  const result = await recheckUploadPass({
    sessionId: "client-1",
    getSession: async () => waiting,
    startUpload: () => assert.fail("a backoff wait must not start an upload"),
    onWait: async (decision) => {
      wake = decision;
    },
    now: retryAt - 1
  });
  assert.equal(result, waiting);
  assert.equal(wake?.retryAt, retryAt);
});

test("status chatter does not retry non-retryable upload states, but recovery does", () => {
  for (const state of ["failed", "needs_reconnect", "auth_required", "receipt_mismatch", "server_failed"]) {
    const blocked = session({ upload: { state, nextSequence: 2 } });
    assert.deepEqual(decideUpload(blocked, NOW, { source: "status" }), {
      action: "idle",
      reason: "non_retryable_status"
    });
    assert.equal(decideUpload(blocked, NOW, { source: "recovery" }).action, "upload_chunks");
    assert.equal(decideUpload(blocked, NOW, { source: "manual" }).action, "upload_chunks");
  }
});

test("an interrupted capture with a server session chooses cancellation and keeps local bytes", () => {
  const interrupted = session({
    capture: { status: "interrupted", interrupted: true, captureComplete: false },
    upload: { cancelRetryAt: null }
  });
  assert.deepEqual(decideUpload(interrupted, NOW), {
    action: "cancel",
    serverSessionId: "server-1",
    epoch: 1
  });
  assert.equal(decideUpload(session({
    capture: { status: "interrupted", interrupted: true },
    upload: { cancelRetryAt: 20_000 }
  }), 19_999).action, "wait");
  assert.ok(CANCEL_RETRY_BACKOFF_MS >= 5000);
  assert.equal(isCancelSettledError({ status: 404 }), true);
  assert.equal(isCancelSettledError({ status: 409, remoteCode: "too_late" }), true);
  assert.equal(isCancelSettledError({ status: 409, remoteCode: "writer_epoch_conflict" }), false);
});

test("an uncertain interrupted create reconciles by source, honors backoff, and cancels once identified", () => {
  const uncertain = session({
    capture: { status: "interrupted", interrupted: true, captureComplete: false },
    upload: { createAttempted: true, serverSessionId: null, retryAt: null, cancelSettled: false }
  });
  for (const source of ["status", "recovery", "manual"]) {
    assert.deepEqual(decideUpload(uncertain, NOW, { source }), { action: "reconcile_session" });
  }

  const waiting = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { createAttempted: true, serverSessionId: null, retryAt: NOW + 1000 }
  });
  assert.deepEqual(decideUpload(waiting, NOW, { source: "recovery" }), {
    action: "wait",
    reason: "upload_backoff",
    retryAt: NOW + 1000
  });

  const unmarked = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { createAttempted: false, serverSessionId: null }
  });
  assert.deepEqual(decideUpload(unmarked, NOW), {
    action: "idle",
    reason: "interrupted_without_server"
  });

  const identified = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { createAttempted: true, serverSessionId: "server-1", serverEpoch: 1 }
  });
  assert.deepEqual(decideUpload(identified, NOW), {
    action: "cancel",
    serverSessionId: "server-1",
    epoch: 1
  });
});

test("reconcile create failures schedule retryable wakeups and suppress non-retryable status retries", () => {
  const uncertain = session({
    capture: { status: "interrupted", interrupted: true },
    upload: { createAttempted: true, serverSessionId: null, retryAt: null }
  });
  assert.deepEqual(classifyUploadFailure(uncertain, "reconcile_session", {
    retryable: true,
    cancelRetryable: false,
    uploadState: "retryable"
  }, NOW), {
    patch: { state: "retryable", retryAt: NOW + UPLOAD_RETRY_BACKOFF_MS, cancelRetryAt: null },
    recordCancelError: false,
    alarmDelay: UPLOAD_RETRY_BACKOFF_MS,
    recheck: false
  });

  const failed = {
    ...uncertain,
    upload: {
      ...uncertain.upload,
      state: "failed",
      retryAt: null
    }
  };
  assert.deepEqual(decideUpload(failed, NOW, { source: "status" }), {
    action: "idle",
    reason: "non_retryable_status"
  });
  assert.deepEqual(decideUpload(failed, NOW, { source: "recovery" }), {
    action: "reconcile_session"
  });
});

test("non-retryable cancellation stops status retries but leaves recovery and discard paths", () => {
  assert.equal(isCancelRetryableError({ code: "network_error" }), true);
  assert.equal(isCancelRetryableError({ code: "timeout" }), true);
  assert.equal(isCancelRetryableError({ code: "backend_unavailable" }), true);
  assert.equal(isCancelRetryableError({ status: 503 }), true);
  assert.equal(isCancelRetryableError({ code: "auth_expired", status: 401 }), false);
  assert.equal(isCancelRetryableError({ status: 409, remoteCode: "writer_epoch_conflict" }), false);

  const failed = session({
    capture: { status: "interrupted", interrupted: true, captureComplete: false },
    upload: { cancelRetryAt: null, cancelError: { code: "auth_expired" }, cancelSettled: false }
  });
  assert.equal(decideUpload(failed, NOW, { source: "status" }).action, "idle");
  assert.equal(decideUpload(failed, NOW, { source: "recovery" }).action, "cancel");
  assert.equal(decideUpload(failed, NOW, { source: "manual" }).action, "cancel");
  assert.equal(failed.upload.cancelSettled, false);
});

test("discard cancels a remote session once but always proceeds with local discard", () => {
  assert.deepEqual(decideDiscard(session()), {
    action: "cancel_once_then_discard",
    serverSessionId: "server-1",
    epoch: 1
  });
  assert.deepEqual(decideDiscard(session({ upload: { serverSessionId: null } })), {
    action: "discard_local"
  });
  assert.deepEqual(decideDiscard(session({
    capture: { status: "interrupted", interrupted: true },
    upload: { createAttempted: true, serverSessionId: null }
  })), {
    action: "reconcile_then_cancel_once_then_discard"
  });
});

test("reconcile persists the create marker before POST, then cancels without upload or finalize", async () => {
  const events = [];
  const original = session({
    capture: { status: "interrupted", interrupted: true },
    upload: {
      createAttempted: false,
      serverSessionId: null,
      nextSequence: 0,
      finalizeSubmitted: false
    }
  });
  const result = await runReconcileCreateThenCancel({
    session: original,
    create: (current) => runCreateWithAttempt({
      session: current,
      persistAttempt: async (attempt) => {
        events.push("persist-marker");
        return { ...attempt, upload: { ...attempt.upload, createAttempted: true } };
      },
      postCreate: async (attempt) => {
        events.push("create");
        assert.equal(attempt.upload.createAttempted, true);
        assert.deepEqual(attempt.capture.createPayload, { client_recording_id: "client-1" });
        return { id: "server-1", epoch: 1 };
      },
      persistResult: async (attempt, remote) => {
        events.push("persist-id-epoch");
        return {
          ...attempt,
          upload: {
            ...attempt.upload,
            serverSessionId: remote.id,
            serverEpoch: remote.epoch
          }
        };
      }
    }),
    cancel: async (resolved) => {
      events.push("cancel");
      assert.equal(resolved.upload.serverSessionId, "server-1");
      assert.equal(resolved.upload.serverEpoch, 1);
      assert.equal(resolved.upload.nextSequence, 0);
      assert.equal(resolved.upload.finalizeSubmitted, false);
      return { ...resolved, upload: { ...resolved.upload, cancelSettled: true } };
    }
  });
  assert.deepEqual(events, ["persist-marker", "create", "persist-id-epoch", "cancel"]);
  assert.equal(result.upload.cancelSettled, true);
  assert.equal(events.includes("upload"), false);
  assert.equal(events.includes("finalize"), false);
});

test("discard releases local deletion when authorization stalls and fences the late cancel", async () => {
  let release;
  const authorization = new Promise((resolve) => { release = resolve; });
  const events = [];
  let lateCommit = 0;
  const resultPromise = runBoundedDiscardCancel({
    timeoutMs: 10,
    now: () => 0,
    cancel: async ({ isActive }) => {
      events.push("authorize");
      await authorization;
      events.push("cancel-response");
      if (isActive()) lateCommit += 1;
    },
    discard: async () => events.push("discard")
  });

  const result = await resultPromise;
  assert.equal(result.timedOut, true);
  assert.deepEqual(events, ["authorize", "discard"]);
  release();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(events, ["authorize", "discard", "cancel-response"]);
  assert.equal(lateCommit, 0);
});

test("discard makes one cancel attempt after a 401 and then deletes local audio", async () => {
  const events = [];
  let attempts = 0;
  const result = await runBoundedDiscardCancel({
    timeoutMs: 50,
    now: () => 0,
    cancel: async () => {
      attempts += 1;
      events.push("cancel");
      throw Object.assign(new Error("expired"), { status: 401 });
    },
    discard: async () => events.push("discard")
  });
  assert.equal(result.cancelFinished, true);
  assert.equal(attempts, 1);
  assert.deepEqual(events, ["cancel", "discard"]);
});

test("a recovery joiner rechecks completion after a deferred PUT fails non-retryably", async () => {
  const put = deferred();
  const putStarted = deferred();
  const locks = new Map();
  const initial = session({
    capture: { status: "recording", captureComplete: false },
    upload: { state: "receiving", nextSequence: 2 }
  });
  let current = initial;
  const starts = [];
  const getSession = async () => current;
  const startUpload = async (sessionId, source) => {
    starts.push({ sessionId, source });
    return { sessionId, source };
  };
  const owner = runLockedUploadPass({
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    now: () => NOW,
    operation: async () => {
      const stale = await getSession();
      assert.equal(decideUpload(stale, NOW, { source: "status" }).action, "upload_chunks");
      putStarted.resolve();
      try {
        await put.promise;
      } catch {
        const failureSnapshot = await getSession();
        const failure = classifyUploadFailure(failureSnapshot, "upload_chunks", {
          retryable: false,
          cancelRetryable: false,
          uploadState: "failed"
        }, NOW);
        current = {
          ...failureSnapshot,
          upload: { ...failureSnapshot.upload, ...failure.patch }
        };
        assert.equal(failure.recheck, false);
        return { session: current, recheck: failure.recheck };
      }
      return { session: stale, recheck: false };
    }
  });
  await putStarted.promise;
  const joiner = runLockedUploadPass({
    sessionId: "client-1",
    source: "recovery",
    locks,
    getSession,
    startUpload,
    now: () => NOW,
    operation: () => assert.fail("the recovery caller must join the owner pass")
  });

  current = session({
    capture: { status: "complete", captureComplete: true },
    upload: { state: "receiving", nextSequence: 2 }
  });
  put.reject(new Error("409"));
  await Promise.all([owner, joiner]);

  assert.deepEqual(starts, [{ sessionId: "client-1", source: "recovery" }]);
});

test("a status joiner rechecks an interruption after a deferred PUT failure snapshot", async () => {
  const put = deferred();
  const putStarted = deferred();
  const failureClassified = deferred();
  const releaseOwner = deferred();
  const locks = new Map();
  const initial = session({
    capture: { status: "recording", captureComplete: false, interrupted: false },
    upload: { state: "receiving", nextSequence: 2, cancelError: null, cancelSettled: false }
  });
  let current = initial;
  let classifiedFailure;
  const starts = [];
  let followUpDecision;
  const getSession = async () => current;
  const startUpload = async (sessionId, source) => {
    followUpDecision = decideUpload(current, NOW, { source });
    starts.push({ sessionId, source });
    return { sessionId, source };
  };
  const owner = runLockedUploadPass({
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    now: () => NOW,
    operation: async () => {
      const stale = await getSession();
      assert.equal(decideUpload(stale, NOW, { source: "status" }).action, "upload_chunks");
      putStarted.resolve();
      try {
        await put.promise;
      } catch {
        const failureSnapshot = await getSession();
        classifiedFailure = classifyUploadFailure(failureSnapshot, "upload_chunks", {
          retryable: false,
          cancelRetryable: false,
          uploadState: "failed"
        }, NOW);
        current = {
          ...failureSnapshot,
          upload: { ...failureSnapshot.upload, ...classifiedFailure.patch }
        };
        failureClassified.resolve();
        await releaseOwner.promise;
        return { session: current, recheck: classifiedFailure.recheck };
      }
      return { session: stale, recheck: false };
    }
  });
  await putStarted.promise;
  const joiner = runLockedUploadPass({
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    now: () => NOW,
    operation: () => assert.fail("the status caller must join the owner pass")
  });

  put.reject(new Error("409"));
  await failureClassified.promise;
  assert.equal(classifiedFailure.recheck, false);
  current = {
    ...current,
    capture: { ...current.capture, status: "interrupted", interrupted: true }
  };
  releaseOwner.resolve();
  await Promise.all([owner, joiner]);

  assert.deepEqual(followUpDecision, {
    action: "cancel",
    serverSessionId: "server-1",
    epoch: 1
  });
  assert.deepEqual(starts, [{ sessionId: "client-1", source: "status" }]);
});

test("several status joiners do not retry a non-retryable healthy failure", async () => {
  const put = deferred();
  const putStarted = deferred();
  const locks = new Map();
  const initial = session({
    capture: { status: "recording", captureComplete: false },
    upload: { state: "receiving", nextSequence: 2 }
  });
  let current = initial;
  const starts = [];
  let wakeRequests = 0;
  const getSession = async () => current;
  const startUpload = async (sessionId, source) => {
    starts.push({ sessionId, source });
    return { sessionId, source };
  };
  const pass = {
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    onWait: async () => { wakeRequests += 1; },
    now: () => NOW,
    operation: async () => {
      const stale = await getSession();
      assert.equal(decideUpload(stale, NOW, { source: "status" }).action, "upload_chunks");
      putStarted.resolve();
      try {
        await put.promise;
      } catch {
        const failureSnapshot = await getSession();
        const failure = classifyUploadFailure(failureSnapshot, "upload_chunks", {
          retryable: false,
          cancelRetryable: false,
          uploadState: "failed"
        }, NOW);
        current = {
          ...failureSnapshot,
          upload: { ...failureSnapshot.upload, ...failure.patch }
        };
        return { session: current, recheck: failure.recheck };
      }
      return { session: stale, recheck: false };
    }
  };
  const owner = runLockedUploadPass(pass);
  await putStarted.promise;
  const joiners = [1, 2, 3].map(() => runLockedUploadPass({ ...pass, operation: () => assert.fail("status caller must join the owner pass") }));
  put.reject(new Error("409"));
  await Promise.all([owner, ...joiners]);

  assert.deepEqual(starts, []);
  assert.equal(wakeRequests, 0);
});

test("status joiners request a wake alarm instead of retrying before a future backoff", async () => {
  const put = deferred();
  const putStarted = deferred();
  const locks = new Map();
  const initial = session({
    capture: { status: "recording", captureComplete: false },
    upload: { state: "receiving", nextSequence: 2 }
  });
  let current = initial;
  const starts = [];
  const wakeRequests = [];
  const getSession = async () => current;
  const startUpload = async (sessionId, source) => {
    starts.push({ sessionId, source });
    return { sessionId, source };
  };
  const pass = {
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    onWait: async (decision) => { wakeRequests.push(decision); },
    now: () => NOW,
    operation: async () => {
      const stale = await getSession();
      assert.equal(decideUpload(stale, NOW, { source: "status" }).action, "upload_chunks");
      putStarted.resolve();
      try {
        await put.promise;
      } catch {
        const failureSnapshot = await getSession();
        const failure = classifyUploadFailure(failureSnapshot, "upload_chunks", {
          retryable: true,
          cancelRetryable: false,
          uploadState: "retryable"
        }, NOW);
        current = {
          ...failureSnapshot,
          upload: { ...failureSnapshot.upload, ...failure.patch }
        };
        return { session: current, recheck: failure.recheck };
      }
      return { session: stale, recheck: false };
    }
  };
  const owner = runLockedUploadPass(pass);
  await putStarted.promise;
  const joiners = [1, 2, 3].map(() => runLockedUploadPass({ ...pass, operation: () => assert.fail("status caller must join the owner pass") }));
  put.reject(new Error("503"));
  await Promise.all([owner, ...joiners]);

  assert.deepEqual(starts, []);
  assert.ok(wakeRequests.length > 0);
  assert.ok(wakeRequests.every((decision) => decision.retryAt === NOW + UPLOAD_RETRY_BACKOFF_MS));
});

test("a joiner rechecks durable state with its own source when the joined pass rejects", async () => {
  const gate = deferred();
  const started = deferred();
  const locks = new Map();
  let current = session({
    capture: { status: "recording", captureComplete: false },
    upload: { state: "receiving", nextSequence: 2 }
  });
  const starts = [];
  const getSession = async () => current;
  const startUpload = async (sessionId, source) => {
    starts.push({ sessionId, source });
    return { sessionId, source };
  };
  const pass = {
    sessionId: "client-1",
    source: "status",
    locks,
    getSession,
    startUpload,
    onWait: async () => {},
    now: () => NOW
  };
  const owner = runLockedUploadPass({
    ...pass,
    operation: async () => {
      started.resolve();
      await gate.promise;
    }
  });
  await started.promise;
  const joiner = runLockedUploadPass({
    ...pass,
    source: "recovery",
    operation: () => assert.fail("recovery caller must join the owner pass")
  });
  current = session({
    capture: { status: "complete", captureComplete: true },
    upload: { state: "receiving", nextSequence: 2 }
  });
  gate.reject(new Error("storage unavailable"));

  await assert.rejects(owner, /storage unavailable/);
  assert.deepEqual(await joiner, { sessionId: "client-1", source: "recovery" });
  assert.deepEqual(starts, [{ sessionId: "client-1", source: "recovery" }]);
  assert.equal(locks.size, 0);
});
