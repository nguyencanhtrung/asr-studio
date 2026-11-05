import assert from "node:assert/strict";
import test from "node:test";

import { initialState as historyInitial, reduce as reduceHistory } from "../../src/asr_studio/web/history-core.js";
import { combine, createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  POLL_HIDDEN_MS,
  POLL_RETRY_MS,
  POLL_VISIBLE_MS,
  autoStartStorageKey,
  displayedLanguage,
  initialState,
  reduce,
  requestStorageKey,
  selectRequestBusy,
  selectRuntime,
  selectTask,
} from "../../src/asr_studio/web/task-core.js";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (status, body) => ({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) });
const text = (message) => {
  if (message === null || message === undefined) return "";
  if (typeof message !== "object") return String(message);
  if (Array.isArray(message)) return message.map(text).join("");
  let out = message.text;
  for (const [name, value] of Object.entries(message.params ?? {})) out = out.replaceAll(`{${name}}`, text(value));
  return out;
};

const task = (id, status, extra = {}) => ({ id, request_id: `r-${id}`, status, language_hint: "en", stage: "transcribing", ...extra });
const meeting = (id, extra = {}, transcription = {}) => ({
  id,
  title: `Meeting ${id}`,
  metadata_version: 1,
  meeting_date: "2025-03-04",
  language_hint: "en",
  notes_source_locked: false,
  transcription: { generation: null, task: null, ...transcription },
  ...extra,
});
const GENERATION = { id: "g1", generation_number: 1, source_version: 1, status: "complete", language_hint: "vi", segments: [], speakers: [] };

// The page as the view wires it: the real store and runner over fakes, the facts of the meeting on screen stamped on
// every action, and the two bridges the view makes from history revisions (`left.rev` -> left, `readyRev` -> ready).
function setup(path = "/meetings/m1") {
  const requests = [];
  const timers = [];
  const calls = [];
  const navigated = [];
  const stored = new Map();
  const address = { pathname: path, search: "", hash: "", tab: null };
  let nextId = 0;
  const run = createRunner({
    fetch: (url, init) =>
      new Promise((resolve, reject) => {
        requests.push({ url, method: init.method, body: init.body && JSON.parse(init.body), resolve, reject, done: false, signal: init.signal });
      }),
    setTimeout: (callback, delay) => timers.push({ callback, delay, live: true }) - 1,
    clearTimeout: (id) => {
      timers[id].live = false;
    },
    navigate: (url) => navigated.push(url),
    storage: { session: () => ({ setItem: (key, value) => stored.set(key, value), removeItem: (key) => stored.delete(key), getItem: (key) => stored.get(key) ?? null }) },
    calls: new Proxy({}, { get: (_, name) => (...args) => calls.push([name, ...args]) }),
  });
  let store;
  const shown = () => {
    const state = store.getState().history;
    const current = state.meeting;
    if (!current) return null;
    const transcription = current.transcription || {};
    const generation = transcription.generation || null;
    return {
      id: current.id,
      language: current.language_hint,
      asrModels: current.transcription?.asr_models ?? [],
      task: transcription.task || null,
      generation: Boolean(generation),
      generationLanguage: generation ? generation.language_hint : null,
      blocked: !state.notes.lockKnown || state.notes.locked,
    };
  };
  const stamp = (action) => ({ ...action, at: { ...address }, shown: shown() });
  store = createStore({
    reduce: combine({ history: reduceHistory, task: reduce }),
    initialState: { history: historyInitial, task: initialState },
    run: (effect, send) => run(effect, (action) => send(stamp(action))),
  });
  const send = (type, extra = {}) => store.dispatch(stamp({ type, ...extra }));
  let seen = store.getState().history;
  store.subscribe((root) => {
    const was = seen;
    seen = root.history;
    if (seen.left.rev !== was.left.rev) send("task/meeting-left");
    if (seen.readyRev !== was.readyRev) {
      const id = seen.meeting.id;
      send("task/meeting-ready", { requestId: `new-${nextId += 1}`, stored: stored.get(requestStorageKey(id)) ?? null, autoStart: stored.get(autoStartStorageKey(id)) ?? null });
    }
  });
  const open = (match) => (r) => !r.done && !r.signal.aborted && match(r);
  const is = (method, url) => (r) => r.method === method && r.url === url;
  const answer = async (match, status, body) => {
    const request = requests.find(open(match));
    assert.ok(request, "no pending request");
    request.done = true;
    request.resolve(reply(status, body));
    await tick();
    return request;
  };
  const fail = async (match) => {
    const request = requests.find(open(match));
    assert.ok(request, "no pending request");
    request.done = true;
    request.reject(new TypeError("offline"));
    await tick();
  };
  const fire = (delay) => {
    const timer = timers.find((item) => item.live && item.delay === delay);
    assert.ok(timer, `no live timer of ${delay} ms`);
    timer.live = false;
    timer.callback();
  };
  return {
    store,
    task: () => store.getState().task,
    history: () => store.getState().history,
    view: () => selectTask(store.getState().task, shown()),
    send,
    address,
    requests,
    answer,
    fail,
    fire,
    stored,
    calls,
    navigated,
    is,
    waiting: (match) => requests.filter(open(match)),
    live: () => timers.filter((item) => item.live && ![120, 4000].includes(item.delay)).map((item) => item.delay), // the task timers; not history's search and tab timers
    sent: (url) => requests.filter((r) => r.method === "POST" && r.url === url),
    shown,
  };
}

const GET_MEETING = (id) => (r) => r.method === "GET" && r.url === `/api/meetings/${id}`;
const GET_TASK = (id) => (r) => r.method === "GET" && r.url === `/api/tasks/${id}`;

// Open a meeting the way the page does: boot at its address, answer the list and the meeting.
async function opened(body, { path = "/meetings/m1", stored = {}, lock = true } = {}) {
  const env = setup(path);
  for (const [key, value] of Object.entries(stored)) env.stored.set(key, value);
  env.send("task/visibility-changed", { hidden: false });
  env.send("history/booted", { today: "2025-03-04" });
  await env.answer(env.is("GET", "/api/meetings"), 200, { meetings: [], recent: [], total: 0 });
  if (lock) env.send("history/notes-source-lock-reported", { meetingId: body.id, locked: false, known: true });
  await env.answer(GET_MEETING(body.id), 200, body);
  return env;
}

// ---- following a running task ----

test("a running task is polled every second while the page is visible and the answer replaces the task", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
  assert.equal(env.view().active, true);

  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running", { segment_count: 3 }));
  assert.equal(env.history().meeting.transcription.task.segment_count, 3);
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
  assert.equal(env.task().poll.phase, "waiting");
});

test("the poll stops when the task ends, retires the pending request id and reads the finished meeting", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "r-t1", languageHint: "en" }) } });
  assert.equal(env.task().pending.requestId, "r-t1");
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "succeeded"));
  assert.equal(env.task().poll, null);
  assert.equal(env.task().pending, null);
  assert.equal(env.stored.has(requestStorageKey("m1")), false);
  assert.deepEqual(env.live(), []);

  const generation = { ...GENERATION, id: "g2" };
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t1", "succeeded"), generation }));
  assert.equal(env.history().meeting.transcription.generation.id, "g2");
  assert.equal(env.history().installKind, "refresh");
});

