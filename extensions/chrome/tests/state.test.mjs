import assert from "node:assert/strict";
import test from "node:test";

import { visibleSessions } from "../control-core.js";
import { capturePresentation, initialState, reduce } from "../popup-core.js";

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
  assert.equal(capturePresentation(base).label, "Đã gửi xong");
  assert.equal(
    capturePresentation(status("epoch-a", 1, [done], null, "https://other.example.test")).label,
    "Sẵn sàng"
  );
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
