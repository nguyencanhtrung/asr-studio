import assert from "node:assert/strict";
import test from "node:test";

import { initialState as controlState, reduce as controlReduce, sessionsRenderKey, visibleSessions } from "../control-core.js";
import { capturePresentation, initialState, pendingRecovery, reduce } from "../popup-core.js";

const status = (epoch, sequence, sessions = [], capture = null, webUrl = "https://asr.example.test") => ({
  statusEpoch: epoch,
  statusSequence: sequence,
  connection: { paired: true, serverUrl: "https://asr.example.test", webUrl, backendId: "backend-a" },
  micReady: true,
  capture,
  sessions,
  message: "",
  prototype: false
});

test("status reducers ignore stale responses and retired worker epochs", () => {
  let state = reduce(initialState, { type: "request-started", kind: "status", requestId: "status:1" });
  state = reduce(state, { type: "status-received", requestId: "status:1", status: status("epoch-a", 2) });
  state = reduce(state, { type: "request-finished", kind: "status", requestId: "status:1" });
  state = reduce(state, { type: "status-received", status: status("epoch-a", 1) });
  assert.equal(state.statusSequence, 2);
  state = reduce(state, { type: "status-received", status: status("epoch-b", 1) });
  state = reduce(state, { type: "status-received", status: status("epoch-a", 3) });
  assert.equal(state.statusEpoch, "epoch-b");
});

test("history is newest first, physically discarded rows stay hidden, and snapshots stay immutable", () => {
  const sessions = [
    { id: "older", capture: { startedAt: 100, status: "complete" }, upload: {} },
    { id: "discarded", capture: { startedAt: 500, status: "discarded" }, upload: { state: "discarded" } },
    { id: "newer", capture: { startedAt: 300, status: "complete" }, upload: {} },
    { id: "interrupted", capture: { startedAt: 200, status: "interrupted" }, upload: {} }
  ];
  const before = structuredClone(sessions);
  assert.deepEqual(visibleSessions(sessions).map((session) => session.id), ["newer", "interrupted", "older"]);
  assert.deepEqual(sessions, before);
  assert.deepEqual(visibleSessions([sessions[1]]), []);
});

test("receipt presentation uses the current API and web binding", () => {
  const done = {
    id: "done",
    endpoint: { serverUrl: "https://asr.example.test", webUrl: "https://asr.example.test", backendId: "backend-a" },
    capture: { sessionId: "done", startedAt: 100, status: "complete" },
    upload: { state: "uploaded", receipt: { recording_id: "server-session" }, cleaned: true }
  };
  const base = status("epoch-a", 1, [done]);
  assert.equal(capturePresentation(base).label, "Server đã lưu audio thô");
  assert.equal(capturePresentation(base).detail, "Server đã lưu audio thô.");
  const processing = { ...done, upload: { ...done.upload, meetingUrl: "https://asr.example.test/meetings/server-session" } };
  assert.equal(
    capturePresentation(status("epoch-a", 1, [processing])).detail,
    "Xem trạng thái xử lý trong ASR Studio."
  );
  const interrupted = {
    id: "interrupted-receipt",
    endpoint: done.endpoint,
    capture: { sessionId: "interrupted-receipt", startedAt: 90, status: "interrupted" },
    upload: { state: "uploaded", recoveryChoice: "save", receipt: { recording_id: "server-session" }, cleaned: true }
  };
  assert.equal(
    capturePresentation(status("epoch-a", 1, [interrupted], interrupted.capture)).label,
    "Server đã lưu phần audio thô"
  );
  assert.equal(
    capturePresentation(status("epoch-a", 1, [interrupted], interrupted.capture)).detail,
    "Server đã lưu phần audio thô."
  );
  const interruptedProcessing = {
    ...interrupted,
    upload: { ...interrupted.upload, meetingUrl: "https://asr.example.test/meetings/server-session" }
  };
  assert.equal(
    capturePresentation(status("epoch-a", 1, [interruptedProcessing], interrupted.capture)).detail,
    "Xem trạng thái xử lý trong ASR Studio."
  );
  assert.equal(
    capturePresentation(status("epoch-a", 1, [done], null, "https://other.example.test")).label,
    "Sẵn sàng"
  );
});