test("a task that failed is not polled and its error and diagnostic command are offered", async () => {
  const id = "a".repeat(32);
  const failed = task(id, "failed", { error_code: "publication_failed", error_message: "raw" });
  const env = await opened(meeting("m1", {}, { task: failed }));
  assert.deepEqual(env.live(), []);
  const { error } = env.view();
  assert.equal(error.visible, true);
  assert.match(text(error.message), /Không thể lưu transcript hoàn chỉnh/);
  assert.equal(error.diagnostic.command, `docker compose exec -T app python -m asr_studio.cli task diagnose ${id}`);
  assert.equal(error.diagnostic.id, id);
});

test("persisted transcription failures use localized codes instead of raw worker text", () => {
  const view = (code, error_message) => selectTask(initialState, {
    id: "m1",
    language: "vi",
    generation: false,
    generationLanguage: null,
    blocked: false,
    task: task("b".repeat(32), "failed", { error_code: code, error_message }),
  });
  assert.equal(text(view("transcription_failed", "Local transcription failed. Check the worker logs and try again.").error.message), "Chép lời local thất bại. Kiểm tra log của worker rồi thử lại.");
  assert.equal(text(view("runner_protocol_error", "The local transcription runner did not return a complete result.").error.message), "Runner chép lời local không trả về kết quả hoàn chỉnh.");
  assert.equal(text(view("worker_unavailable", "The processing worker is unavailable.").error.message), "Worker chép lời chưa sẵn sàng.");
});

test("an unknown error code uses a safe fallback and no diagnostic; an unsafe task id gets no command", () => {
  const view = (shown) => selectTask(initialState, shown);
  const base = { id: "m1", language: "en", generation: false, generationLanguage: null, blocked: false };
  const unknown = view({ ...base, task: task("b".repeat(32), "failed", { error_code: "odd", error_message: "Safe fallback." }) });
  assert.equal(text(unknown.error.message), "Tác vụ chưa hoàn tất.");
  assert.equal(unknown.error.diagnostic, null);
  const unsafe = view({ ...base, task: task("../x", "failed", { error_code: "publication_failed" }) });
  assert.equal(unsafe.error.diagnostic, null);
  assert.equal(text(view({ ...base, task: task("c".repeat(32), "interrupted") }).error.message), "Tác vụ chưa hoàn tất.");
  assert.equal(view({ ...base, task: task("d".repeat(32), "succeeded", { error_code: "publication_failed" }) }).error.visible, false);
});

// ---- stale and out-of-order answers ----

test("a poll answer that arrives after the user left the meeting is dropped", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  const [pending] = env.waiting(GET_TASK("t1"));
  env.send("history/home-clicked");
  assert.equal(env.task().poll, null);
  assert.equal(pending.signal.aborted, true);
  pending.done = true;
  pending.resolve(reply(200, task("t1", "succeeded")));
  await tick();
  assert.equal(env.history().meeting.transcription.task.status, "running");
  assert.deepEqual(env.live(), []);
});

test("opening another meeting stops the poll of the first; the answer for it changes nothing", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  env.address.pathname = "/meetings/m2";
  env.send("history/meeting-link-clicked", { meetingId: "m2" });
  assert.equal(env.task().poll, null);
  await env.answer(GET_MEETING("m2"), 200, meeting("m2"));
  assert.equal(env.history().meeting.id, "m2");
  assert.equal(env.history().meeting.transcription.task, null);
  assert.deepEqual(env.live(), []);
});

test("an answer for a task that is no longer the one followed is ignored by the reducer", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  const before = env.task();
  const stale = { type: "task/poll-loaded", meta: { meetingId: "m1", taskId: "t0" }, response: { status: 200, body: task("t0", "succeeded") }, shown: env.shown() };
  assert.equal(reduce(before, stale).state, before);
  const wrongMeeting = { ...stale, meta: { meetingId: "m1", taskId: "t1" }, shown: { ...env.shown(), id: "m9" } };
  assert.equal(reduce(before, wrongMeeting).state.poll, null); // the answer is dropped and the poll ends
  const early = { ...stale, meta: { meetingId: "m1", taskId: "t1" } };
  assert.equal(reduce(initialState, early).state, initialState); // no poll at all
});

test("a poll timer that fires for a meeting no longer on screen ends the poll without a request", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  const state = env.task();
  const due = { type: "task/poll-due", meetingId: "m1", taskId: "t1", shown: { ...env.shown(), id: "m2" } };
  const result = reduce(state, due);
  assert.equal(result.state.poll, null);
  assert.deepEqual(result.effects, []);
  const wrongTask = reduce(state, { ...due, taskId: "t9", shown: env.shown() });
  assert.equal(wrongTask.state, state);
});

test("a poll answer with a body that is not a task counts as a failed read", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, null);
  assert.equal(env.view().notice, "poll-lost");
  assert.deepEqual(env.live(), [POLL_RETRY_MS]);
  assert.equal(env.history().meeting.transcription.task.status, "running");
});

// ---- network errors ----

test("a failed poll read keeps the task, shows the lost-connection line and retries after three seconds", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.fail(GET_TASK("t1"));
  assert.equal(env.view().notice, "poll-lost");
  assert.equal(env.history().meeting.transcription.task.status, "running");
  assert.deepEqual(env.live(), [POLL_RETRY_MS]);

  env.fire(POLL_RETRY_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running"));
  assert.equal(env.view().notice, null);
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
});

test("the exact workspace 401 ends the session, while a structured provider 401 stays a poll loss", async () => {
  const workspace = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  workspace.fire(POLL_VISIBLE_MS);
  await workspace.answer(GET_TASK("t1"), 401, { detail: "Authentication required" });
  assert.equal(workspace.view().notice, "session-ended");
  assert.equal(workspace.task().notice.kind, "session-ended");
  assert.equal(workspace.task().poll, null);
  assert.deepEqual(workspace.live(), []);
  assert.deepEqual(workspace.navigated, ["/access"]);

  const provider = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  provider.fire(POLL_VISIBLE_MS);
  await provider.answer(GET_TASK("t1"), 401, { detail: { code: "provider_auth_error" } });
  assert.equal(provider.view().notice, "poll-lost");
  assert.deepEqual(provider.live(), [POLL_RETRY_MS]);
  assert.deepEqual(provider.navigated, []);
});

test("a failed poll read after the poll was stopped changes nothing", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  const before = env.task();
  const lost = reduce({ ...before, poll: null }, { type: "task/poll-failed", meta: { meetingId: "m1", taskId: "t1" }, error: { message: "x" } });
  assert.equal(lost.state.poll, null);
  assert.deepEqual(lost.effects, []);
});

test("the finished meeting that cannot be read leaves a line saying so and the poll stays stopped", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "succeeded"));
  await env.fail(GET_MEETING("m1"));
  assert.equal(env.view().notice, "refresh-lost");
  assert.equal(env.task().poll, null);
  assert.equal(selectRequestBusy(env.task()), false);
});

test("a start clicked while the finished task's meeting is being read again stays busy when that read lands", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "succeeded")); // the poll reads the meeting again
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(selectRequestBusy(env.task()), true);
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t1", "succeeded") }));
  assert.equal(selectRequestBusy(env.task()), true); // only the start answer ends the request
});

test("a new start clears the line about the earlier run, also when it is refused", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "succeeded"));
  await env.fail(GET_MEETING("m1"));
  assert.equal(env.view().notice, "refresh-lost");
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: { code: "system_busy", message: "Busy." } });
  assert.equal(env.view().notice, null);
});

test("opening the same meeting again clears the line about it and repaints", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "succeeded"));
  await env.fail(GET_MEETING("m1"));
  const rev = env.task().rev;
  env.send("history/meeting-link-clicked", { meetingId: "m1" });
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t1", "succeeded") }));
  assert.equal(env.view().notice, null);
  assert.ok(env.task().rev > rev); // the old page rewrote the status line on every load
});

test("a start that cannot reach the server keeps the request id and asks to verify with it", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  assert.deepEqual(env.sent("/api/tasks")[0].body, { meeting_id: "m1", request_id: "q1", language_hint: "en", asr_model: "auto" });
  assert.equal(selectRequestBusy(env.task()), true);
  await env.fail((r) => r.url === "/api/tasks");

  assert.equal(selectRequestBusy(env.task()), false);
  assert.match(text(env.view().error.message), /Bấm lại để xác minh bằng cùng request ID, ngôn ngữ gợi ý và model\.$/);
  assert.equal(env.view().notice, null);
  assert.equal(env.task().pending.requestId, "q1");
  assert.ok(env.stored.has(requestStorageKey("m1")));
  assert.equal(env.view().language.disabled, true);

  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.sent("/api/tasks")[1].body.request_id, "q1"); // the same id, never a new one while unverified
});

test("a start the server refuses (4xx) drops the request id and offers a new request", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: { code: "system_busy", message: "Busy." } });
  assert.equal(env.task().pending, null);
  assert.equal(env.stored.has(requestStorageKey("m1")), false);
  assert.equal(text(env.view().error.message), "Máy đang bận vì một tác vụ khác đang chạy. Bấm lại để gửi một yêu cầu mới.");

  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.sent("/api/tasks")[1].body.request_id, "q2");
});

test("a server error (5xx) is not definitive: the request id is kept", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 503, {});
  assert.equal(env.task().pending.requestId, "q1");
});

test("a refused automatic start names the reason; a manual one shows the server text", async () => {
  const env = await opened(meeting("m1"), { stored: { [autoStartStorageKey("m1")]: "1" } });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: { code: "worker_unavailable", message: "x" } });
  assert.match(text(env.view().error.message), /^Worker chép lời chưa sẵn sàng\. Cuộc họp đã được lưu nhưng chưa chép lời\./);

  env.send("task/start-clicked", { requestId: "q2" });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: { code: "worker_unavailable", message: "Server text." } });
  assert.equal(text(env.view().error.message), "Worker chép lời chưa sẵn sàng. Bấm lại để gửi một yêu cầu mới.");
});

test("a start answer that is not a task is a failed start that can be verified", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 200, null);
  assert.equal(selectRequestBusy(env.task()), false);
  assert.equal(text(env.view().error.message), "Không thể xác nhận yêu cầu. Bấm lại để xác minh bằng cùng request ID, ngôn ngữ gợi ý và model.");
  assert.equal(env.task().pending.requestId, "q1");
});

test("a start that was accepted shows the task, forgets the request id and follows the task", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 202, task("t1", "accepted", { request_id: "q1" }));
  assert.equal(env.history().meeting.transcription.task.id, "t1");
  assert.equal(env.task().pending, null);
  assert.equal(env.stored.has(requestStorageKey("m1")), false);
  assert.equal(env.task().error, null);
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
  assert.equal(env.view().active, true);
});

test("a start that answers an already finished task reads the meeting before the request counts as done", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 200, task("t1", "succeeded", { request_id: "q1" }));
  assert.equal(selectRequestBusy(env.task()), true);
  assert.deepEqual(env.live(), []);
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t1", "succeeded"), generation: GENERATION }));
  assert.equal(selectRequestBusy(env.task()), false);
  assert.equal(env.history().meeting.transcription.generation.id, "g1");
});

test("a start whose meeting can no longer be read is reported as an unconfirmed request", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 200, task("t1", "succeeded", { request_id: "q1" }));
  await env.answer(GET_MEETING("m1"), 404, { detail: "gone" });
  assert.equal(selectRequestBusy(env.task()), false);
  assert.match(text(env.view().error.message), /Bấm lại để gửi một yêu cầu mới\.$/);
});

test("a start answered after the user left only retires the request id", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  env.address.pathname = "/meetings/m2";
  env.send("history/meeting-link-clicked", { meetingId: "m2" });
  await env.answer(GET_MEETING("m2"), 200, meeting("m2"));
  assert.equal(selectRequestBusy(env.task()), true); // the request is still on the wire
  assert.equal(env.view().requestBusy, true);

  await env.answer((r) => r.url === "/api/tasks", 202, task("t1", "accepted", { request_id: "q1" }));
  assert.equal(selectRequestBusy(env.task()), false);
  assert.equal(env.task().pending, null);
  assert.equal(env.history().meeting.id, "m2");
  assert.equal(env.history().meeting.transcription.task, null);
  assert.deepEqual(env.live(), []);
});

// ---- busy buttons ----

test("pressing start again while a start is on the wire sends one request", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.sent("/api/tasks").length, 1);
  assert.equal(env.sent("/api/tasks")[0].body.request_id, "q1");
});

test("start is refused while the notes source lock is unknown or held", async () => {
  const unknown = await opened(meeting("m1", { notes_source_locked: undefined }), { lock: false });
  unknown.send("task/start-clicked", { requestId: "q1" });
  assert.equal(unknown.sent("/api/tasks").length, 0);
  assert.equal(selectRequestBusy(unknown.task()), false);

  const held = await opened(meeting("m1"));
  held.send("history/notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  held.send("task/start-clicked", { requestId: "q1" });
  assert.equal(held.sent("/api/tasks").length, 0);
});

test("start does nothing without a meeting on screen", () => {
  const env = setup("/");
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.requests.length, 0);
  env.send("task/cancel-clicked");
  assert.equal(env.requests.length, 0);
});

test("pressing cancel again while a cancel is on the wire sends one request; the button stays disabled", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  const rev = env.task().rev;
  env.send("task/cancel-clicked");
  assert.ok(env.task().rev > rev); // the view disables the button only on this repaint
  assert.equal(env.view().cancelDisabled, true);
  env.send("task/cancel-clicked");
  assert.equal(env.sent("/api/tasks/t1/cancel").length, 1);
  assert.deepEqual(env.sent("/api/tasks/t1/cancel")[0].body, {});
});

test("cancel is ignored when there is no running task or the task is already stopping", async () => {
  const idle = await opened(meeting("m1"));
  idle.send("task/cancel-clicked");
  assert.equal(idle.requests.filter((r) => r.method === "POST").length, 0);

  const stopping = await opened(meeting("m1", {}, { task: task("t1", "stopping") }));
  stopping.send("task/cancel-clicked");
  assert.equal(stopping.sent("/api/tasks/t1/cancel").length, 0);
  assert.equal(stopping.view().cancelDisabled, true);
  assert.equal(stopping.view().stopping, true);

  const done = await opened(meeting("m1", {}, { task: task("t1", "canceled") }));
  done.send("task/cancel-clicked");
  assert.equal(done.sent("/api/tasks/t1/cancel").length, 0);
});

// ---- cancel in the middle of a run ----

test("cancel mid-run shows the stopping task, keeps polling and ends with the cancelled task", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 202, task("t1", "stopping"));
  assert.equal(env.history().meeting.transcription.task.status, "stopping");
  assert.equal(env.view().cancelDisabled, true);
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]); // the poll chain of the running task goes on, once

  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "canceled"));
  assert.equal(env.history().meeting.transcription.task.status, "canceled");
  assert.equal(env.task().poll, null);
  assert.deepEqual(env.live(), []);
  assert.equal(env.view().active, false);
});