test("stopping presentation names capture completion without claiming a save", () => {
  const capture = { sessionId: "stopping", startedAt: 100, status: "stopping" };
  const session = {
    id: "stopping",
    endpoint: { serverUrl: "https://asr.example.test", webUrl: "https://asr.example.test", backendId: "backend-a" },
    capture,
    upload: { state: "waiting_for_stop", cleaned: false, receipt: null }
  };
  const presentation = capturePresentation(status("epoch-a", 1, [session], capture));
  assert.equal(presentation.label, "Đang dừng ghi");
  assert.equal(presentation.detail, "Đang hoàn tất việc ghi âm.");
});

test("recording presentation names live upload in both active UI states", () => {
  const capture = { sessionId: "live", startedAt: 100, status: "recording" };
  const live = {
    id: "live",
    endpoint: { serverUrl: "https://asr.example.test", webUrl: "https://asr.example.test", backendId: "backend-a" },
    capture,
    upload: { state: "receiving", cleaned: false, receipt: null }
  };
  const presentation = capturePresentation(status("epoch-a", 1, [live], capture));
  assert.equal(presentation.label, "Đang ghi âm");
  assert.match(presentation.detail, /Đang gửi audio/);
});

test("a pending request rebuilds the control rows so competing choices stay disabled", () => {
  const row = { id: "a", capture: { startedAt: 1 }, upload: { state: "receiving" } };
  const idle = controlReduce(controlState, { type: "status-received", status: status("epoch-a", 1, [row]) });
  const pending = controlReduce(idle, { type: "request-started", kind: "save-partial", requestId: "save-partial:1" });
  assert.notEqual(sessionsRenderKey(idle), sessionsRenderKey(pending));
  const finished = controlReduce(pending, { type: "request-finished", kind: "save-partial", requestId: "save-partial:1" });
  assert.equal(sessionsRenderKey(finished), sessionsRenderKey(idle));
});

test("a failed Cancel points to the retry instead of the interrupted choice", () => {
  const failed = {
    id: "a",
    endpoint: { serverUrl: "https://asr.example.test", webUrl: "https://asr.example.test", backendId: "backend-a" },
    capture: { sessionId: "a", status: "interrupted", chunkCount: 0, startedAt: 1 },
    upload: { recoveryChoice: "cancel", awaitingChoice: false, cancelSettled: false, cleaned: false, error: { code: "host_permission" } }
  };
  const snapshot = status("epoch-a", 1, [failed], failed.capture);
  assert.equal(capturePresentation(snapshot).label, "Chưa hủy xong");
  failed.upload.error = null;
  assert.equal(capturePresentation(snapshot).label, "Bị gián đoạn");
});

test("the popup keeps a pending recovery visible after a failed same-server re-pair", () => {
  const pending = {
    id: "a",
    endpoint: { serverUrl: "https://asr.example.test", webUrl: "https://asr.example.test", backendId: "backend-a" },
    capture: { sessionId: "a", status: "interrupted", chunkCount: 3, startedAt: 1 },
    upload: { awaitingChoice: true }
  };
  const snapshot = status("epoch-a", 1, [pending], pending.capture);
  snapshot.connection = { paired: false, serverUrl: "https://asr.example.test" };
  assert.equal(pendingRecovery(snapshot)?.id, "a");
  snapshot.connection = { paired: false, serverUrl: "https://other.example.test" };
  assert.equal(pendingRecovery(snapshot), null);
  snapshot.connection = { paired: true, serverUrl: "https://asr.example.test", backendId: "backend-b" };
  assert.equal(pendingRecovery(snapshot), null);
});