test("a cancel the server refuses shows its text and re-enables the button", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 409, { detail: "Too late." });
  assert.equal(text(env.view().error.message), "Dữ liệu đã thay đổi hoặc đang được sử dụng. Hãy tải lại rồi thử lại.");
  assert.equal(env.view().error.visible, true);
  assert.equal(env.view().cancelDisabled, false);
  assert.equal(env.history().meeting.transcription.task.status, "running");

  env.send("task/cancel-clicked");
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 202, task("t1", "stopping"));
  assert.equal(env.view().error.visible, false); // a good answer clears the refusal
});

test("a cancel that cannot reach the server shows the connection text", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  await env.fail((r) => r.url === "/api/tasks/t1/cancel");
  assert.equal(text(env.view().error.message), "Không kết nối được với máy chủ trên máy này.");
  assert.equal(env.view().cancelDisabled, false);
});

test("a cancel answered after the user left changes nothing on screen", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  env.address.pathname = "/meetings/m2";
  env.send("history/meeting-link-clicked", { meetingId: "m2" });
  await env.answer(GET_MEETING("m2"), 200, meeting("m2"));
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 202, task("t1", "stopping"));
  assert.equal(env.history().meeting.id, "m2");
  assert.equal(env.history().meeting.transcription.task, null);
  assert.deepEqual(env.live(), []);
  assert.deepEqual(env.task().cancelling, []);
});

test("an answer to a cancel that was never sent is ignored", () => {
  const stale = reduce(initialState, { type: "task/cancel-succeeded", meta: { meetingId: "m1", taskId: "t1" }, response: { body: task("t1", "stopping") }, shown: null });
  assert.equal(stale.state, initialState);
  const failed = reduce(initialState, { type: "task/cancel-failed", meta: { meetingId: "m1", taskId: "t1" }, error: { message: "x" } });
  assert.equal(failed.state, initialState);
});

// ---- the model chosen for one run ----

const CHOICES = [
  { id: "auto", model_name: "qwen3-asr-1.7b", ready: true, reason: null, prepare_command: null },
  { id: "whisper-large-v3", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
  { id: "pho-whisper-large", model_name: "pho-whisper-large", ready: false, reason: "not_prepared", prepare_command: "docker compose run prepare" },
  { id: "qwen3-asr-1.7b", model_name: "qwen3-asr-1.7b", ready: true, reason: null, prepare_command: null },
];
const withChoices = (extra = {}, choices = CHOICES) => meeting("m1", {}, { asr_models: choices, ...extra });

test("the selector starts on Tự động and names what the hardware default is", async () => {
  const env = await opened(withChoices());
  const { model } = env.view();
  assert.equal(model.value, "auto");
  assert.equal(text(model.note), "Theo phần cứng của máy: qwen3-asr-1.7b.");
  assert.deepEqual(model.options.map((option) => [option.id, option.ready]), CHOICES.map((choice) => [choice.id, choice.ready]));
  assert.equal(model.blocked, false);
});

test("no list from the server, no selector: a run stays on the hardware default", async () => {
  const env = await opened(meeting("m1"));
  assert.equal(env.view().model, null);
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.sent("/api/tasks")[0].body.asr_model, "auto");
});

test("the chosen model goes with this run's request and with its uncertain retry", async () => {
  const env = await opened(withChoices());
  env.send("task/model-chosen", { model: "qwen3-asr-1.7b" });
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.sent("/api/tasks")[0].body.asr_model, "qwen3-asr-1.7b");
  assert.equal(JSON.parse(env.stored.get(requestStorageKey("m1"))).asrModel, "qwen3-asr-1.7b");
  assert.equal(env.view().model.value, "qwen3-asr-1.7b");
  assert.equal(env.view().model.disabled, true);
});

test("a restored request keeps its model; a record from an older page means Tự động", async () => {
  const kept = JSON.stringify({ requestId: "k", languageHint: "en", asrModel: "qwen3-asr-1.7b" });
  const restored = await opened(withChoices(), { stored: { [requestStorageKey("m1")]: kept } });
  assert.equal(restored.view().model.value, "qwen3-asr-1.7b");
  restored.send("task/start-clicked", { requestId: "fresh" });
  assert.equal(restored.sent("/api/tasks")[0].body.asr_model, "qwen3-asr-1.7b");

  const older = JSON.stringify({ requestId: "k", languageHint: "en" });
  assert.equal((await opened(withChoices(), { stored: { [requestStorageKey("m1")]: older } })).view().model.value, "auto");
  const forged = JSON.stringify({ requestId: "k", languageHint: "en", asrModel: "cloud" });
  assert.equal((await opened(withChoices(), { stored: { [requestStorageKey("m1")]: forged } })).view().model.value, "auto");
});

test("a model that is not prepared cannot start and says how to prepare it", async () => {
  const env = await opened(withChoices());
  env.send("task/model-chosen", { model: "pho-whisper-large" });
  const { model } = env.view();
  assert.equal(model.blocked, true);
  assert.match(text(model.guidance), /chưa được chuẩn bị/);
  assert.equal(model.command, "docker compose run prepare");
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.sent("/api/tasks").length, 0);
  assert.equal(env.stored.has(requestStorageKey("m1")), false);
});

test("a GPU-only model on a CPU worker is refused with a reason and no command", async () => {
  const choices = CHOICES.map((choice) => choice.id === "qwen3-asr-1.7b" ? { ...choice, ready: false, reason: "gpu_required" } : choice);
  const env = await opened(withChoices({}, choices));
  env.send("task/model-chosen", { model: "qwen3-asr-1.7b" });
  assert.equal(env.view().model.blocked, true);
  assert.match(text(env.view().model.guidance), /chỉ chạy trên GPU/);
  assert.equal(env.view().model.command, "");
});

test("a worker that is down keeps the server's own refusal instead of a dead button", async () => {
  const choices = CHOICES.map((choice) => ({ ...choice, model_name: null, ready: false, reason: "worker_unavailable" }));
  const env = await opened(withChoices({}, choices));
  assert.equal(env.view().model.blocked, false);
  assert.equal(text(env.view().model.note), "Theo phần cứng của máy.");
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.sent("/api/tasks").length, 1);
});

test("PhoWhisper is described for Vietnamese and never as the choice for English or mixed content", async () => {
  const env = await opened(withChoices());
  env.send("task/model-chosen", { model: "pho-whisper-large" });
  assert.match(text(env.view().model.note), /^Tối ưu cho tiếng Việt\. Không dành cho English/);
});

test("a running task shows its own model and locks the selector", async () => {
  const env = await opened(withChoices({ task: task("t1", "running", { asr_model: "qwen3-asr-1.7b" }) }));
  assert.equal(env.view().model.value, "qwen3-asr-1.7b");
  assert.equal(env.view().model.disabled, true);
  assert.equal(env.view().model.guidance, null);
});

test("the server refusing a model unlocks the selector: no task exists, so nothing needs verifying", async () => {
  const stale = CHOICES.map((choice) => choice.id === "pho-whisper-large" ? { ...choice, ready: true, reason: null, prepare_command: null } : choice);
  const env = await opened(withChoices({}, stale));
  env.send("task/model-chosen", { model: "pho-whisper-large" });
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.view().model.disabled, true);
  await env.answer((r) => r.url === "/api/tasks", 503, { detail: { code: "model_unavailable", message: "x" } });
  assert.equal(env.task().pending, null);
  assert.equal(env.stored.has(requestStorageKey("m1")), false);
  assert.equal(env.view().model.disabled, false);
  assert.match(text(env.view().error.message), /^Model đã chọn chưa được chuẩn bị\. Tải lại trang/);

  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.sent("/api/tasks")[1].body.request_id, "q2");
  await env.answer((r) => r.url === "/api/tasks", 503, { detail: { code: "model_requires_gpu", message: "x" } });
  assert.equal(env.view().model.disabled, false);
  assert.equal(text(env.view().error.message), "Model đã chọn chỉ chạy trên GPU NVIDIA. Hãy chọn model khác.");
});

for (const code of ["worker_unavailable", "processing_unavailable", "diarization_model_unavailable"]) {
  test(`a ${code} refusal created no task: the choice is free again`, async () => {
    const env = await opened(withChoices());
    env.send("task/model-chosen", { model: "qwen3-asr-1.7b" });
    env.send("task/start-clicked", { requestId: "q1" });
    await env.answer((r) => r.url === "/api/tasks", 503, { detail: { code, message: "x" } });
    assert.equal(env.task().pending, null);
    assert.equal(env.view().model.disabled, false);
    assert.equal(env.view().model.value, "qwen3-asr-1.7b");
  });
}

test("a restored request for a model that became unusable can still be resolved and then unlocks", async () => {
  const kept = JSON.stringify({ requestId: "k", languageHint: "en", asrModel: "pho-whisper-large" });
  const env = await opened(withChoices(), { stored: { [requestStorageKey("m1")]: kept } });
  assert.equal(env.view().model.disabled, true);
  assert.equal(env.view().model.blocked, false);
  env.send("task/start-clicked", { requestId: "fresh" });
  assert.equal(env.sent("/api/tasks")[0].body.request_id, "k");
  await env.answer((r) => r.url === "/api/tasks", 503, { detail: { code: "model_unavailable", message: "x" } });
  assert.equal(env.view().model.disabled, false);
  assert.equal(env.view().model.blocked, true);
  assert.equal(env.view().model.command, "docker compose run prepare");
});

test("a 503 from something other than the app stays uncertain: the same id is resent", async () => {
  const env = await opened(withChoices());
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 503, { detail: { code: "gateway_said_no", message: "x" } });
  assert.equal(env.task().pending.requestId, "q1");
  assert.equal(env.view().model.disabled, true);
});

test("a choice that is not in the catalog is ignored", async () => {
  const env = await opened(withChoices());
  env.send("task/model-chosen", { model: "cloud-model" });
  assert.equal(env.view().model.value, "auto");
});

// ---- the pending request id across a page reload ----

test("a pending request is restored after a reload: language shown and locked, the same id and language resent", async () => {
  const record = JSON.stringify({ requestId: "kept", languageHint: "vi" });
  const env = await opened(meeting("m1"), { stored: { [requestStorageKey("m1")]: record } });
  assert.equal(env.task().pending.requestId, "kept");
  assert.deepEqual(env.view().language, { value: "vi", disabled: true });

  env.send("task/start-clicked", { requestId: "fresh" });
  assert.deepEqual(env.sent("/api/tasks")[0].body, { meeting_id: "m1", request_id: "kept", language_hint: "vi", asr_model: "auto" });
});

test("an older page stored the request id as plain text; it is read, kept in the meeting's language and rewritten as JSON", async () => {
  const env = await opened(meeting("m1", { language_hint: "vi" }), { stored: { [requestStorageKey("m1")]: "legacy-id" } });
  assert.deepEqual(env.task().pending, { meetingId: "m1", requestId: "legacy-id", languageHint: "vi", asrModel: "auto" });
  assert.equal(env.stored.get(requestStorageKey("m1")), JSON.stringify({ requestId: "legacy-id", languageHint: "vi", asrModel: "auto" }));
});

test("a stored record that is JSON of another kind, empty or without an id is not a pending request", async () => {
  for (const [stored, expectedId] of [['{"languageHint":"vi"}', undefined], ["", undefined], ['"quoted"', "quoted"], ["null", "null"], ["12", "12"]]) {
    const env = await opened(meeting("m1"), { stored: { [requestStorageKey("m1")]: stored } });
    assert.equal(env.task().pending?.requestId, expectedId, stored);
  }
});

test("a stored language that is not offered falls back to the meeting's language", async () => {
  const env = await opened(meeting("m1", { language_hint: "vi" }), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "k", languageHint: "fr" }) } });
  assert.equal(env.task().pending.languageHint, "vi");
});

test("the pending request of a terminal task already seen is retired; one for a newer start is kept", async () => {
  const seen = await opened(meeting("m1", {}, { task: task("t1", "failed") }), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "r-t1", languageHint: "en" }) } });
  assert.equal(seen.task().pending, null);
  assert.equal(seen.stored.has(requestStorageKey("m1")), false);

  const newer = await opened(meeting("m1", {}, { task: task("t1", "failed") }), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "newer", languageHint: "vi" }) } });
  assert.equal(newer.task().pending.requestId, "newer");
  assert.ok(newer.stored.has(requestStorageKey("m1")));
  assert.deepEqual(newer.view().language, { value: "vi", disabled: true });
});

test("the pending request is kept in memory when the storage cannot be written", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  env.stored.clear(); // as if the write never landed
  await env.fail((r) => r.url === "/api/tasks");
  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.sent("/api/tasks")[1].body.request_id, "q1");
});

test("a request pending for another meeting does not leak into this one", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.fail((r) => r.url === "/api/tasks");
  env.address.pathname = "/meetings/m2";
  env.send("history/meeting-link-clicked", { meetingId: "m2" });
  await env.answer(GET_MEETING("m2"), 200, meeting("m2", { language_hint: "vi" }));
  assert.equal(env.view().language.disabled, false);
  env.send("task/start-clicked", { requestId: "q2" });
  assert.deepEqual(env.sent("/api/tasks")[1].body, { meeting_id: "m2", request_id: "q2", language_hint: "vi", asr_model: "auto" });
});

// ---- automatic start ----

test("the create dialog's hand-off starts the transcription once and is consumed", async () => {
  const env = await opened(meeting("m1"), { stored: { [autoStartStorageKey("m1")]: "1" } });
  assert.equal(env.stored.has(autoStartStorageKey("m1")), false);
  assert.equal(env.sent("/api/tasks").length, 1);
  assert.equal(env.sent("/api/tasks")[0].body.request_id, "new-1");
  assert.equal(env.sent("/api/tasks")[0].body.asr_model, "auto");

  await env.fail((r) => r.url === "/api/tasks");
  // The meeting loads again with the hand-off still readable: the reducer starts nothing a second time.
  env.stored.set(autoStartStorageKey("m1"), "1");
  env.send("history/meeting-requested", { meetingId: "m1" });
  await env.answer(GET_MEETING("m1"), 200, meeting("m1"));
  assert.equal(env.sent("/api/tasks").length, 1);
  assert.equal(env.stored.has(autoStartStorageKey("m1")), false);
});

test("a named create-dialog hand-off selects that model for the automatic start", async () => {
  const env = await opened(withChoices(), { stored: { [autoStartStorageKey("m1")]: "qwen3-asr-1.7b" } });
  assert.equal(env.stored.has(autoStartStorageKey("m1")), false);
  assert.equal(env.sent("/api/tasks")[0].body.asr_model, "qwen3-asr-1.7b");
  assert.equal(env.view().model.value, "qwen3-asr-1.7b");
});

test("the hand-off does not start a meeting that already has a task or a transcript, and is still consumed", async () => {
  const withTask = await opened(meeting("m1", {}, { task: task("t1", "failed") }), { stored: { [autoStartStorageKey("m1")]: "1" } });
  assert.equal(withTask.sent("/api/tasks").length, 0);
  assert.equal(withTask.stored.has(autoStartStorageKey("m1")), false);

  const withTranscript = await opened(meeting("m1", {}, { generation: GENERATION }), { stored: { [autoStartStorageKey("m1")]: "1" } });
  assert.equal(withTranscript.sent("/api/tasks").length, 0);
  assert.equal(withTranscript.stored.has(autoStartStorageKey("m1")), false);
});

test("without the hand-off nothing starts by itself", async () => {
  const env = await opened(meeting("m1"));
  assert.equal(env.sent("/api/tasks").length, 0);
});

test("the hand-off is consumed but starts nothing when the notes source lock is held", async () => {
  const env = await opened(meeting("m1", { notes_source_locked: true }), { stored: { [autoStartStorageKey("m1")]: "1" } });
  assert.equal(env.sent("/api/tasks").length, 0);
  assert.equal(env.stored.has(autoStartStorageKey("m1")), false);
});

test("an automatic start uses the language the meeting shows", async () => {
  const env = await opened(meeting("m1", { language_hint: "vi" }), { stored: { [autoStartStorageKey("m1")]: "1" } });
  assert.equal(env.sent("/api/tasks")[0].body.language_hint, "vi");
});

// ---- poll cadence follows the page ----

test("a hidden page polls every three seconds, and returning to the page restarts the waiting poll at one second", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
  env.send("task/visibility-changed", { hidden: true });
  assert.deepEqual(env.live(), [POLL_HIDDEN_MS]);
  env.fire(POLL_HIDDEN_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running"));
  assert.deepEqual(env.live(), [POLL_HIDDEN_MS]); // the next poll keeps the hidden pace

  env.send("task/visibility-changed", { hidden: false });
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
});

test("a visibility change during a read starts no second chain; the next poll takes the new pace", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  env.send("task/visibility-changed", { hidden: true });
  assert.deepEqual(env.live(), []);
  assert.equal(env.waiting(GET_TASK("t1")).length, 1);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running"));
  assert.deepEqual(env.live(), [POLL_HIDDEN_MS]);
});

test("a visibility change without a running task or poll schedules nothing", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "succeeded") }));
  env.send("task/visibility-changed", { hidden: true });
  assert.deepEqual(env.live(), []);
  assert.equal(env.task().hidden, true);

  const stopped = reduce({ ...initialState, poll: { meetingId: "m1", taskId: "t1", phase: "waiting" } }, {
    type: "task/visibility-changed",
    hidden: true,
    shown: { id: "m1", task: task("t1", "failed") },
  });
  assert.deepEqual(stopped.effects, []);
});

test("a failed read retries after three seconds whether the page is visible or not", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/visibility-changed", { hidden: false });
  env.fire(POLL_VISIBLE_MS);
  await env.fail(GET_TASK("t1"));
  assert.deepEqual(env.live(), [POLL_RETRY_MS]);
});

test("loading the same running meeting again does not start a second poll chain", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("history/meeting-requested", { meetingId: "m1" });
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t1", "running") }));
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
  env.fire(POLL_VISIBLE_MS);
  assert.equal(env.waiting(GET_TASK("t1")).length, 1);
});

test("a different running task replaces the followed one", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("history/meeting-requested", { meetingId: "m1" });
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", {}, { task: task("t2", "running") }));
  assert.equal(env.task().poll.taskId, "t2");
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
});

// ---- language ----

test("the language shown: a pending request, the running task, the user's choice, the last task, the meeting", () => {
  const shown = (extra) => ({ id: "m1", language: "vi", task: null, generation: false, generationLanguage: null, blocked: false, ...extra });
  const choose = (language, meetingId = "m1") => reduce(initialState, { type: "task/language-chosen", language, shown: shown({ id: meetingId }) }).state;
  assert.equal(displayedLanguage(initialState, shown({})), "vi");
  assert.equal(displayedLanguage(initialState, shown({ generationLanguage: "en" })), "en");
  assert.equal(displayedLanguage(initialState, shown({ generationLanguage: "en", task: task("t", "failed", { language_hint: "vi" }) })), "vi");
  assert.equal(displayedLanguage(choose("en"), shown({ task: task("t", "failed", { language_hint: "vi" }) })), "en");
  assert.equal(displayedLanguage(choose("en", "m2"), shown({})), "vi"); // another meeting's choice does not count
  assert.equal(displayedLanguage(choose("en"), shown({ task: task("t", "running", { language_hint: "vi" }) })), "vi");
  const pending = { ...choose("en"), pending: { meetingId: "m1", requestId: "x", languageHint: "vi" } };
  assert.equal(displayedLanguage(pending, shown({ language: "en" })), "vi");
  assert.equal(displayedLanguage(initialState, shown({ language: "xx" })), "en");
});

test("a language the user chose is sent with the next start and survives unrelated repaints", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/language-chosen", { language: "vi" });
  env.send("history/notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  env.send("history/notes-source-lock-reported", { meetingId: "m1", locked: false, known: true });
  assert.deepEqual(env.view().language, { value: "vi", disabled: false });
  env.send("task/start-clicked", { requestId: "q1" });
  assert.equal(env.sent("/api/tasks")[0].body.language_hint, "vi");
});

test("a language that is not offered is not taken", async () => {
  const env = await opened(meeting("m1"));
  const before = env.task();
  env.send("task/language-chosen", { language: "fr" });
  assert.equal(env.task(), before);
  const without = reduce(initialState, { type: "task/language-chosen", language: "vi", shown: null });
  assert.equal(without.state, initialState);
});

test("the language control is locked while a task runs", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running", { language_hint: "vi" }) }));
  assert.deepEqual(env.view().language, { value: "vi", disabled: true });
});

// ---- the diagnostic command ----

test("copying the diagnostic command reports success, or falls back to selecting it", async () => {
  const id = "e".repeat(32);
  const env = await opened(meeting("m1", {}, { task: task(id, "failed", { error_code: "diarization_failed" }) }));
  env.send("task/copy-clicked");
  assert.deepEqual(env.calls[0], ["clipboard.write", `docker compose exec -T app python -m asr_studio.cli task diagnose ${id}`]);
  env.send("task/copy-succeeded", { meta: { taskId: id } });
  assert.equal(env.view().copy, "copied");

  env.send("task/copy-failed", { meta: { taskId: id }, error: { message: "denied" } });
  assert.equal(env.calls[1][0], "clipboard.select");
  env.send("task/copy-selected", { meta: { taskId: id }, value: false });
  assert.equal(env.view().copy, "selected");
  env.send("task/copy-selected", { meta: { taskId: id }, value: true });
  assert.equal(env.view().copy, "copied");
});

test("there is nothing to copy without a diagnostic command, and the answer shows only for its own task", async () => {
  const env = await opened(meeting("m1", {}, { task: task("f".repeat(32), "failed", { error_code: "odd" }) }));
  env.send("task/copy-clicked");
  assert.equal(env.calls.length, 0);

  const id = "1".repeat(32);
  const other = await opened(meeting("m1", {}, { task: task(id, "failed", { error_code: "diarization_failed" }) }));
  other.send("task/copy-succeeded", { meta: { taskId: "2".repeat(32) } });
  assert.equal(other.view().copy, null);
  other.send("task/copy-succeeded", { meta: { taskId: id } });
  assert.equal(other.view().copy, "copied");
  other.send("task/meeting-left");
  assert.equal(other.view().copy, null);
});

test("a refusal hides the diagnostic details but not the failed task's text once it is gone", async () => {
  const id = "3".repeat(32);
  const env = await opened(meeting("m1", {}, { task: task(id, "failed", { error_code: "diarization_failed" }) }));
  assert.ok(env.view().error.diagnostic);
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: "Busy." });
  assert.equal(env.view().error.diagnostic, null);
  assert.equal(text(env.view().error.message), "Dữ liệu đã thay đổi hoặc đang được sử dụng. Hãy tải lại rồi thử lại. Bấm lại để gửi một yêu cầu mới.");
});

// ---- selectors ----

test("the runtime profile reads from the device", () => {
  assert.deepEqual(selectRuntime({ device: "cuda", compute_type: "float16", model_name: "m" }), { device: "cuda", backend: "GPU · CUDA · float16", model: "m" });
  assert.equal(selectRuntime({ device: "cpu", compute_type: "int8" }).backend, "CPU fallback · int8");
  const none = selectRuntime(null);
  assert.equal(none.device, "unavailable");
  assert.equal(text(none.backend), "Worker chưa sẵn sàng");
  assert.equal(text(none.model), "Chưa xác định");
});

test("the progress bar is indeterminate without a recording length and never reads 100% early", () => {
  const view = (extra) => selectTask(initialState, { id: "m", language: "en", generation: false, generationLanguage: null, blocked: false, task: task("t", "running", extra) }).progress;
  assert.deepEqual(view({ audio_duration_seconds: null }), { known: false, fraction: 0, percent: null });
  assert.equal(view({ audio_duration_seconds: 0 }).known, false);
  assert.equal(view({ audio_duration_seconds: 100, transcript_end_seconds: 99.8 }).percent, 99);
  assert.equal(view({ audio_duration_seconds: 100, transcript_end_seconds: 130 }).percent, 100);
  assert.equal(view({ audio_duration_seconds: 100, transcript_end_seconds: -5 }).percent, 0);
  assert.deepEqual(
    view({ stage: "diarizing", audio_duration_seconds: 100, transcript_end_seconds: 99.8 }),
    { known: false, fraction: 0, percent: null },
  );
  assert.equal(selectTask(initialState, { id: "m", language: "en", task: task("t", "failed") }).progress, null);
});

test("worker checking shows only while a task is active", () => {
  const view = (status) => selectTask(initialState, { id: "m", language: "en", task: task("t", status, { worker_state: "checking-worker" }) }).workerChecking;
  assert.equal(view("accepted"), true);
  assert.equal(view("failed"), false);
});

test("without a meeting the selector shows an idle task half", () => {
  const view = selectTask(initialState, null);
  assert.equal(view.active, false);
  assert.equal(view.language, null);
  assert.equal(view.error.visible, false);
  assert.equal(view.cancelDisabled, true);
});

// ---- leaving ----

test("leaving the meeting stops the poll and its timer and clears the transient notices", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  await env.fail(GET_TASK("t1"));
  assert.equal(env.view().notice, "poll-lost");
  env.send("history/home-clicked");
  assert.equal(env.task().poll, null);
  assert.deepEqual(env.live(), []);
  assert.equal(env.view().notice, null);
});

test("leaving keeps a start on the wire counted as in flight until it answers", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  env.send("history/home-clicked");
  assert.equal(selectRequestBusy(env.task()), true);
  assert.equal(env.waiting((r) => r.url === "/api/tasks").length, 1);
});

test("the shared task answers are applied to the meeting on screen only", () => {
  const state = reduceHistory(historyInitial, { type: "history/booted", today: "2025-03-04", at: { pathname: "/", search: "", hash: "", tab: null } }).state;
  const withMeeting = { ...state, meeting: meeting("m1", {}, { task: task("t1", "running") }) };
  const take = (meetingId, body) => reduceHistory(withMeeting, { type: "task/poll-loaded", meta: { meetingId, taskId: "t1" }, response: { body } }).state;
  assert.equal(take("m1", task("t1", "succeeded")).meeting.transcription.task.status, "succeeded");
  assert.equal(take("m2", task("t1", "succeeded")), withMeeting);
  assert.equal(take("m1", null), withMeeting);
  assert.equal(take("m1", [1]), withMeeting);
  assert.equal(withMeeting.meeting.transcription.task.status, "running"); // the old meeting object is not edited
});

// ---- guards that only a single action can reach ----

test("a poll answer that comes while no read is on the wire is ignored, and a timer that fires during a read sends nothing", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  const waiting = env.task();
  const early = { type: "task/poll-loaded", meta: { meetingId: "m1", taskId: "t1" }, response: { body: task("t1", "succeeded") }, shown: env.shown() };
  assert.equal(reduce(waiting, early).state, waiting); // phase is waiting: nobody asked

  env.fire(POLL_VISIBLE_MS);
  const reading = env.task();
  const due = reduce(reading, { type: "task/poll-due", meetingId: "m1", taskId: "t1", shown: env.shown() });
  assert.equal(due.state, reading);
  assert.deepEqual(due.effects, []);
});

test("meeting-ready for the task already followed leaves the read on the wire and starts no timer", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.fire(POLL_VISIBLE_MS);
  const [reading] = env.waiting(GET_TASK("t1"));
  env.send("task/meeting-ready", { requestId: "x", stored: null, autoStart: false });
  assert.equal(reading.signal.aborted, false);
  assert.deepEqual(env.live(), []);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running"));
  assert.deepEqual(env.live(), [POLL_VISIBLE_MS]);
});

test("an answer to a start that is not the one on the wire is ignored", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  const before = env.task();
  const meta = { meetingId: "m1", requestId: "other" };
  assert.equal(reduce(before, { type: "task/start-succeeded", meta, response: { body: task("t9", "accepted") }, shown: env.shown() }).state, before);
  assert.equal(reduce(before, { type: "task/start-failed", meta, error: { message: "x" } }).state, before);
  assert.equal(reduce(initialState, { type: "task/start-failed", meta, error: { message: "x" } }).state, initialState);
});

test("only a 4xx answer is definitive; a redirect-like status is not", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 304, {});
  assert.equal(env.task().pending.requestId, "q1");
});

test("a finished meeting read for a meeting that is no longer shown retires nothing", async () => {
  const env = await opened(meeting("m1"), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "r-t1", languageHint: "en" }) } });
  const before = env.task();
  const answer = { type: "task/refresh-loaded", meta: { meetingId: "m1", origin: "poll" }, response: { body: meeting("m1", {}, { task: task("t1", "succeeded") }) } };
  assert.equal(reduce(before, { ...answer, shown: env.shown() }).state.pending, null);
  assert.equal(reduce(before, { ...answer, shown: { ...env.shown(), id: "m2" } }).state.pending, before.pending);
  assert.equal(reduce(before, { ...answer, shown: env.shown(), response: { body: { id: "m1" } } }).state.pending, before.pending); // no transcription in it
});

test("the finished meeting is installed only for the meeting it was read for", () => {
  const booted = reduceHistory(historyInitial, { type: "history/booted", today: "2025-03-04", at: { pathname: "/", search: "", hash: "", tab: null } }).state;
  const state = { ...booted, meeting: meeting("m2") };
  const answer = (meta, body) => reduceHistory(state, { type: "task/refresh-loaded", meta: { meetingId: meta, origin: "poll" }, response: { body } }).state;
  assert.equal(answer("m1", meeting("m2", {}, { generation: GENERATION })), state); // read for m1, payload is m2's
  assert.equal(answer("m2", meeting("m2", {}, { generation: GENERATION })).meeting.transcription.generation.id, "g1");
  assert.equal(answer("m2", { id: "m2" }), state);
  assert.equal(answer("m2", { meeting: meeting("m2", {}, { generation: GENERATION }) }).meeting.transcription.generation.id, "g1");
});

test("the meeting that takes a task carries the notes source lock as reported, not the one it was loaded with", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("history/notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  assert.equal(env.history().meeting.notes_source_locked, false); // the object still holds the load-time value
  env.fire(POLL_VISIBLE_MS);
  await env.answer(GET_TASK("t1"), 200, task("t1", "running"));
  assert.equal(env.history().meeting.notes_source_locked, true);

  const unknown = reduceHistory({ ...env.history(), notes: { ...env.history().notes, lockKnown: false, locked: false } }, { type: "task/poll-loaded", meta: { meetingId: "m1" }, response: { body: task("t1", "running") } }).state;
  assert.equal(unknown.meeting.notes_source_locked, true); // an unknown lock leaves the field as it was
});

test("a load repaints the task half only when a pending request came back, and leaving repaints nothing", async () => {
  const plain = await opened(meeting("m1"));
  assert.equal(plain.task().rev, 0);
  plain.send("history/home-clicked");
  assert.equal(plain.task().rev, 0);

  const restored = await opened(meeting("m1"), { stored: { [requestStorageKey("m1")]: JSON.stringify({ requestId: "k", languageHint: "en" }) } });
  assert.equal(restored.task().rev, 1);
});

// ---- round 2: the language after a switch, pinned numbers, boundaries ----

test("a language chosen for one meeting is forgotten when another meeting is opened and again when coming back", async () => {
  const env = await opened(meeting("m1", { language_hint: "en" }));
  env.send("task/language-chosen", { language: "vi" });
  assert.equal(env.view().language.value, "vi");
  env.address.pathname = "/meetings/m2";
  env.send("history/meeting-link-clicked", { meetingId: "m2" });
  await env.answer(GET_MEETING("m2"), 200, meeting("m2", { language_hint: "en" }));
  assert.equal(env.task().language, null);
  env.address.pathname = "/meetings/m1";
  env.send("history/meeting-link-clicked", { meetingId: "m1" });
  await env.answer(GET_MEETING("m1"), 200, meeting("m1", { language_hint: "en" }));
  assert.equal(env.view().language.value, "en"); // the meeting's own, as the old page reset it
});

test("the numbers and the storage key the page depends on are fixed", async () => {
  assert.equal(POLL_VISIBLE_MS, 1000);
  assert.equal(POLL_HIDDEN_MS, 3000);
  assert.equal(POLL_RETRY_MS, 3000);
  assert.equal(requestStorageKey("m1"), "asr-studio:transcription-request:m1");
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  assert.deepEqual(env.live(), [1000]);
  env.fire(1000);
  await env.fail(GET_TASK("t1"));
  assert.deepEqual(env.live(), [3000]);
});

test("a task started while the page is hidden is polled every three seconds", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/visibility-changed", { hidden: true });
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 202, task("t1", "accepted", { request_id: "q1" }));
  assert.deepEqual(env.live(), [3000]);
});

test("a 500 is not definitive and a 499 is", async () => {
  const kept = await opened(meeting("m1"));
  kept.send("task/start-clicked", { requestId: "q1" });
  await kept.answer((r) => r.url === "/api/tasks", 500, {});
  assert.equal(kept.task().pending.requestId, "q1");
  const dropped = await opened(meeting("m1"));
  dropped.send("task/start-clicked", { requestId: "q1" });
  await dropped.answer((r) => r.url === "/api/tasks", 499, {});
  assert.equal(dropped.task().pending, null);
});

test("an automatic start refused with an unknown code uses a safe status fallback", async () => {
  const env = await opened(meeting("m1"), { stored: { [autoStartStorageKey("m1")]: "1" } });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: { code: "toString", message: "Server text." } });
  assert.equal(text(env.view().error.message), "Dữ liệu đã thay đổi hoặc đang được sử dụng. Hãy tải lại rồi thử lại. Bấm lại để gửi một yêu cầu mới.");
});

test("a cancel answered 2xx with a body that is not a task is a failed cancel", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 200, null);
  assert.equal(env.view().cancelDisabled, false);
  assert.equal(env.view().error.visible, true);
  assert.equal(env.history().meeting.transcription.task.status, "running");
});

test("a new start clears the refusal of the one before", async () => {
  const env = await opened(meeting("m1"));
  env.send("task/start-clicked", { requestId: "q1" });
  await env.answer((r) => r.url === "/api/tasks", 409, { detail: "No." });
  assert.equal(env.view().error.visible, true);
  env.send("task/start-clicked", { requestId: "q2" });
  assert.equal(env.view().error.visible, false);
});

test("a cancel answered with a task that already ended stops the poll", async () => {
  const env = await opened(meeting("m1", {}, { task: task("t1", "running") }));
  env.send("task/cancel-clicked");
  await env.answer((r) => r.url === "/api/tasks/t1/cancel", 200, task("t1", "canceled"));
  assert.equal(env.task().poll, null);
  assert.deepEqual(env.live(), []);
  assert.equal(env.history().meeting.transcription.task.status, "canceled");
});

test("a task answer replaces the task and keeps the rest of the transcription", () => {
  const state = reduceHistory(historyInitial, { type: "history/booted", today: "2025-03-04", at: { pathname: "/", search: "", hash: "", tab: null } }).state;
  const withMeeting = { ...state, meeting: meeting("m1", {}, { task: task("t1", "running"), generation: GENERATION, extra: 7 }) };
  const next = reduceHistory(withMeeting, { type: "task/poll-loaded", meta: { meetingId: "m1", taskId: "t1" }, response: { body: task("t1", "succeeded") } }).state;
  assert.equal(next.meeting.transcription.generation, GENERATION);
  assert.equal(next.meeting.transcription.extra, 7);
});

test("reading the finished meeting repaints the sidebar at the query typed meanwhile", () => {
  const state = reduceHistory(historyInitial, { type: "history/booted", today: "2025-03-04", at: { pathname: "/", search: "", hash: "", tab: null } }).state;
  const typed = { ...state, meeting: meeting("m1", {}, { task: task("t1", "succeeded") }), query: "abc", shownQuery: "" };
  const next = reduceHistory(typed, { type: "task/refresh-loaded", meta: { meetingId: "m1", origin: "poll" }, response: { body: meeting("m1", {}, { task: task("t1", "succeeded"), generation: GENERATION }) } }).state;
  assert.equal(next.shownQuery, "abc");
});

test("a poll answer for a followed task while another meeting is shown ends the poll", () => {
  const followed = { ...initialState, poll: { meetingId: "m1", taskId: "t1", phase: "fetching" } };
  const next = reduce(followed, { type: "task/poll-loaded", meta: { meetingId: "m1", taskId: "t1" }, response: { body: task("t1", "running") }, shown: { id: "m2", task: null } });
  assert.equal(next.state.poll, null);
});
