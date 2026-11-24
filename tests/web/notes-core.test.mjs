import assert from "node:assert/strict";
import test from "node:test";

import { createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  blockRuns,
  hasUnsavedDraft,
  failureMessage,
  initialState,
  normalizePendingRequest,
  pendingRequestStorageKey,
  reduce,
  requestStorageKey,
  selectView,
  sourceFromHref,
  sourceHref,
  sourceReference,
} from "../../src/asr_studio/web/notes-core.js";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const ORIGIN = "http://asr.test";

const GENERATION = { id: "g1", status: "complete", source_version: 2, segments: [] };
const meeting = (id = "m1") => ({
  id,
  title: "Weekly",
  metadata_version: 3,
  meeting_date: null,
  notes_source_locked: false,
  transcription: { generation: GENERATION, task: null },
});
const revision = (extra = {}) => ({
  id: "r1",
  meeting_id: "m1",
  source_snapshot_id: "s1",
  markdown: "# Plan\n- one",
  edit_version: 1,
  source_state: "current",
  output_language: "en",
  ...extra,
});
const TASK = { id: "t1", kind: "notes", status: "running", stage: "generating", provider: "chatgpt", model: "m1", request_id: "req-0001-aaaa" };
const PROVIDERS = { providers: [{ id: "chatgpt", status: "connected" }, { id: "antigravity", status: "disconnected" }] };
const PREFS = { provider: "chatgpt", models: { chatgpt: "m1" }, reasoning_effort: "default" };
const CATALOG = { models: [{ id: "m1", display_name: "M1" }] };

// Real store and runner over fakes: requests wait in `requests` until a test answers them.
function setup() {
  const requests = [];
  const timers = [];
  const written = [];
  const hostCalls = [];
  const hostResults = {};
  const fetch = (url, init) =>
    new Promise((resolve, reject) => {
      requests.push({ url, method: init.method, body: init.body && JSON.parse(init.body), resolve, reject, done: false, signal: init.signal });
    });
  const calls = new Proxy({}, {
    get: (_, name) => (...args) => {
      hostCalls.push([name, ...args]);
      return hostResults[name]?.(...args);
    },
  });
  const run = createRunner({
    fetch,
    setTimeout: (callback, delay) => timers.push({ callback, delay, live: true }) - 1,
    clearTimeout: (id) => {
      timers[id].live = false;
    },
    storage: {
      local: () => ({ setItem: (k, v) => written.push(["local", "write", k, v]), removeItem: (k) => written.push(["local", "remove", k]) }),
      session: () => ({ setItem: (k, v) => written.push(["session", "write", k, v]), removeItem: (k) => written.push(["session", "remove", k]) }),
    },
    calls,
  });
  const store = createStore({ reduce, initialState, run });
  const is = (method, path) => (r) => r.method === method && r.url.includes(path);
  const answer = async (match, status, body) => {
    const request = requests.find((r) => !r.done && !r.signal.aborted && match(r));
    assert.ok(request, "no pending request");
    request.done = true;
    request.resolve(reply(status, body));
    await tick();
    return request;
  };
  const fail = async (match) => {
    const request = requests.find((r) => !r.done && !r.signal.aborted && match(r));
    assert.ok(request, "no pending request");
    request.done = true;
    request.reject(new TypeError("offline"));
    await tick();
  };
  const waiting = (match) => requests.filter((r) => !r.done && !r.signal.aborted && match(r));
  const fire = (delay) => {
    const timer = timers.find((item) => item.live && (delay === undefined || item.delay === delay));
    assert.ok(timer, `no timer ${delay}`);
    timer.live = false;
    timer.callback();
  };
  const live = () => timers.filter((item) => item.live).map((item) => item.delay);
  const dispatch = (type, extra = {}) => store.dispatch({ type: `notes/${type}`, ...extra });
  const context = (m = meeting(), extra = {}) =>
    dispatch("context", { detail: { view: "meeting", meeting: m, tab: "notes" }, stored: {}, href: `${ORIGIN}/meetings/${m.id}`, ...extra });
  store.dispatch({ type: "notes/booted", mode: "view", hidden: false, origin: ORIGIN });
  return { store, state: () => store.getState(), requests, answer, fail, waiting, fire, live, is, dispatch, context, written, hostCalls, hostResults };
}

const notesBody = (revisions = [], task = null, locked = false) => ({ revisions, task, source_locked: locked });

// Open the meeting and answer the notes read plus the provider choice, so the run button can work.
async function opened(env, { revisions = [], task = null, catalog = CATALOG } = {}) {
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody(revisions, task));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, catalog);
}

const notes = (env, id = "m1") => env.state().notes[id];
// The page text of a core message: parameters filled in, arrays joined.
const text = (message) => {
  if (message === null || message === undefined) return "";
  if (typeof message !== "object") return String(message);
  if (Array.isArray(message)) return message.map(text).join("");
  return message.text.replace(/\{(\w+)\}/g, (_, name) => text(message.params?.[name]));
};

// ---- context and reads ----

test("opening a meeting reads its notes and the saved choice, then the run button works", async () => {
  const env = setup();
  await opened(env);
  assert.equal(notes(env).known, true);
  assert.equal(env.state().choice.loaded, true);
  assert.equal(env.state().choice.loading, false);
  const view = selectView(env.state());
  assert.equal(view.run.disabled, false);
  assert.equal(view.run.action, "start");
  // history.js was told about the lock and the summary
  assert.ok(env.hostCalls.some(([name, id, locked, known]) => name === "history.setNotesSourceLock" && id === "m1" && locked === false && known === true));
  assert.deepEqual(env.hostCalls.filter(([name]) => name === "history.setNotesSummary").at(-1), ["history.setNotesSummary", "m1", 0, undefined, true]);
});

test("a notes answer for the meeting that was left is dropped", async () => {
  const env = setup();
  env.context(meeting("m1"));
  env.context(meeting("m2"));
  const old = env.waiting(env.is("GET", "/api/meetings/m1/notes"));
  assert.equal(old.length, 1); // reads are never aborted: the answer arrives and the reducer ignores it
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  assert.equal(notes(env, "m1").revisions.length, 0);
  assert.equal(env.state().meetingId, "m2");
  await env.answer(env.is("GET", "/api/meetings/m2/notes"), 200, notesBody([revision({ id: "r2", meeting_id: "m2" })]));
  assert.equal(notes(env, "m2").revisions[0].id, "r2");
});

test("repeated paints of the same meeting context do not add notes reads", () => {
  const env = setup();
  const current = meeting();
  env.context(current);
  env.context(current);
  env.context(current);
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, 1);
  env.context({ ...current }); // a genuinely refreshed server context still reads
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, 2);
});

test("only the newest read of a meeting counts", async () => {
  const env = setup();
  env.context();
  env.context(meeting()); // a second context: another read
  const [first, second] = env.waiting(env.is("GET", "/api/meetings/m1/notes"));
  second.done = true;
  second.resolve(reply(200, notesBody([revision({ id: "new" })])));
  await tick();
  first.done = true;
  first.resolve(reply(200, notesBody([revision({ id: "old" })])));
  await tick();
  assert.deepEqual(notes(env).revisions.map((item) => item.id), ["new"]);
});

test("a failed read says so and a poll read tries again in three seconds", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  assert.deepEqual(env.live(), [1000]); // the running task is polled
  env.fire(1000);
  await env.fail(env.is("GET", "/api/meetings/m1/notes"));
  assert.equal(notes(env).readError.text, "Không kết nối được với máy chủ trên máy này.");
  assert.deepEqual(env.live(), [3000]);
  env.fire(3000);
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], { ...TASK, status: "succeeded" }));
  assert.equal(notes(env).readError, "");
  assert.deepEqual(env.live(), []); // the task ended: no more polling
});

test("a received meeting change reads authoritative notes without echoing or duplicating the active poll", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  const beforeReads = env.waiting(env.is("GET", "/api/meetings/m1/notes")).length;
  env.dispatch("meeting-change-received", { meetingId: "m1" });
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, beforeReads + 1);
  assert.equal(env.live().filter((delay) => delay === 1000).length, 1);
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ edit_version: 2 })], TASK, true));
  assert.equal(env.hostCalls.some(([name]) => name === "meeting.change"), false);
  assert.equal(env.live().filter((delay) => delay === 1000).length, 1);
});

test("a poll that was due before the meeting changed does nothing", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.fire(1000);
  const reads = env.waiting(env.is("GET", "/api/meetings/m1/notes")).length;
  assert.equal(reads, 1);
  env.context(meeting("m2")); // the poll token moves on
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()], TASK));
  assert.equal(notes(env, "m1").revisions.length, 0);
  assert.equal(env.state().meetingId, "m2");
});

test("a hidden page polls every three seconds, a visible one every second", async () => {
  const env = setup();
  env.dispatch("visibility-changed", { hidden: true });
  await opened(env, { task: TASK });
  assert.deepEqual(env.live(), [3000]);
  env.dispatch("visibility-changed", { hidden: false });
  assert.deepEqual(env.live(), [1000]);
});

// ---- starting a run ----

test("a second press while the request is in flight sends nothing", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  env.dispatch("start-clicked", { requestId: "req-bbbb-0002" });
  const posts = env.requests.filter(env.is("POST", "/notes-runs")); // every one issued, not only the live ones
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.request_id, "req-aaaa-0001");
  assert.equal(posts[0].body.expected_model, "m1");
  assert.equal(notes(env).starting, true);
  assert.equal(selectView(env.state()).run.disabled, true);
  assert.ok(env.hostCalls.some(([name, tab]) => name === "history.selectTab" && tab === "notes"));
  // the request is stored in both areas so a reload can verify it
  assert.equal(env.written.filter(([, op, key]) => op === "write" && key === pendingRequestStorageKey("m1")).length, 4);
});

test("an accepted run locks the source, forgets the pending request and polls", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await env.answer(env.is("POST", "/notes-runs"), 202, { ...TASK, status: "accepted", stage: "accepted" });
  assert.equal(notes(env).starting, false);
  assert.equal(notes(env).sourceLocked, true);
  assert.equal(env.state().pending.m1, undefined);
  assert.ok(env.written.some(([, op, key]) => op === "remove" && key === pendingRequestStorageKey("m1")));
  assert.deepEqual(env.live(), [1000]);
  assert.equal(env.state().taskSteps.t1, 0);
});

test("a refused run clears the pending request and names the provider's reason; a network error keeps it", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await env.answer(env.is("POST", "/notes-runs"), 409, { detail: { code: "provider_quota" } });
  assert.equal(env.state().pending.m1, undefined);
  assert.match(text(notes(env).runError), /hết hạn mức/);
  assert.equal(notes(env).runErrorUncertain, false);
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, 1); // a 409 reads the notes again
  assert.ok(env.hostCalls.some(([name]) => name === "history.refreshMeeting"));
  assert.equal(env.waiting(env.is("GET", "/api/providers")).length, 1); // and reloads the choice

  const other = setup();
  await opened(other);
  other.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await other.fail(other.is("POST", "/notes-runs"));
  assert.equal(other.state().pending.m1.request_id, "req-aaaa-0001");
  assert.equal(notes(other).runErrorUncertain, true);
  assert.equal(selectView(other.state()).run.label.text, "Xác minh yêu cầu notes");
  // pressing again resends the same request id
  other.dispatch("start-clicked", { requestId: "req-cccc-0003" });
  assert.equal(other.waiting(other.is("POST", "/notes-runs"))[0].body.request_id, "req-aaaa-0001");
});

test("a connected provider with a missing saved model opens model configuration, not account connection", async () => {
  const env = setup();
  await opened(env, { catalog: { models: [{ id: "m2", display_name: "M2" }] } });
  const view = selectView(env.state());
  assert.equal(view.run.action, "configure");
  assert.equal(view.run.disabled, false);
  assert.equal(view.run.label.text, "Chọn hoặc đổi model để tạo notes.");
  assert.equal(view.run.describedBy, "notes-run-plan");
  env.dispatch("configure-clicked");
  assert.equal(env.state().choice.open, true);
  assert.equal(env.state().choice.focus, 1);
  assert.equal(env.waiting(env.is("POST", "/notes-runs")).length, 0);
});

test("typed provider busy refusal is definitive even when the server returns 503", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await env.answer(env.is("POST", "/notes-runs"), 503, {
    detail: { code: "provider_busy", message: "The provider is busy." },
  });
  assert.equal(env.state().pending.m1, undefined);
  assert.equal(notes(env).runErrorUncertain, false);
  assert.match(text(notes(env).runError), /đang bận với một thao tác/);
});

test("provider outage copy stays cause-neutral while transport loss keeps the local network copy", async () => {
  assert.match(text(failureMessage("provider_disconnected", "chatgpt")), /Hãy kết nối trong phần Kết nối/);
  const unavailable = setup();
  await opened(unavailable);
  unavailable.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await unavailable.answer(unavailable.is("POST", "/notes-runs"), 503, {
    detail: { code: "provider_unavailable", message: "The provider is unavailable." },
  });
  assert.match(text(notes(unavailable).runError), /tạm thời không khả dụng/);
  assert.doesNotMatch(text(notes(unavailable).runError), /không kết nối được Internet/);

  const network = setup();
  await opened(network);
  network.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await network.fail(network.is("POST", "/notes-runs"));
  assert.equal(text(notes(network).runError), "Không kết nối được với máy chủ trên máy này.");
  assert.equal(notes(network).runErrorUncertain, true);
});

test("notes provider codes localize start refusals and persisted output-size failures", async () => {
  const refused = setup();
  await opened(refused);
  refused.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await refused.answer(refused.is("POST", "/notes-runs"), 409, {
    detail: { code: "provider_busy", message: "Another ChatGPT operation is still running. Try again shortly." },
  });
  assert.match(text(notes(refused).runError), /ChatGPT đang bận với một thao tác/);
  assert.doesNotMatch(text(notes(refused).runError), /Another ChatGPT operation/);

  const failed = setup();
  await opened(failed, {
    task: {
      ...TASK,
      status: "failed",
      error_code: "provider_output_too_large",
      error_message: "ChatGPT's response exceeded the allowed output size.",
    },
  });
  const status = text(selectView(failed.state()).task.status);
  assert.match(status, /Phản hồi của ChatGPT vượt quá kích thước đầu ra cho phép/);
  assert.doesNotMatch(status, /ChatGPT's response exceeded/);

  const antigravity = setup();
  await opened(antigravity, {
    task: {
      ...TASK,
      provider: "antigravity",
      status: "failed",
      error_code: "provider_output_too_large",
      error_message: "Google Antigravity's response exceeded the allowed output size.",
    },
  });
  const antigravityStatus = text(selectView(antigravity.state()).task.status);
  assert.match(antigravityStatus, /Google Antigravity/);
  assert.doesNotMatch(antigravityStatus, /ChatGPT/);
});

test("a stale worker heartbeat takes precedence over the running provider message", async () => {
  const env = setup();
  await opened(env, { task: {
    ...TASK, worker_state: "checking-worker", stage: "chunking",
    progress: { current: 1, total: 3, unit: "chunks" },
  } });
  const status = text(selectView(env.state()).task.status);
  assert.match(status, /Đang kiểm tra worker/);
  assert.doesNotMatch(status, /đang tạo notes/);
  assert.equal(selectView(env.state()).task.progress, "");
});

test("a pending request found in storage on reload is verified, not replaced", async () => {
  const env = setup();
  const stored = JSON.stringify({ request_id: "req-stored-0001", output_language: "vi", provider: "chatgpt" });
  env.context(meeting(), { stored: { pending: stored } });
  assert.equal(env.state().pending.m1.request_id, "req-stored-0001");
  assert.equal(env.state().outputLanguages.m1, "vi");
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(selectView(env.state()).outputLanguage, "vi");
  assert.equal(selectView(env.state()).languageDisabled, true);
  env.dispatch("start-clicked", { requestId: "req-new-00000001" });
  assert.equal(env.waiting(env.is("POST", "/notes-runs"))[0].body.request_id, "req-stored-0001");
  // an old plain id from before the v2 key becomes a v2 request
  const legacy = setup();
  legacy.context(meeting(), { stored: { legacy: "plain-request-id" } });
  assert.equal(legacy.state().pending.m1.request_id, "plain-request-id");
  assert.ok(legacy.written.some(([, op, key]) => op === "write" && key === pendingRequestStorageKey("m1")));
});

test("a run that arrives for an active task of the meeting clears the pending request", async () => {
  const env = setup();
  env.context(meeting(), { stored: { pending: JSON.stringify({ request_id: "req-stored-0001", output_language: "en", provider: "chatgpt" }) } });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], TASK));
  assert.equal(env.state().pending.m1, undefined);
});

// ---- canceling ----

test("canceling a running task: one request, then polling continues until the worker confirms", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  env.dispatch("cancel-clicked"); // busy: ignored
  assert.equal(env.requests.filter(env.is("POST", "/api/tasks/t1/cancel")).length, 1);
  assert.equal(notes(env).canceling, true);
  assert.equal(selectView(env.state()).task.cancelDisabled, true);
  await env.answer(env.is("POST", "/api/tasks/t1/cancel"), 200, { ...TASK, status: "stopping" });
  assert.equal(notes(env).canceling, false);
  assert.equal(notes(env).task.status, "stopping");
  env.fire(1000);
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], { ...TASK, status: "canceled" }));
  assert.equal(notes(env).task.status, "canceled");
  assert.equal(notes(env).sourceLocked, false);
});

test("a failed cancel request is reported and the button works again", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  await env.fail(env.is("POST", "/api/tasks/t1/cancel"));
  assert.equal(notes(env).canceling, false);
  assert.equal(notes(env).runError.text, "Không kết nối được với máy chủ trên máy này.");
  env.dispatch("cancel-clicked");
  assert.equal(env.waiting(env.is("POST", "/api/tasks/t1/cancel")).length, 1);
});

test("a task that ended on its own after the cancel answer is read, not polled", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  await env.answer(env.is("POST", "/api/tasks/t1/cancel"), 200, { ...TASK, status: "canceled" });
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, 1);
  env.fire(1000); // the timer of the earlier poll finds the task ended and reads nothing more
  assert.equal(env.waiting(env.is("GET", "/api/meetings/m1/notes")).length, 1);
  assert.deepEqual(env.live(), []);
});

// ---- saving ----

const saved = (env) => env.state().drafts.r1;

test("saving: one PATCH at a time, the draft becomes the saved text", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "# Plan\n- two" });
  assert.equal(hasUnsavedDraft(env.state()), true);
  env.dispatch("save-clicked");
  env.dispatch("save-clicked");
  const patches = env.requests.filter(env.is("PATCH", "/api/notes/r1"));
  assert.equal(patches.length, 1);
  assert.deepEqual(patches[0].body, { expected_edit_version: 1, markdown: "# Plan\n- two" });
  assert.equal(selectView(env.state()).savedWork.textareaDisabled, true);
  await env.answer(env.is("PATCH", "/api/notes/r1"), 200, revision({ markdown: "# Plan\n- two", edit_version: 2 }));
  assert.equal(notes(env).saving.r1, undefined);
  assert.equal(notes(env).revisions[0].edit_version, 2);
  assert.equal(saved(env).baselineMarkdown, "# Plan\n- two");
  assert.equal(hasUnsavedDraft(env.state()), false);
  assert.equal(notes(env).actionMessage.text, "Markdown đã được lưu.");
});

test("a revision conflict reads the latest, keeps the draft and offers both ways out", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, { detail: "version" });
  assert.equal(notes(env).saving.r1, true); // still busy while the latest is read
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "theirs", edit_version: 5 }));
  assert.equal(notes(env).saving.r1, undefined);
  assert.equal(saved(env).value, "mine");
  assert.deepEqual(saved(env).conflict, { markdown: "theirs", editVersion: 5 });
  assert.equal(selectView(env.state()).savedWork.conflict.disabled, false);

  // keep the draft: save it on top of the latest
  env.dispatch("keep-draft-clicked");
  const patch = env.waiting(env.is("PATCH", "/api/notes/r1"))[0];
  assert.deepEqual(patch.body, { expected_edit_version: 5, markdown: "mine" });
  assert.equal(saved(env).conflict, null);
});

test("a conflict whose latest cannot be read still keeps the draft and says why", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, {});
  await env.fail(env.is("GET", "/api/notes/r1"));
  assert.equal(saved(env).value, "mine");
  assert.deepEqual(saved(env).conflict, { markdown: "", editVersion: 1 });
  assert.match(text(notes(env).actionMessage), /chưa tải được phiên bản mới nhất/);
});

test("use the latest saved text replaces the draft", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  // the notes change elsewhere and a poll read brings the new revision
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "theirs", edit_version: 2 })]));
  assert.deepEqual(saved(env).conflict, { markdown: "theirs", editVersion: 2 });
  env.dispatch("use-latest-clicked");
  assert.equal(saved(env).value, "theirs");
  assert.equal(saved(env).conflict, null);
  assert.equal(hasUnsavedDraft(env.state()), false);
});

test("a clean draft follows the saved text; a dirty one is kept", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "v2", edit_version: 2 })]));
  assert.equal(saved(env).value, "v2");
  env.dispatch("draft-edited", { value: "typed" });
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "v2", edit_version: 2 })]));
  assert.equal(saved(env).value, "typed");
  assert.equal(saved(env).conflict, null); // same edit version: no conflict
});

test("copy reads the saved text first and refuses while a draft is unsaved", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("copy-clicked");
  assert.match(text(notes(env).actionMessage), /Lưu hoặc giải quyết bản nháp/);
  assert.equal(env.waiting(env.is("GET", "/api/notes/r1")).length, 0);
  env.dispatch("draft-edited", { value: "# Plan\n- one" });
  env.dispatch("copy-clicked");
  env.hostResults.copy = async () => true;
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision());
  assert.ok(env.hostCalls.some(([name, markdown]) => name === "copy" && markdown === "# Plan\n- one"));
  await tick();
  assert.match(text(notes(env).actionMessage), /Đã sao chép/);
});

test("a draft edited while the saved text is being read is kept and the copy is not made", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("export-clicked");
  env.dispatch("draft-edited", { value: "typed meanwhile" });
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ edit_version: 2, markdown: "other" }));
  assert.ok(!env.hostCalls.some(([name]) => name === "download"));
  assert.equal(saved(env).value, "typed meanwhile");
  assert.deepEqual(saved(env).conflict, { markdown: "other", editVersion: 2 });
});

test("export hands the saved text to the download", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("export-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision());
  assert.deepEqual(env.hostCalls.find(([name]) => name === "download"), ["download", "Weekly.md", "# Plan\n- one"]);
});

test("the view mode is remembered through a storage effect", () => {
  const env = setup();
  env.dispatch("mode-chosen", { mode: "write" });
  assert.equal(env.state().mode, "write");
  assert.deepEqual(env.written.at(-1), ["local", "write", "asr-studio:notes-view-mode", "write"]);
  env.dispatch("mode-chosen", { mode: "write" });
  assert.equal(env.written.length, 1);
});

// ---- provider choice ----

test("a choice load that fails blocks new runs until it is reloaded; an old load is ignored", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.fail(env.is("GET", "/api/providers"));
  assert.equal(env.state().choice.loaded, false);
  assert.equal(env.state().choice.loading, false);
  assert.equal(selectView(env.state()).run.disabled, true);
  assert.match(text(env.state().choice.message), /Chưa đọc được lựa chọn/);
  env.dispatch("choice-reload-clicked");
  env.dispatch("choice-reload-clicked"); // a newer load replaces the older one
  const staleProviders = env.requests.filter((r) => r.url === "/api/providers" && !r.done);
  assert.equal(staleProviders.length, 2);
  staleProviders[0].done = true;
  staleProviders[0].resolve(reply(200, PROVIDERS));
  await tick();
  assert.equal(env.state().choice.load.providers, null); // the older answer did not count
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  while (env.waiting(env.is("GET", "/api/notes-preferences")).length) await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(env.state().choice.loading, false);
  assert.equal(env.state().choice.loaded, true);
});

test("picking a provider with a saved model saves at once; without one it waits for a model", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("provider-picked", { provider: "antigravity" });
  assert.equal(env.state().choice.localProvider, "antigravity");
  assert.equal(env.waiting(env.is("PUT", "/api/notes-preferences")).length, 0);
  env.dispatch("model-picked", { model: "g1" });
  const put = env.waiting(env.is("PUT", "/api/notes-preferences"))[0];
  assert.deepEqual(put.body, { provider: "antigravity", model: "g1", reasoning_effort: "high" });
  assert.equal(env.state().choice.saving, true);
  await env.answer(env.is("PUT", "/api/notes-preferences"), 200, { provider: "antigravity", models: { antigravity: "g1", chatgpt: "m1" } });
  assert.equal(env.state().choice.saving, false);
  assert.equal(env.state().choice.localProvider, null);
  assert.equal(env.state().choice.preferences.provider, "antigravity");
  assert.match(text(env.state().choice.message), /Google Antigravity · g1/);
});

test("a failed preference save keeps the saved choice and says so", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("model-picked", { model: "m1" });
  await env.answer(env.is("PUT", "/api/notes-preferences"), 500, {});
  assert.equal(env.state().choice.saving, false);
  assert.equal(env.state().choice.preferences.models.chatgpt, "m1");
  assert.match(text(env.state().choice.message), /Chưa lưu được lựa chọn/);
});

test("the language is remembered per meeting and cannot change while a request is pending", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("language-chosen", { language: "vi" });
  assert.equal(env.state().outputLanguages.m1, "vi");
  assert.equal(env.written.filter(([, , key]) => key === "asr-studio:notes-output-language:m1").length, 2);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  env.dispatch("language-chosen", { language: "en" });
  assert.equal(env.state().outputLanguages.m1, "vi");
  assert.equal(env.waiting(env.is("POST", "/notes-runs"))[0].body.output_language, "vi");
});

// ---- source ----

const SNAPSHOT = (id) => ({
  id,
  meeting_id: "m1",
  generation_id: "g1",
  meeting_date: null,
  speakers: [],
  segments: [{ id: "seg1", start_seconds: 12, text: "hello", speaker_id: "s1" }],
});
const reference = (snapshotId) => ({ meetingId: "m1", snapshotId, segmentId: "seg1" });

test("opening a source fetches the snapshot, shows the segment and sends the player there", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.hostResults["history.playRecording"] = async () => true;
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: true });
  assert.ok(env.hostCalls.some(([name]) => name === "history.showNotesSourceTab"));
  assert.match(text(env.state().source.status), /Đang tải bản chụp/);
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  assert.ok(!env.hostCalls.some(([name]) => name === "history.markNotesSource" || name === "history.seekNotesSource"), "mark and seek wait for the render");
  env.fire(0);
  await tick();
  await tick();
  const view = selectView(env.state()).source;
  assert.equal(view.hidden, false);
  assert.equal(view.text, "hello");
  assert.equal(view.time, "00:00:12");
  assert.match(text(view.status), /đang phát/);
  assert.ok(env.hostCalls.some(([name, segment, seconds]) => name === "history.markNotesSource" && segment === "seg1" && seconds === 12));
  // the snapshot is cached: opening it again asks the server for nothing
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  assert.equal(env.waiting(env.is("GET", "/api/notes-sources/")).length, 0);
});

test("the answer for a source that was replaced meanwhile is dropped", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.hostResults["history.playRecording"] = async () => false;
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  env.dispatch("source-opened", { reference: { ...reference("s2"), segmentId: "seg1" }, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  assert.equal(env.state().source.current, null);
  await env.answer(env.is("GET", "/api/notes-sources/s2"), 200, SNAPSHOT("s2"));
  env.fire(0);
  await tick();
  await tick();
  assert.equal(env.state().source.current.snapshotId, "s2");
  assert.match(text(env.state().source.status), /Bấm phát trên trình phát/);
});

test("a snapshot of another meeting, a missing segment and a failed player each say so", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, { ...SNAPSHOT("s1"), meeting_id: "other" });
  assert.match(text(env.state().source.status), /không khớp cuộc họp/);
  assert.equal(env.state().source.current, null);
  env.dispatch("source-opened", { reference: { ...reference("s3"), segmentId: "missing" }, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s3"), 200, SNAPSHOT("s3"));
  assert.match(text(env.state().source.status), /Không tìm thấy đoạn/);
  env.hostResults["history.seekNotesSource"] = async () => false;
  env.dispatch("source-opened", { reference: reference("s4"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s4"), 200, SNAPSHOT("s4"));
  env.fire(0);
  await tick();
  assert.match(text(env.state().source.status), /chưa thể tua bản ghi/);
});

test("closing a source clears the address query and tells history.js", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.hostResults["history.playRecording"] = async () => true;
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  await tick();
  await tick();
  env.dispatch("source-closed", { href: `${ORIGIN}/meetings/m1?notes_source=s1&segment=seg1#transcript` });
  assert.equal(env.state().source.visible, false);
  assert.deepEqual(env.hostCalls.at(-1), ["replaceUrl", "/meetings/m1#transcript"]);
  assert.ok(env.hostCalls.some(([name, arg]) => name === "history.markNotesSource" && arg === null));
});

test("the page address opens a source once and Back to a plain address closes it", async () => {
  const env = setup();
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.hostResults["history.playRecording"] = async () => true;
  env.context(meeting(), { href: `${ORIGIN}/meetings/m1?notes_source=s1&segment=seg1` });
  assert.equal(env.state().source.key, "m1\u0000s1\u0000seg1");
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  await tick();
  await tick();
  const id = env.state().source.id;
  env.dispatch("location-changed", { href: `${ORIGIN}/meetings/m1?notes_source=s1&segment=seg1` });
  assert.equal(env.state().source.id, id); // already showing it
  env.dispatch("location-changed", { href: `${ORIGIN}/meetings/m1#notes` });
  assert.equal(env.state().source.visible, false);
  assert.ok(!env.hostCalls.some(([name]) => name === "replaceUrl"));
});

test("source links are only the saved-source shape of this origin", () => {
  const ok = "/meetings/m1?notes_source=s1&segment=seg1#transcript";
  assert.deepEqual(sourceReference(ok, ORIGIN, "m1", "s1"), { meetingId: "m1", snapshotId: "s1", segmentId: "seg1", href: ok });
  assert.equal(sourceReference(ok, ORIGIN, "m2"), null);
  assert.equal(sourceReference("//evil.test/meetings/m1?notes_source=s1&segment=a", ORIGIN), null);
  assert.equal(sourceReference("/meetings/m1?notes%5Fsource=s1&segment=a", ORIGIN), null);
  assert.equal(sourceReference("/meetings/m1?notes_source=s1&segment=a&x=1", ORIGIN), null);
  assert.equal(sourceFromHref(`${ORIGIN}/meetings/m1?segment=a&notes_source=s1`), null);
  assert.equal(sourceFromHref(`${ORIGIN}/meetings/m1`), null);
});

// ---- the page ----

test("leaving the meeting list stops polling and the tab keeps no meeting", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  assert.deepEqual(env.live(), [1000]);
  env.dispatch("context", { detail: { view: "list", meeting: null }, stored: {}, href: `${ORIGIN}/` });
  assert.equal(env.state().meetingId, null);
  assert.deepEqual(env.live(), []);
});

test("a transcript that is not complete shows only the way to get one", () => {
  const env = setup();
  const pending = { ...meeting(), transcription: { generation: null, task: { status: "running" } } };
  env.context(pending);
  const view = selectView(env.state());
  assert.equal(view.needed, true);
  assert.equal(view.emptyCopy.text, "Đang chép lời. Notes tạo được sau khi transcript hoàn tất.");
  assert.equal(view.run.disabled, true);
});

const patches = (env) => env.requests.filter((r) => r.method === "PATCH");
const posts = (env, path) => env.requests.filter((r) => r.method === "POST" && r.url.includes(path));

// ---- repeat while busy: count every request ever issued, not only the ones still waiting ----
test("save pressed twice issues one PATCH in total", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  env.dispatch("save-clicked");
  assert.equal(patches(env).length, 1);
});
test("save on a clean draft sends nothing", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("save-clicked");
  assert.equal(patches(env).length, 0);
});
test("start pressed twice issues one POST in total", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  env.dispatch("start-clicked", { requestId: "req-bbbb-0002" });
  assert.equal(posts(env, "/notes-runs").length, 1);
});
test("no start while a task is active, the source is locked, or transcription runs", async () => {
  const active = setup();
  await opened(active, { task: TASK });
  active.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  assert.equal(posts(active, "/notes-runs").length, 0);
  assert.equal(selectView(active.state()).run.disabled, true);
  const locked = setup();
  locked.context();
  await locked.answer(locked.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], null, true));
  await locked.answer(locked.is("GET", "/api/providers"), 200, PROVIDERS);
  await locked.answer(locked.is("GET", "/api/notes-preferences"), 200, PREFS);
  await locked.answer(locked.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  locked.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  assert.equal(posts(locked, "/notes-runs").length, 0);
  const busy = setup();
  busy.context(meeting(), { detail: { view: "meeting", meeting: meeting(), transcriptionRequestInFlight: true } });
  await busy.answer(busy.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await busy.answer(busy.is("GET", "/api/providers"), 200, PROVIDERS);
  await busy.answer(busy.is("GET", "/api/notes-preferences"), 200, PREFS);
  await busy.answer(busy.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  busy.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  assert.equal(posts(busy, "/notes-runs").length, 0);
});
test("no start while the choice is being saved", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("model-picked", { model: "m1" });
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  assert.equal(posts(env, "/notes-runs").length, 0);
});
test("cancel pressed twice issues one POST; a finished task cannot be canceled", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  env.dispatch("cancel-clicked");
  assert.equal(posts(env, "/cancel").length, 1);
  const done = setup();
  await opened(done, { task: { ...TASK, status: "succeeded" } });
  done.dispatch("cancel-clicked");
  assert.equal(posts(done, "/cancel").length, 0);
});
test("an invalid cancel answer is a failed cancel", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  await env.answer(env.is("POST", "/cancel"), 200, { nonsense: true });
  assert.equal(notes(env).canceling, false);
  assert.ok(text(notes(env).runError) !== "");
});

// ---- conflict and drafts ----
test("conflict: latest text is kept in the conflict; a mismatched latest keeps the draft and says why", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, { detail: "version" });
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "theirs", edit_version: 5 }));
  assert.equal(saved(env).conflict.markdown, "theirs");
  assert.equal(notes(env).saving.r1, undefined);
  env.dispatch("use-latest-clicked");
  assert.equal(saved(env).conflict, null);
  assert.equal(saved(env).value, "theirs");
  const other = setup();
  await opened(other, { revisions: [revision()] });
  other.dispatch("draft-edited", { value: "mine" });
  other.dispatch("save-clicked");
  await other.answer(other.is("PATCH", "/api/notes/r1"), 409, { detail: "version" });
  await other.answer(other.is("GET", "/api/notes/r1"), 200, revision({ id: "r9", markdown: "theirs", edit_version: 5 }));
  assert.equal(other.state().drafts.r1.conflict.markdown, "");
  assert.match(text(notes(other).actionMessage), /chưa tải được phiên bản mới nhất/);
});
test("a save answer for another revision is refused and clears busy", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 200, revision({ id: "r9", edit_version: 2, markdown: "mine" }));
  assert.match(text(notes(env).actionMessage), /nhầm revision/);
  assert.equal(notes(env).saving.r1, undefined);
  assert.equal(saved(env).value, "mine");
  assert.equal(saved(env).baselineMarkdown, "# Plan\n- one");
});
test("a non-409 save failure says why and clears busy; success after keep-draft clears the conflict", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 500, { detail: "boom" });
  assert.ok(text(notes(env).actionMessage) !== "");
  assert.equal(notes(env).saving.r1, undefined);
  env.dispatch("save-clicked");
  assert.equal(patches(env).length, 2);
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, { detail: "version" });
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "theirs", edit_version: 5 }));
  env.dispatch("keep-draft-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 200, revision({ markdown: "mine", edit_version: 6 }));
  assert.equal(saved(env).conflict, null);
  assert.equal(saved(env).expectedEditVersion, 6);
});
test("editing while the saved text moved on records the conflict", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "v2", edit_version: 2 })]));
  env.dispatch("draft-edited", { value: "typed" });
  assert.equal(saved(env).conflict, null);
  env.dispatch("draft-edited", { value: "typed more" });
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "v3", edit_version: 3 })]));
  assert.deepEqual(saved(env).conflict, { markdown: "v3", editVersion: 3 });
  assert.equal(saved(env).value, "typed more");
});
test("copy of a clean draft uses the newer saved text and refreshes the draft", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.hostResults.copy = async () => true;
  env.dispatch("copy-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "newer", edit_version: 2 }));
  assert.ok(env.hostCalls.some(([name, md]) => name === "copy" && md === "newer"));
  assert.equal(saved(env).value, "newer");
  assert.equal(saved(env).expectedEditVersion, 2);
});
test("copy: a read of another note, or a note picked meanwhile, makes no copy", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("copy-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ id: "r9" }));
  assert.ok(!env.hostCalls.some(([name]) => name === "copy"));
  assert.match(text(notes(env).actionMessage), /không khớp/);
  const two = setup();
  await opened(two, { revisions: [revision(), revision({ id: "r2", edit_version: 1, markdown: "other" })] });
  two.dispatch("copy-clicked");
  const request = two.waiting(two.is("GET", "/api/notes/r1"))[0];
  two.dispatch("revision-picked", { id: "r2" });
  assert.equal(request.signal.aborted, true);
  request.resolve(reply(200, revision({ markdown: "stale", edit_version: 2 })));
  await tick();
  assert.ok(!two.hostCalls.some(([name]) => name === "copy"));
  assert.equal(notes(two).actionMessage, "");
  assert.equal(notes(two).revisions[0].markdown, "# Plan\n- one");
});
test("picking a revision or editing clears the action message", async () => {
  const env = setup();
  await opened(env, { revisions: [revision(), revision({ id: "r2", markdown: "other" })] });
  env.dispatch("draft-edited", { value: "x" });
  env.dispatch("copy-clicked");
  assert.ok(text(notes(env).actionMessage) !== "");
  env.dispatch("draft-edited", { value: "xy" });
  assert.equal(text(notes(env).actionMessage), "");
  env.dispatch("copy-clicked");
  env.dispatch("revision-picked", { id: "r2" });
  assert.equal(text(notes(env).actionMessage), "");
});

test("a 5xx answer to a start keeps the pending request as uncertain", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await env.answer(env.is("POST", "/notes-runs"), 503, { detail: "busy" });
  assert.equal(env.state().pending.m1.request_id, "req-aaaa-0001");
  assert.equal(notes(env).runErrorUncertain, true);
});
test("an ordinary edit raises no conflict", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "# Plan\n- edited" });
  assert.equal(selectView(env.state()).savedWork.conflict, null);
});
test("switching revisions and back keeps an unsaved draft", async () => {
  const env = setup();
  await opened(env, { revisions: [revision(), revision({ id: "r2", markdown: "other" })] });
  env.dispatch("draft-edited", { value: "# Plan\n- edited" });
  env.dispatch("revision-picked", { id: "r2" });
  env.dispatch("revision-picked", { id: "r1" });
  assert.equal(selectView(env.state()).savedWork.draftValue, "# Plan\n- edited");
  assert.equal(hasUnsavedDraft(env.state()), true);
});
test("speaker names come with the context and are cleared by a context without them", async () => {
  const env = setup();
  await opened(env, { revisions: [] });
  assert.deepEqual(selectView(env.state()).reminder.names, []);
  env.context(meeting(), { speakerNames: ["Speaker 1"] });
  assert.deepEqual(selectView(env.state()).reminder.names, ["Speaker 1"]);
  env.context(meeting());
  assert.deepEqual(selectView(env.state()).reminder.names, []);
});

// ---- pending recovery, language, mode ----
test("normalizePendingRequest: provider default, partial or invalid expected fields", () => {
  const base = { request_id: "req-stored-0001", output_language: "en" };
  assert.equal(normalizePendingRequest(JSON.stringify(base)).provider, "chatgpt");
  assert.equal(normalizePendingRequest(JSON.stringify({ ...base, expected_generation_id: "g" })), null);
  const full = { ...base, expected_generation_id: "g", expected_source_version: 1, expected_metadata_version: 1 };
  assert.ok(normalizePendingRequest(JSON.stringify(full)));
  assert.equal(normalizePendingRequest(JSON.stringify({ ...full, expected_source_version: 0 })), null);
  assert.equal(normalizePendingRequest(JSON.stringify({ ...full, expected_metadata_version: 0 })), null);
  assert.ok(normalizePendingRequest(JSON.stringify({ ...base, expected_generation_id: "g" }), { legacy: true }));
});
test("stored pending: the first one wins, the stored language is adopted once", async () => {
  const env = setup();
  const one = JSON.stringify({ request_id: "req-stored-0001", output_language: "vi", provider: "chatgpt" });
  const two = JSON.stringify({ request_id: "req-stored-0002", output_language: "en", provider: "chatgpt" });
  env.context(meeting(), { stored: { pending: one } });
  env.context(meeting(), { stored: { pending: two } });
  assert.equal(env.state().pending.m1.request_id, "req-stored-0001");
  const lang = setup();
  lang.context(meeting(), { stored: { language: "vi" } });
  assert.equal(lang.state().outputLanguages.m1, "vi");
  lang.context(meeting(), { stored: { language: "en" } });
  assert.equal(lang.state().outputLanguages.m1, "vi");
});
test("a read that shows a finished task of another request keeps the pending request", async () => {
  const env = setup();
  env.context(meeting(), { stored: { pending: JSON.stringify({ request_id: "req-stored-0001", output_language: "en", provider: "chatgpt" }) } });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], { ...TASK, status: "succeeded", request_id: "req-other-0009" }));
  assert.equal(env.state().pending.m1.request_id, "req-stored-0001");
  const same = setup();
  same.context(meeting(), { stored: { pending: JSON.stringify({ request_id: "req-0001-aaaa", output_language: "en", provider: "chatgpt" }) } });
  await same.answer(same.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], { ...TASK, status: "succeeded" }));
  assert.equal(same.state().pending.m1, undefined);
});
test("clearing a pending request removes the v2 and the legacy key from both areas", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  await env.answer(env.is("POST", "/notes-runs"), 202, { ...TASK, status: "accepted" });
  const removed = env.written.filter(([, op]) => op === "remove").map(([area, , key]) => `${area}:${key}`).sort();
  assert.deepEqual(removed, [
    `local:${pendingRequestStorageKey("m1")}`, `local:${requestStorageKey("m1")}`,
    `session:${pendingRequestStorageKey("m1")}`, `session:${requestStorageKey("m1")}`,
  ].sort());
});
test("the stored request carries the confirmed model and effort; a complete one is resent unchanged", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  const body = posts(env, "/notes-runs")[0].body;
  assert.equal(body.expected_model, "m1");
  assert.ok(body.expected_reasoning_effort);
  assert.equal(body.provider, "chatgpt");
  const stored = JSON.stringify({ request_id: "req-stored-0001", output_language: "vi", provider: "chatgpt", expected_generation_id: "g1", expected_source_version: 2, expected_metadata_version: 3 });
  const again = setup();
  again.context(meeting(), { stored: { pending: stored } });
  await again.answer(again.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await again.answer(again.is("GET", "/api/providers"), 200, PROVIDERS);
  await again.answer(again.is("GET", "/api/notes-preferences"), 200, PREFS);
  again.dispatch("start-clicked", { requestId: "req-new-00000001" });
  assert.deepEqual(posts(again, "/notes-runs")[0].body, JSON.parse(stored));
});
test("booted restores the stored mode and hidden flag; unknown mode and language are ignored", () => {
  const env = setup();
  env.dispatch("booted", { mode: "write", hidden: true, origin: ORIGIN });
  assert.equal(env.state().mode, "write");
  assert.equal(env.state().hidden, true);
  env.dispatch("booted", { mode: "weird", hidden: false, origin: ORIGIN });
  assert.equal(env.state().mode, "view");
  env.dispatch("mode-chosen", { mode: "weird" });
  assert.equal(env.state().mode, "view");
  assert.equal(env.written.length, 0);
});
test("an unknown language is ignored", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("language-chosen", { language: "fr" });
  assert.equal(env.state().outputLanguages.m1, "en");
  assert.equal(env.written.length, 0);
});

// ---- choice ----
test("picking a provider that has a saved model saves it at once", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("provider-picked", { provider: "chatgpt" });
  assert.equal(env.requests.filter((r) => r.method === "PUT").length, 1);
  assert.equal(env.requests.filter((r) => r.method === "PUT")[0].body.model, "m1");
  env.dispatch("provider-picked", { provider: "nope" });
  assert.equal(env.requests.filter((r) => r.method === "PUT").length, 1);
  env.dispatch("model-picked", { model: "" });
  assert.equal(env.requests.filter((r) => r.method === "PUT").length, 1);
});
test("a second preference save makes the first answer stale", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("model-picked", { model: "m1" });
  env.dispatch("model-picked", { model: "m2" });
  const puts = env.requests.filter((r) => r.method === "PUT");
  puts[0].resolve(reply(200, { provider: "chatgpt", models: { chatgpt: "m1" } }));
  await tick();
  assert.equal(env.state().choice.saving, true);
  assert.equal(env.state().choice.message, "");
  puts[1].resolve(reply(200, { provider: "chatgpt", models: { chatgpt: "m2" } }));
  await tick();
  assert.equal(env.state().choice.saving, false);
});
test("provider-choice-changed, the reload button and a finished run each reload the choice", async () => {
  const env = setup();
  await opened(env);
  const providers = () => env.requests.filter((r) => r.url.endsWith("/api/providers")).length;
  assert.equal(providers(), 1);
  env.dispatch("provider-choice-changed");
  assert.equal(providers(), 2);
  const run = setup();
  await opened(run, { task: TASK });
  const before = run.requests.filter((r) => r.url.endsWith("/api/providers")).length;
  run.context();
  await run.answer(run.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()], { ...TASK, status: "succeeded" }));
  assert.equal(run.requests.filter((r) => r.url.endsWith("/api/providers")).length, before + 1);
});
test("another meeting closes the open choice and loads it again; the first load's answers are stale", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("change-toggled");
  assert.equal(env.state().choice.open, true);
  env.context(meeting("m2"));
  assert.equal(env.state().choice.open, false);
  assert.equal(env.requests.filter((r) => r.url.endsWith("/api/providers")).length, 2);
  const stale = env.requests.filter((r) => r.url.endsWith("/api/providers"))[0];
  assert.equal(env.state().choice.loadId, 2);
  stale.resolve(reply(200, { providers: [{ id: "chatgpt", status: "disconnected" }, { id: "antigravity", status: "disconnected" }] }));
  await tick();
  assert.equal(env.state().choice.loading, true);
});
test("a choice load for the same meeting is not repeated while it is loaded", async () => {
  const env = setup();
  await opened(env);
  env.context();
  assert.equal(env.requests.filter((r) => r.url.endsWith("/api/providers")).length, 1);
});

// ---- host boundary ----
test("name-speakers and open-transcript go to history.js", () => {
  const env = setup();
  env.dispatch("name-speakers-clicked");
  env.dispatch("open-transcript-clicked");
  assert.ok(env.hostCalls.some(([name]) => name === "history.focusSpeakerCatalog"));
  assert.ok(env.hostCalls.some(([name, tab]) => name === "history.selectTab" && tab === "transcript"));
});
test("the lock reaches history.js from a context, a read and an accepted run", async () => {
  const env = setup();
  env.context(meeting());
  assert.ok(env.hostCalls.some(([name, id, locked]) => name === "history.setNotesSourceLock" && id === "m1" && locked === false));
  const locked = setup();
  locked.context({ ...meeting(), notes_source_locked: true });
  assert.ok(locked.hostCalls.some(([name, , lock, known]) => name === "history.setNotesSourceLock" && lock === true && known === true));
  assert.equal(locked.state().notes.m1.sourceLocked, true);
  const read = setup();
  read.context();
  const before = read.hostCalls.length;
  await read.answer(read.is("GET", "/api/meetings/m1/notes"), 200, notesBody([], null, true));
  assert.ok(read.hostCalls.slice(before).some(([name, , lock, known]) => name === "history.setNotesSourceLock" && lock === true && known === true));
  const start = setup();
  await opened(start);
  start.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  const n = start.hostCalls.length;
  await start.answer(start.is("POST", "/notes-runs"), 202, { ...TASK, status: "accepted" });
  assert.ok(start.hostCalls.slice(n).some(([name, , lock]) => name === "history.setNotesSourceLock" && lock === true));
});
test("a context sends the summary again even when it did not change; a view other than meeting has no meeting", async () => {
  const env = setup();
  await opened(env);
  const count = () => env.hostCalls.filter(([name]) => name === "history.setNotesSummary").length;
  const before = count();
  env.context();
  assert.equal(count(), before + 1);
  env.dispatch("context", { detail: { view: "list", meeting: meeting() }, stored: {}, href: ORIGIN + "/" });
  assert.equal(env.state().meetingId, null);
  assert.equal(env.state().view, "list");
});
test("a poll for the meeting that was left does not reschedule; token bump drops a poll read", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  assert.deepEqual(env.live(), [1000]);
  env.context(meeting("m2"));
  assert.deepEqual(env.live(), []);
  assert.equal(env.state().poll.scheduled, false);
});
test("source: closing bumps the id so a late snapshot is dropped; a stale failure is dropped", async () => {
  const env = setup();
  await opened(env);
  const ref = { meetingId: "m1", snapshotId: "s9", segmentId: "seg1" };
  env.dispatch("source-opened", { reference: ref, fromClick: false });
  env.dispatch("source-closed", { href: null });
  const snap = { id: "s9", meeting_id: "m1", generation_id: "g1", meeting_date: null, speakers: [], segments: [{ id: "seg1", start_seconds: 12, text: "hello", speaker_id: "s1" }] };
  await env.answer(env.is("GET", "/api/notes-sources/s9"), 200, snap);
  assert.equal(env.state().source.current, null);
  assert.equal(env.state().source.visible, false);
});
test("source: a negative start or missing segment fails", async () => {
  const env = setup();
  await opened(env);
  const ref = { meetingId: "m1", snapshotId: "s9", segmentId: "seg1" };
  env.dispatch("source-opened", { reference: ref, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s9"), 200, { id: "s9", meeting_id: "m1", generation_id: "g1", meeting_date: null, speakers: [], segments: [{ id: "seg1", start_seconds: -1, text: "hello", speaker_id: "s1" }] });
  assert.equal(env.state().source.current, null);
  assert.match(text(env.state().source.status), /Không tìm thấy đoạn/);
});
test("source seek answered true marks the source on the player", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  const ref = { meetingId: "m1", snapshotId: "s9", segmentId: "seg1" };
  env.dispatch("source-opened", { reference: ref, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s9"), 200, { id: "s9", meeting_id: "m1", generation_id: "g1", meeting_date: null, speakers: [], segments: [{ id: "seg1", start_seconds: 12, text: "hello", speaker_id: "s1" }] });
  await tick();
  const mark = env.hostCalls.length;
  env.dispatch("source-seek-clicked");
  await tick();
  assert.ok(env.hostCalls.slice(mark).some(([name, seg, at]) => name === "history.markNotesSource" && seg === "seg1" && at === 12));
});
test("a location for another meeting does not open a source", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("location-changed", { href: `${ORIGIN}/meetings/m2?note-source=s9&segment=seg1#transcript` });
  assert.equal(env.requests.filter((r) => r.url.includes("/api/notes-sources/")).length, 0);
  env.dispatch("source-opened", { reference: { meetingId: "m2", snapshotId: "s9", segmentId: "seg1" }, fromClick: true });
  assert.equal(env.requests.filter((r) => r.url.includes("/api/notes-sources/")).length, 0);
});

// ---- more reducer paths ----
test("a poll that was due for an older schedule reads nothing", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  const reads = () => env.requests.filter((r) => r.method === "GET" && r.url.includes("/api/meetings/m1/notes")).length;
  const before = reads();
  env.dispatch("poll-due", { meetingId: "m1", token: env.state().poll.token - 1 });
  assert.equal(reads(), before);
  env.dispatch("poll-due", { meetingId: "m1", token: env.state().poll.token });
  assert.equal(reads(), before + 1);
});
test("a notes answer that is not the published shape is reported", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, { nonsense: true });
  assert.match(text(notes(env).readError), /API đã công bố/);
});
test("a failed first read tells history.js the lock is unknown", async () => {
  const env = setup();
  env.context({ ...meeting(), notes_source_locked: undefined });
  const before = env.hostCalls.length;
  await env.fail(env.is("GET", "/api/meetings/m1/notes"));
  assert.ok(env.hostCalls.slice(before).some(([name, , lock, known]) => name === "history.setNotesSourceLock" && lock === false && known === false));
});
test("a failed catalog empties that provider and says why", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("provider-choice-changed");
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 500, {});
  assert.equal(env.state().choice.catalogs.chatgpt, null);
  assert.match(text(env.state().choice.catalogErrors.chatgpt), /Chưa tải được danh sách model/);
  assert.equal(env.state().choice.loading, false);
  assert.equal(selectView(env.state()).choice.planHidden, false);
});
test("a failed saved-text read says why", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("copy-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 500, { detail: "boom" });
  assert.ok(text(notes(env).actionMessage) !== "" && !/Đang đọc/.test(text(notes(env).actionMessage)));
});
test("copy success and failure each say so", async () => {
  const ok = setup();
  await opened(ok, { revisions: [revision()] });
  ok.hostResults.copy = async () => true;
  ok.dispatch("copy-clicked");
  await ok.answer(ok.is("GET", "/api/notes/r1"), 200, revision());
  await tick();
  assert.match(text(notes(ok).actionMessage), /Đã sao chép/);
  const bad = setup();
  await opened(bad, { revisions: [revision()] });
  bad.hostResults.copy = async () => { throw new Error("denied"); };
  bad.dispatch("copy-clicked");
  await bad.answer(bad.is("GET", "/api/notes/r1"), 200, revision());
  await tick();
  assert.match(text(notes(bad).actionMessage), /clipboard/);
});
test("export of a clean draft downloads the newer saved text", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("export-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "newer", edit_version: 2 }));
  assert.deepEqual(env.hostCalls.find(([name]) => name === "download"), ["download", "Weekly.md", "newer"]);
});
test("a run for the saved Antigravity choice sends that provider", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.answer(env.is("GET", "/api/providers"), 200, { providers: [{ id: "chatgpt", status: "disconnected" }, { id: "antigravity", status: "connected" }] });
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, { provider: "antigravity", models: { antigravity: "g1" }, reasoning_effort: "default" });
  await env.answer(env.is("GET", "/api/providers/antigravity/models"), 200, { models: [{ id: "g1", display_name: "G1" }] });
  env.dispatch("start-clicked", { requestId: "req-aaaa-0001" });
  assert.equal(posts(env, "/notes-runs")[0].body.provider, "antigravity");
});
test("the choice stays loading until every catalog answered", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.answer(env.is("GET", "/api/providers"), 200, { providers: [{ id: "chatgpt", status: "connected" }, { id: "antigravity", status: "connected" }] });
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(env.state().choice.loading, true);
  await env.answer(env.is("GET", "/api/providers/antigravity/models"), 200, { models: [] });
  assert.equal(env.state().choice.loading, false);
});
test("an unknown saved provider is not adopted", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, { ...PREFS, provider: "bogus" });
  assert.equal(env.state().choice.preferences.provider, null);
});
test("a context after a failed choice load loads it again", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody());
  await env.fail(env.is("GET", "/api/providers"));
  assert.equal(env.state().choice.loaded, false);
  const count = () => env.requests.filter((r) => r.url.endsWith("/api/providers")).length;
  const before = count();
  env.context();
  assert.equal(count(), before + 1);
});
test("an unknown provider pick changes nothing; the toggle opens and closes", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("provider-picked", { provider: "nope" });
  assert.equal(env.state().choice.localProvider, null);
  env.dispatch("change-toggled");
  env.dispatch("change-toggled");
  assert.equal(env.state().choice.open, false);
});
test("a preference saved during a run says it applies to the next run", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("model-picked", { model: "m1" });
  await env.answer(env.is("PUT", "/api/notes-preferences"), 200, { provider: "chatgpt", models: { chatgpt: "m1" } });
  assert.match(text(env.state().choice.message), /tác vụ đang chạy giữ/);
});
test("a location for another meeting does not open a source (real source href)", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("location-changed", { href: ORIGIN + sourceHref("m2", "s9", "seg1") });
  assert.equal(env.requests.filter((r) => r.url.includes("/api/notes-sources/")).length, 0);
  env.dispatch("location-changed", { href: ORIGIN + sourceHref("m1", "s9", "seg1") });
  assert.equal(env.requests.filter((r) => r.url.includes("/api/notes-sources/")).length, 1);
});
test("a source dated differently from the meeting says so; a failed source bumps the focus", async () => {
  const env = setup();
  await opened(env);
  const ref = { meetingId: "m1", snapshotId: "s9", segmentId: "seg1" };
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.dispatch("source-opened", { reference: ref, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s9"), 200, { id: "s9", meeting_id: "m1", generation_id: "g1", meeting_date: "2026-10-19", speakers: [], segments: [{ id: "seg1", start_seconds: 12, text: "hello", speaker_id: "s1" }] });
  const view = selectView(env.state()).source;
  assert.ok(view.difference.some((part) => /Ngày họp hiện tại/.test(text(part))));
  const focus = env.state().source.focus;
  env.dispatch("source-opened", { reference: { ...ref, snapshotId: "s10" }, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s10"), 500, { detail: "x" });
  assert.equal(env.state().source.focus, focus + 1);
});
test("a 409 save shows no conflict until the latest text is read", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, { detail: "version" });
  assert.equal(saved(env).conflict, null);
  assert.equal(selectView(env.state()).savedWork.conflict, null);
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "theirs", edit_version: 5 }));
  assert.equal(saved(env).conflict.editVersion, 5);
});

test("a source-shown timer of a closed source does nothing", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  env.dispatch("source-closed", { href: null });
  const mark = env.hostCalls.length;
  env.fire(0);
  await tick();
  assert.ok(!env.hostCalls.slice(mark).some(([name]) => name === "history.seekNotesSource"));
});

test("a snapshot answer after the meeting switched is ignored", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("source-opened", { reference: reference("s7"), fromClick: false });
  env.context(meeting("m2"));
  const mark = env.hostCalls.length;
  await env.answer(env.is("GET", "/api/notes-sources/s7"), 200, SNAPSHOT("s7"));
  assert.equal(env.state().source.current, null);
  assert.equal(env.hostCalls.length, mark);
});

test("a notes read in flight when the list was opened is ignored", async () => {
  const env = setup();
  env.context();
  env.dispatch("context", { detail: { view: "list", meeting: null }, stored: {}, href: `${ORIGIN}/` });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  assert.equal(env.state().meetingId, null);
  assert.equal(notes(env, "m1")?.revisions?.length ?? 0, 0);
});

// ---- failing paths ----

test("a failed poll read of a meeting that was left schedules no new poll", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.fire(1000);
  env.context(meeting("m2"));
  await env.fail(env.is("GET", "/api/meetings/m1/notes"));
  assert.ok(!env.live().includes(3000));
  assert.equal(notes(env, "m1").readError, "");
});

test("a visibility change reschedules the poll at the new pace", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  assert.deepEqual(env.live(), [1000]);
  env.dispatch("visibility-changed", { hidden: true });
  assert.deepEqual(env.live(), [3000]);
  env.dispatch("visibility-changed", { hidden: false });
  assert.deepEqual(env.live(), [1000]);
});

test("source answers carrying an old id change nothing", async () => {
  const env = setup();
  await opened(env);
  env.hostResults["history.seekNotesSource"] = async () => true;
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 200, SNAPSHOT("s1"));
  const before = env.state().source;
  const mark = env.hostCalls.length;
  for (const type of ["source-sought", "source-played", "source-failed"]) {
    env.dispatch(type, { meta: { id: before.id - 1 }, value: true, error: { message: "late" } });
  }
  assert.deepEqual(env.state().source, before);
  assert.equal(env.hostCalls.length, mark);
});

test("a failed snapshot read for a replaced source is dropped; for the current one it says so", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("source-opened", { reference: reference("s1"), fromClick: false });
  env.dispatch("source-opened", { reference: reference("s2"), fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s1"), 404, { error: "gone" });
  assert.match(text(env.state().source.status), /Đang tải/);
  await env.answer(env.is("GET", "/api/notes-sources/s2"), 404, { error: "gone" });
  assert.equal(env.state().source.current, null);
  assert.doesNotMatch(text(env.state().source.status), /Đang tải/);
});

test("hasUnsavedDraft looks at every draft, not only the open one", () => {
  const clean = { value: "a", baselineMarkdown: "a" };
  const dirty = { value: "b", baselineMarkdown: "a" };
  assert.equal(hasUnsavedDraft({ drafts: { r1: clean, r2: dirty } }), true);
  assert.equal(hasUnsavedDraft({ drafts: { r1: clean, r2: clean } }), false);
});

test("opening another meeting forgets what the server said about the last one's notes and lock", async () => {
  const env = setup();
  await opened(env);
  assert.equal(notes(env).known, true);
  assert.equal(notes(env).sourceLockKnown, true);
  env.context(meeting("m2"));
  env.context({ ...meeting("m1"), notes_source_locked: undefined });
  assert.equal(notes(env, "m1").known, false);
  assert.equal(notes(env, "m1").sourceLockKnown, false);
});

test("workspace authentication failure ends notes polling without dropping drafts or pending work", () => {
  const state = {
    ...initialState, view: "meeting", meetingId: "m1", readId: 1,
    poll: { token: 2, scheduled: false },
    drafts: { r1: { value: "Unsaved notes" } },
    pending: { m1: { request_id: "still-owned-by-worker" } },
  };
  const action = {
    type: "notes/request-failed",
    meta: { op: "read", id: 1, meetingId: "m1", poll: 2 },
    error: { status: 401, body: { detail: "Authentication required" } },
  };
  const expired = reduce(state, action);
  assert.ok(expired.effects.some(effect => effect.type === "navigate" && effect.url === "/access"));
  assert.equal(expired.state.poll.scheduled, false);
  assert.deepEqual(expired.state.drafts, state.drafts);
  assert.deepEqual(expired.state.pending, state.pending);
  const provider = reduce(state, {
    ...action, error: { status: 401, body: { detail: { code: "provider_auth_error" } } },
  });
  assert.ok(!provider.effects.some(effect => effect.type === "navigate"));
  assert.deepEqual(provider.state.choice.status, state.choice.status);
});

test("rich text runs edit only their source range and cancel restores the selected baseline", async () => {
  const env = setup();
  const markdown = "# Weekly\r\n\r\nKeep \\*literal\\* and **ship**.\r\n\r\n"
    + "3) First\r\n4) Second\r\n\r\n| Key | Value |\r\n| --- | ---: |\r\n| A | [docs](https://example.test/a?x=1&y=2) |\r\n";
  await opened(env, { revisions: [revision({ markdown }), revision({ id: "r2", markdown: "# Older" })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const work = selectView(env.state()).savedWork;
  assert.equal(work.mode, "rich");
  assert.equal(work.blocks.find((block) => block.kind === "list").start, 3);
  assert.equal(work.blocks.find((block) => block.kind === "table").align[1], "right");
  const runs = work.blocks.flatMap(blockRuns);
  const firstEdit = () => env.dispatch("rich-edited", {
    index: runs.findIndex((node) => node.text === "ship"), before: "ship", value: "ship now",
  });
  firstEdit();
  assert.equal(saved(env).value, markdown.replace("**ship**", "**ship now**"));
  assert.equal(selectView(env.state()).savedWork.mode, "rich");
  env.dispatch("revision-picked", { id: "r2" });
  assert.equal(selectView(env.state()).savedWork.draftValue, "# Older");
  env.dispatch("revision-picked", { id: "r1" });
  env.dispatch("edit-canceled");
  assert.equal(saved(env).value, markdown);
  assert.equal(env.state().mode, "view");
  assert.equal(hasUnsavedDraft(env.state()), false);
  env.dispatch("mode-chosen", { mode: "rich" });
  firstEdit();
  env.dispatch("save-clicked");
  const save = env.waiting(env.is("PATCH", "/api/notes/r1"))[0];
  assert.deepEqual(save.body, { expected_edit_version: 1, markdown: markdown.replace("**ship**", "**ship now**") });
  await env.answer(env.is("PATCH", "/api/notes/r1"), 409, { detail: "conflict" });
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "# Elsewhere", edit_version: 2 }));
  env.dispatch("edit-canceled");
  assert.equal(env.state().mode, "view");
  assert.equal(saved(env).value, save.body.markdown);
  assert.equal(saved(env).conflict.editVersion, 2);
});

test("rich text edits keep ordinary spaces and punctuation canonical", async () => {
  const env = setup();
  const markdown = "# Plan\n\nHello\n\n- one\n- two\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const index = runs.findIndex((node) => node.text === "Hello");
  env.dispatch("rich-edited", { index, before: "Hello", value: "Hello world (draft)." });
  assert.equal(saved(env).value, markdown.replace("Hello", "Hello world (draft)."));
  assert.doesNotMatch(saved(env).value, /&#32;|\\[().]/);
});

test("rich appends preserve authored entities and normalize only generated trailing-space suffixes", async () => {
  const env = setup();
  const markdown = "# Notes\n\nOne&#32;point at noon\n\nPlan&#32;\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  let runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const paragraphIndex = runs.findIndex((node) => node.text === "One point at noon");
  env.dispatch("rich-edited", {
    index: paragraphIndex, before: "One point at noon", value: "One point at noon today",
  });
  assert.equal(saved(env).value, "# Notes\n\nOne&#32;point at noon today\n\nPlan&#32;\n");
  runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const trailingIndex = runs.findIndex((node) => node.text === "Plan ");
  env.dispatch("rich-edited", { index: trailingIndex, before: "Plan ", value: "Plan three" });
  assert.equal(saved(env).value, "# Notes\n\nOne&#32;point at noon today\n\nPlan&#32;three\n");

  const generated = setup();
  await opened(generated, { revisions: [revision({ markdown: "# Notes\n\nPlan\n" })] });
  generated.dispatch("mode-chosen", { mode: "rich" });
  let generatedRuns = selectView(generated.state()).savedWork.blocks.flatMap(blockRuns);
  const planIndex = generatedRuns.findIndex((node) => node.text === "Plan");
  generated.dispatch("rich-edited", { index: planIndex, before: "Plan", value: "Plan " });
  assert.equal(saved(generated).value, "# Notes\n\nPlan&#32;\n");
  generatedRuns = selectView(generated.state()).savedWork.blocks.flatMap(blockRuns);
  const planWithSpaceIndex = generatedRuns.findIndex((node) => node.text === "Plan ");
  generated.dispatch("rich-edited", { index: planWithSpaceIndex, before: "Plan ", value: "Plan today" });
  assert.equal(saved(generated).value, "# Notes\n\nPlan today\n");
});

test("rich appends preserve authored space and tab entities after the prefix changes", async () => {
  for (const entity of ["&#32;", "&#9;"]) {
    const env = setup();
    const markdown = `# Notes\n\nPlan${entity}\n`;
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    let runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const planIndex = runs.findIndex((node) => node.text === `Plan${entity === "&#32;" ? " " : "\t"}`);
    env.dispatch("rich-edited", {
      index: planIndex,
      before: `Plan${entity === "&#32;" ? " " : "\t"}`,
      value: `Blan${entity === "&#32;" ? " " : "\t"}`,
    });
    runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const changedIndex = runs.findIndex((node) => node.text === `Blan${entity === "&#32;" ? " " : "\t"}`);
    env.dispatch("rich-edited", {
      index: changedIndex,
      before: `Blan${entity === "&#32;" ? " " : "\t"}`,
      value: `Blan${entity === "&#32;" ? " " : "\t"}today`,
    });
    assert.equal(saved(env).value, `# Notes\n\nBlan${entity}today\n`);
  }

  const inserted = setup();
  const markdown = "# Notes\n\nPlan&#32;\n";
  await opened(inserted, { revisions: [revision({ markdown })] });
  inserted.dispatch("mode-chosen", { mode: "rich" });
  let runs = selectView(inserted.state()).savedWork.blocks.flatMap(blockRuns);
  const planIndex = runs.findIndex((node) => node.text === "Plan ");
  inserted.dispatch("rich-edited", { index: planIndex, before: "Plan ", value: " Plan " });
  assert.equal(saved(inserted).value, "# Notes\n\n&#32;Plan&#32;\n");
  runs = selectView(inserted.state()).savedWork.blocks.flatMap(blockRuns);
  const insertedIndex = runs.findIndex((node) => node.text === " Plan ");
  inserted.dispatch("rich-edited", { index: insertedIndex, before: " Plan ", value: " Plan today" });
  assert.equal(saved(inserted).value, "# Notes\n\n&#32;Plan&#32;today\n");
});

test("raw edits preserve untouched CRLF and CR delimiters", async () => {
  const env = setup();
  await opened(env, { revisions: [revision({ markdown: "# A\r\n\r\nEdit me\rKeep me\n" })] });
  env.dispatch("draft-edited", { value: "# A\n\nEdit this\nKeep me\n" });
  env.dispatch("save-clicked");
  assert.equal(env.waiting(env.is("PATCH", "/api/notes/r1"))[0].body.markdown, "# A\r\n\r\nEdit this\rKeep me\n");
});

test("unsupported inline tokens leave surrounding source links and stored citation proof intact in View", async () => {
  const env = setup();
  const href = "/meetings/m1?notes_source=s1&segment=seg1#transcript";
  const bullet = "The launch plan.";
  const markdown = "# Plan\n\n  - The launch plan.\n\n"
    + `[Before](${href}) plus [literal fragment] plus [After](${href}) and ~~struck~~\n\n`
    + "```md\n# Plan\n- The launch plan.\n" + `[In code](${href})\n` + "```"
    + `\n\n<script>\n\n# Plan\n- The launch plan.\n\n[In unsafe HTML](${href})\n</script>`;
  await opened(env, { revisions: [revision({ markdown, citations: [{ section: "Plan", text: bullet, sources: [{ id: "seg1", start_seconds: 9 }] }] })] });
  const { blocks } = selectView(env.state()).savedWork;
  assert.equal(blocks.find((block) => block.kind === "list").items[0].chips.length, 1);
  assert.deepEqual(blocks.flatMap(blockRuns).filter((node) => node.kind === "source").map((node) => node.text), ["Before", "After"]);
  assert.ok(blocks.flatMap(blockRuns).some((node) => node.kind === "raw" && node.text === "[literal fragment]"));
  const literal = blocks.filter((block) => block.kind === "raw").map((block) => block.text).join("\n");
  assert.match(literal, /\[In code\]/);
  assert.match(literal, /<script>[\s\S]*\[In unsafe HTML\][\s\S]*<\/script>/);
  env.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(env.state()).savedWork.mode, "write");
  assert.equal(selectView(env.state()).savedWork.richFallback, true);
  assert.equal(saved(env).value, markdown);
  env.dispatch("rich-edited", { index: 0, before: "Plan", value: "Wrong" });
  assert.equal(saved(env).value, markdown);
});

test("citation matching treats raw headings and indented code as hard boundaries", async () => {
  const env = setup();
  const bullet = "The team reviewed its launch plan.";
  const markdown = "# Meeting Notes\n\n## Overview\n\nNothing recorded.\n\n## Elsewhere  \n\n- " + `${bullet}\n`;
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg1", start_seconds: 9 }] }],
  })] });
  const view = selectView(env.state()).savedWork;
  assert.equal(view.blocks.find((block) => block.kind === "list").items[0].chips.length, 0);
  const code = "# Notes\n\n    | A | B |\n    | --- | --- |\n    | x | y |\n";
  env.dispatch("draft-edited", { value: code });
  assert.equal(selectView(env.state()).savedWork.blocks[1].kind, "raw");
  env.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(env.state()).savedWork.mode, "write");
  assert.equal(selectView(env.state()).savedWork.richFallback, true);
});

test("View keeps a safe authored source link visible through a Markdown hard break", async () => {
  const env = setup();
  const href = "/meetings/m1?notes_source=s1&segment=seg1#transcript";
  const markdown = `# Notes\n\n[Before break](${href})  \nNext line.\n`;
  await opened(env, { revisions: [revision({ markdown })] });
  const raw = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  const source = raw.viewLines.flatMap(({ nodes }) => nodes).find((node) => node.kind === "source");
  assert.equal(source.text, "Before break");
  assert.equal(source.href, href);
});

test("View keeps citation parity for unsupported lists and raw heading scope", async () => {
  const env = setup();
  const markdown = "# Notes\n\n## Actions \n\n- Alpha.\n  - sub note\n- Beta. \n";
  await opened(env, { revisions: [revision({
    markdown,
    citations: [
      { section: "Actions", text: "Alpha.", sources: [{ id: "seg-a", start_seconds: 1 }] },
      { section: "Actions", text: "Beta.", sources: [{ id: "seg-b", start_seconds: 2 }] },
    ],
  })] });
  const raw = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.deepEqual(raw.viewLines.map((line) => line.chips.map((chip) => chip.segmentId)), [["seg-a"], [], ["seg-b"]]);

  const scoped = "# Notes\n\n## Decisions\n\n- Email the vendor.\n\n## Actions \n\n- Email the vendor.\n";
  const scopedEnv = setup();
  await opened(scopedEnv, { revisions: [revision({
    markdown: scoped,
    citations: [
      { section: "Decisions", text: "Email the vendor.", sources: [{ id: "seg-d", start_seconds: 1 }] },
      { section: "Actions", text: "Email the vendor.", sources: [{ id: "seg-a", start_seconds: 50 }] },
    ],
  })] });
  const lists = selectView(scopedEnv.state()).savedWork.blocks.filter((block) => block.kind === "list");
  assert.equal(lists[1].items[0].chips[0].segmentId, "seg-a");
});

test("citation scope uses the same ATX grammar as View for inline Unicode separators", async () => {
  const bullet = "The team reviewed its launch plan.";
  const markdown = "# Notes\n\n## Overview\n\n## New\u2028 topic\n\n- " + `${bullet}\n`;
  const env = setup();
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const list = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "list");
  assert.equal(list.items[0].chips.length, 0);
});

test("shared marker grammar keeps tab-delimited blocks rich and preserves unsupported indentation", async () => {
  for (const [markdown, kind] of [
    ["# Notes\n\n##\tTopic\u2028 inline\n", "heading"],
    ["# Notes\n\n- \tTopic\u2028 inline\n", "list"],
    ["# Notes\n\n1.\tTopic\u2028 inline\n", "list"],
  ]) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    assert.equal(selectView(env.state()).savedWork.mode, "rich");
    assert.ok(selectView(env.state()).savedWork.blocks.some((block) => block.kind === kind));
  }

  const env = setup();
  const markdown = "# Notes\n\n- Exact statement\n  Supplemental context belongs in View.\n\n## Next\n";
  await opened(env, { revisions: [revision({ markdown })] });
  const raw = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.equal(raw.text, "- Exact statement\n  Supplemental context belongs in View.");
  assert.deepEqual(raw.viewLines.map(({ nodes }) => nodes.map((node) => node.text).join("")), [
    "- Exact statement",
    "  Supplemental context belongs in View.",
  ]);
});

test("ATX scope resets for unsupported headings and closing hashes stay raw", async () => {
  const bullet = "The team reviewed its launch plan.";
  for (const boundary of ["  ## \tOther\u2028 topic", "##\t"]) {
    const env = setup();
    const markdown = `# Notes\n\n## Overview\n\n${boundary}\n\n- ${bullet}\n`;
    await opened(env, { revisions: [revision({
      markdown,
      citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
    })] });
    const list = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "list");
    assert.equal(list.items[0].chips.length, 0);
  }

  const env = setup();
  const markdown = "# Notes\n\n##\tTopic\u2029 end\t##\n";
  await opened(env, { revisions: [revision({ markdown })] });
  assert.ok(selectView(env.state()).savedWork.blocks.some((block) => block.kind === "raw" && block.text === "##\tTopic\u2029 end\t##"));
  env.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(env.state()).savedWork.richFallback, true);
  assert.equal(saved(env).value, markdown);
});

test("View keeps source and citation parity through quote, tab-list and nested code boundaries", async () => {
  const env = setup();
  const href = "/meetings/m1?notes_source=s1&segment=seg-a#transcript";
  const bullet = "The team reviewed its launch plan.";
  const markdown = "# Notes\n\n"
    + "> [Before](" + href + ")\n\n"
    + "## Overview\n\n"
    + "- Other bullet.\n\n"
    + "  ```text\n\n"
    + "  - " + bullet + "\n  [In code](" + href + ")\n"
    + "  ```\n\n"
    + "-\t" + bullet + "\n";
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const view = selectView(env.state()).savedWork;
  const source = view.blocks
    .filter((block) => block.kind === "raw" && block.viewLines)
    .flatMap((block) => block.viewLines)
    .flatMap((line) => line.nodes)
    .find((node) => node.kind === "source");
  assert.equal(source.text, "Before");
  const codeRaw = view.blocks.find((block) => block.kind === "raw" && block.text.includes("[In code]"));
  assert.ok(codeRaw);
  assert.equal(codeRaw.viewLines.flatMap(({ nodes }) => nodes).filter((node) => node.kind === "source").length, 0);
  const list = view.blocks.find((block) => block.kind === "list");
  assert.equal(list.items[0].chips[0].segmentId, "seg-a");
});

test("View skips code for citation matching and rich text keeps heading markers literal", async () => {
  const env = setup();
  const bullet = "The team reviewed its launch plan.";
  const markdown = "# Meeting Notes\n\n## Overview\n\n```text\nExample code\n```\n\n- " + `${bullet}\n`;
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const list = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "list");
  assert.equal(list.items[0].chips[0].segmentId, "seg-a");

  const indented = "# Meeting Notes\n\n## Overview\n\n- Other bullet.\n\n      - " + `${bullet}\n`;
  env.dispatch("draft-edited", { value: indented });
  const raw = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.ok(raw);
  assert.deepEqual(raw.viewLines.map((line) => line.chips), [[], [], []]);

  const literal = "# Notes\n\nOrdinary text.\n";
  env.dispatch("draft-edited", { value: literal });
  env.dispatch("mode-chosen", { mode: "rich" });
  const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  env.dispatch("rich-edited", {
    index: runs.findIndex((node) => node.text === "Ordinary text."),
    before: "Ordinary text.",
    value: "## Literal text",
  });
  assert.equal(saved(env).value, "# Notes\n\n\\## Literal text\n");
  assert.equal(selectView(env.state()).savedWork.blocks.filter((block) => block.kind === "heading").length, 1);
  assert.equal(selectView(env.state()).savedWork.blocks.find((block) => block.kind === "heading").level, 1);

  const crEnv = setup();
  const crlf = "# Notes\r\rOrdinary\r";
  await opened(crEnv, { revisions: [revision({ markdown: crlf })] });
  crEnv.dispatch("mode-chosen", { mode: "rich" });
  const crRuns = selectView(crEnv.state()).savedWork.blocks.flatMap(blockRuns);
  crEnv.dispatch("rich-edited", {
    index: crRuns.findIndex((node) => node.text === "Ordinary"),
    before: "Ordinary",
    value: "> Literal",
  });
  assert.equal(saved(crEnv).value, "# Notes\r\r\\> Literal\r");
  assert.equal(selectView(crEnv.state()).savedWork.blocks[1].kind, "paragraph");
});

test("rich marker prefixes stay literal when typed incrementally", async () => {
  const expected = new Map([
    ["## Literal", "\\## Literal"],
    ["1. Literal", "1\\. Literal"],
    ["+ Literal", "\\+ Literal"],
    ["> Literal", "\\> Literal"],
  ]);
  for (const value of expected.keys()) {
    const env = setup();
    const markdown = "# Notes\n\nOriginal paragraph\n";
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    let before = "Original paragraph";
    let current = "";
    for (const character of value) {
      current += character;
      const index = selectView(env.state()).savedWork.blocks.flatMap(blockRuns)
        .findIndex((node) => node.text === before);
      env.dispatch("rich-edited", { index, before, value: current });
      const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
      assert.equal(runs[index].text, current);
      before = current;
    }
    assert.equal(saved(env).value, `${markdown.slice(0, -"Original paragraph\n".length)}${expected.get(value)}\n`);
  }
});

test("rich boundary spaces stay editable in links and code", async () => {
  const env = setup();
  const href = "https://example.invalid/agenda";
  const markdown = "# Notes\n\n[Open](" + href + ") and `npm`.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  let runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const linkIndex = runs.findIndex((node) => node.kind === "link" && node.text === "Open");
  env.dispatch("rich-edited", { index: linkIndex, before: "Open", value: "Open " });
  assert.equal(saved(env).value, "# Notes\n\n[Open&#32;](" + href + ") and `npm`.\n");
  runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const linkWithSpace = runs.findIndex((node) => node.kind === "link" && node.text === "Open ");
  env.dispatch("rich-edited", { index: linkWithSpace, before: "Open ", value: "Open agenda" });
  assert.equal(saved(env).value, "# Notes\n\n[Open agenda](" + href + ") and `npm`.\n");
  runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const codeIndex = runs.findIndex((node) => node.kind === "code" && node.text === "npm");
  env.dispatch("rich-edited", { index: codeIndex, before: "npm", value: "npm " });
  assert.equal(saved(env).value, "# Notes\n\n[Open agenda](" + href + ") and `npm `.\n");
  runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const codeWithSpace = runs.findIndex((node) => node.kind === "code" && node.text === "npm ");
  env.dispatch("rich-edited", { index: codeWithSpace, before: "npm ", value: "npm ci" });
  assert.equal(saved(env).value, "# Notes\n\n[Open agenda](" + href + ") and `npm ci`.\n");
});

test("rich intraword emphasis rejects boundary whitespace that breaks CommonMark", async () => {
  const env = setup();
  const markdown = "# Notes\n\nHello**a b**world.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const index = runs.findIndex((node) => node.kind === "strong" && node.text === "a b");
  env.dispatch("rich-edited", { index, before: "a b", value: " b" });
  assert.equal(saved(env).value, markdown);
  assert.equal(selectView(env.state()).savedWork.blocks.flatMap(blockRuns)[index].text, "a b");
});

test("rich intraword emphasis accepts ordinary edge letters", async () => {
  const env = setup();
  const markdown = "# Notes\n\nHello**bold**world.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const index = runs.findIndex((node) => node.kind === "strong" && node.text === "bold");
  env.dispatch("rich-edited", { index, before: "bold", value: "boldX" });
  assert.equal(saved(env).value, "# Notes\n\nHello**boldX**world.\n");
});

test("rich intraword emphasis rejects punctuation at an edited delimiter edge", async () => {
  const env = setup();
  const markdown = "# Notes\n\nHello**bold**world.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const edit = (value) => {
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.kind === "strong" && node.text === "bold");
    env.dispatch("rich-edited", { index, before: "bold", value });
    assert.equal(saved(env).value, markdown);
  };
  for (const value of ["?bold", "bold?", "&bold", "bold&", " bold", "bold ", "€bold", "bold€", "→bold", "bold😀"]) edit(value);
});

test("rich adjacent plain-text edits cannot invalidate emphasis delimiters", async () => {
  const cases = [
    ["# Notes\n\nHello **?bold**world.\n", "Hello ", "Hello"],
    ["# Notes\n\nHello**bold?** world\n", " world", "world"],
    ["# Notes\n\nHello **&#32;bold**world\n", "Hello ", "Hello"],
    ["# Notes\n\nHello**bold&#32;** world\n", " world", "world"],
    ["# Notes\n\nHello _bold_ world\n", "Hello ", "Hello"],
    ["# Notes\n\nHello _bold_ world\n", " world", "world"],
    ["# Notes\n\nHello __bold__ world\n", "Hello ", "Hello"],
    ["# Notes\n\nHello __bold__ world\n", " world", "world"],
  ];
  for (const [markdown, before, value] of cases) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.kind === "text" && node.text === before);
    assert.notEqual(index, -1, `missing adjacent run ${before}`);
    env.dispatch("rich-edited", { index, before, value });
    assert.equal(saved(env).value, markdown);
    assert.equal(selectView(env.state()).savedWork.blocks.flatMap(blockRuns)[index].text, before);
  }
});

test("rich emphasis edge checks use the active delimiter run", async () => {
  const cases = [
    ["# Notes\n\nIntro *one*__two__ end\n", "two", "?two"],
    ["# Notes\n\nIntro **one**_two_ end\n", "two", "?two"],
    ["# Notes\n\nIntro _one_**two** end\n", "two", "?two"],
    ["# Notes\n\nIntro *one*__two__ end\n", "one", "one?"],
  ];
  for (const [markdown, before, value] of cases) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.kind !== "text" && node.text === before);
    assert.notEqual(index, -1, `missing emphasis run ${before}`);
    env.dispatch("rich-edited", { index, before, value });
    assert.equal(saved(env).value, markdown.replace(before, value));
  }
});

test("rich emphasis parsing uses CommonMark whitespace boundaries", async () => {
  const cases = ["\u2028bold", "bold\u2028", "\u2029bold", "\ufeffbold", "\u000bbold"];
  for (const value of cases) {
    const env = setup();
    const markdown = "# Notes\n\nHello *bold* world\n";
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.kind === "em" && node.text === "bold");
    assert.notEqual(index, -1);
    env.dispatch("rich-edited", { index, before: "bold", value });
    assert.equal(saved(env).value, markdown.replace("*bold*", `*${value}*`));
  }
});

test("rich emphasis accepts encoded boundary spaces beside Unicode symbols", async () => {
  const cases = [
    ["# Notes\n\n🦉**bold**world.\n", " bold", "# Notes\n\n🦉**&#32;bold**world.\n"],
    ["# Notes\n\nHello**bold**🦉.\n", "bold ", "# Notes\n\nHello**bold&#32;**🦉.\n"],
  ];
  for (const [markdown, value, expected] of cases) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => ["strong", "em"].includes(node.kind) && node.text === "bold");
    assert.notEqual(index, -1);
    env.dispatch("rich-edited", { index, before: "bold", value });
    assert.equal(saved(env).value, expected);
  }
});

test("rich deletion encodes newly exposed boundary spaces without rewriting other bytes", async () => {
  const env = setup();
  const markdown = "# Plan A\n\n- one two\n3. one two\n\n| A | B |\n|---|---|\n| x y | z |\n\n**a b**\n\nab words here.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const edit = (before, value) => {
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.text === before);
    assert.notEqual(index, -1, `missing run ${before}`);
    env.dispatch("rich-edited", { index, before, value });
    assert.equal(selectView(env.state()).savedWork.blocks.flatMap(blockRuns)[index].text, value);
  };
  edit("Plan A", "Plan ");
  edit("one two", "one ");
  edit("one two", "one ");
  edit("x y", "x ");
  edit("a b", "a ");
  edit("ab words here.", " words here.");
  assert.equal(
    saved(env).value,
    "# Plan&#32;\n\n- one&#32;\n3. one&#32;\n\n| A | B |\n|---|---|\n| x&#32; | z |\n\n**a&#32;**\n\n&#32;words here.\n",
  );
});

test("rich marker protection applies at list-item content starts", async () => {
  const env = setup();
  const markdown = "# Notes\n\n- one\n3. two\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  let runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const bulletIndex = runs.findIndex((node) => node.text === "one");
  env.dispatch("rich-edited", { index: bulletIndex, before: "one", value: "## one" });
  assert.equal(saved(env).value, "# Notes\n\n- \\## one\n3. two\n");
  runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const orderedIndex = runs.findIndex((node) => node.text === "two");
  env.dispatch("rich-edited", { index: orderedIndex, before: "two", value: "1. two" });
  assert.equal(saved(env).value, "# Notes\n\n- \\## one\n3. 1\\. two\n");
});

test("rich edits preserve heading and list blocks containing U+2028/U+2029", async () => {
  const cases = [
    ["# Notes\n\n## Decisions\u2028 today\n\n- item\n", "Decisions\u2028 today", "Decisions\u2028 todays", "heading", 2],
    ["# Notes\n\n1. Alice owns\u2028 budget\n2. tail\n", "Alice owns\u2028 budget", "Alice owns\u2028 budgets", "list", 1],
    ["# Notes\n\n- Alice\u2029 owns\n- tail\n", "Alice\u2029 owns", "Alice\u2029 ownss", "list", 1],
  ];
  for (const [markdown, before, value, kind, level] of cases) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const work = selectView(env.state()).savedWork;
    const block = work.blocks.find((candidate) => candidate.kind === kind
      && blockRuns(candidate).some((node) => node.text === before));
    assert.ok(block);
    if (kind === "heading") assert.equal(block.level, level);
    else assert.equal(block.items.length, 2);
    const runs = work.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.text === before);
    assert.notEqual(index, -1);
    env.dispatch("rich-edited", { index, before, value });
    assert.equal(saved(env).value, markdown.replace(before, value));
    assert.equal(selectView(env.state()).savedWork.blocks.find((candidate) => candidate.kind === kind).kind, kind);
  }
});

test("rich edits preserve list blocks with mixed space-tab marker padding", async () => {
  const cases = [
    "# Notes\n\n- \tfoo\n",
    "# Notes\n\n1. \tfoo\n",
    "# Notes\n\n-  \tfoo\n",
    "# Notes\n\n-    foo\n",
    "# Notes\n\n+ \tfoo\n",
    "# Notes\n\n1) \tfoo\n",
  ];
  for (const markdown of cases) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const work = selectView(env.state()).savedWork;
    assert.equal(work.mode, "rich");
    assert.equal(work.blocks.find((block) => block.kind === "list").items.length, 1);
    const runs = work.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.text === "foo");
    assert.notEqual(index, -1);
    env.dispatch("rich-edited", { index, before: "foo", value: "foox" });
    assert.equal(saved(env).value, markdown.replace("foo", "foox"));
    assert.equal(selectView(env.state()).savedWork.blocks.find((block) => block.kind === "list").kind, "list");
  }
});

test("list padding beyond four columns stays literal and out of citation scope", async () => {
  const env = setup();
  const markdown = "# Notes\n\n- \t    The team reviewed its launch plan.\n";
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Notes", text: "The team reviewed its launch plan.", sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const view = selectView(env.state()).savedWork;
  const raw = view.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.ok(raw);
  assert.deepEqual(raw.viewLines[0].nodes, [{ kind: "raw", text: "- \t    The team reviewed its launch plan." }]);
  assert.deepEqual(raw.viewLines[0].chips, []);
  env.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(env.state()).savedWork.richFallback, true);
  assert.equal(saved(env).value, markdown);
});

test("raw boundaries reset heading scope and keep decreasing-indent code literal", async () => {
  const bullet = "The team reviewed its launch plan.";
  const scoped = setup();
  const scopedMarkdown = `# Notes\n\n## Overview\n\n    Indented log entry.\n  ## Other\u2028 topic\n\n- ${bullet}\n`;
  await opened(scoped, { revisions: [revision({
    markdown: scopedMarkdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const scopedList = selectView(scoped.state()).savedWork.blocks.find((block) => block.kind === "list");
  assert.equal(scopedList.items[0].chips.length, 0);

  const literal = setup();
  const literalMarkdown = `# Notes\n\n## Overview\n\n  - \t    **literal**\n- ${bullet}\n`;
  await opened(literal, { revisions: [revision({
    markdown: literalMarkdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const raw = selectView(literal.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.deepEqual(raw.viewLines[0].nodes, [{ kind: "raw", text: "  - \t    **literal**" }]);
  assert.equal(raw.viewLines[0].chips.length, 0);
  assert.equal(raw.viewLines[1].chips[0].segmentId, "seg-a");
});

test("unsupported loose lists keep legacy citation membership", async () => {
  const env = setup();
  const bullet = "The team reviewed its launch plan.";
  const markdown = `# Notes\n\n## Overview\n\n  - Context\n\n  - ${bullet}\n`;
  await opened(env, { revisions: [revision({
    markdown,
    citations: [{ section: "Overview", text: bullet, sources: [{ id: "seg-a", start_seconds: 1 }] }],
  })] });
  const raw = selectView(env.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.ok(raw);
  assert.equal(raw.viewLines[2].chips[0].segmentId, "seg-a");
});

test("unsupported loose lists use the recognized item range for unique and duplicate citation proof", async () => {
  const citation = { section: "Tasks", text: "Confirm", sources: [{ id: "seg-a", start_seconds: 1 }] };
  const unique = setup();
  await opened(unique, { revisions: [revision({
    markdown: "# Tasks\n - Confirm\n- Review\n  continuation\n",
    citations: [citation],
  })] });
  const uniqueRaw = selectView(unique.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.equal(uniqueRaw.viewLines[0].chips.length, 1);
  assert.equal(uniqueRaw.viewLines[1].chips.length, 0);

  const duplicate = setup();
  await opened(duplicate, { revisions: [revision({
    markdown: "# Tasks\n - Confirm\n- Confirm\n  continuation\n",
    citations: [citation],
  })] });
  const duplicateRaw = selectView(duplicate.state()).savedWork.blocks.find((block) => block.kind === "raw" && block.viewLines);
  assert.deepEqual(duplicateRaw.viewLines.slice(0, 2).map((line) => line.chips), [[], []]);
});

test("a list item containing a heading stays raw for rich editing and formatted clipboard", async () => {
  const env = setup();
  const markdown = "- # Agenda\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const work = selectView(env.state()).savedWork;
  assert.equal(work.mode, "write");
  assert.equal(work.richFallback, true);
  assert.equal(work.draftValue, markdown);

  env.dispatch("rich-preview-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown, edit_version: 2 }));
  env.hostResults.copyFormatted = async () => "html";
  env.dispatch("preview-copy-clicked");
  await tick();
  const copy = env.hostCalls.find(([name]) => name === "copyFormatted");
  assert.ok(copy);
  assert.match(copy[1], /<pre[^>]*>- # Agenda<\/pre>/);
  assert.doesNotMatch(copy[1], /<li># Agenda<\/li>/);
});

test("stale saved reads cannot update revisions, copy, export or send an expired-session redirect after switching away and back", async () => {
  const env = setup();
  await opened(env, { revisions: [revision(), revision({ id: "r2" })] });
  const metaFor = (action) => reduce(env.state(), { type: `notes/${action}-clicked` }).effects.find((effect) => effect.type === "fetch").meta;
  const oldRevision = metaFor("copy");
  const oldMeeting = metaFor("export");
  env.dispatch("copy-clicked");
  const copy = env.waiting(env.is("GET", "/api/notes/r1"))[0];
  env.dispatch("revision-picked", { id: "r2" });
  env.dispatch("revision-picked", { id: "r1" });
  assert.equal(copy.signal.aborted, true);
  env.context(meeting("m2"));
  env.context();
  const before = env.state();
  for (const meta of [oldRevision, oldMeeting]) {
    const result = reduce(before, { type: "notes/response", meta, response: { body: revision({ markdown: "# Stale", edit_version: 9 }) } });
    assert.equal(result.state, before);
    assert.equal(result.effects, undefined);
    const failure = reduce(before, { type: "notes/request-failed", meta, error: { status: 401, body: { detail: "Authentication required" } } });
    assert.equal(failure.state, before);
    assert.equal(failure.effects, undefined);
  }
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  env.dispatch("export-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "# Fresh", edit_version: 2 }));
  copy.resolve(reply(200, revision({ markdown: "# Stale", edit_version: 9 })));
  await tick();
  assert.deepEqual(env.hostCalls.filter(([name]) => name === "copy" || name === "download"), [["download", "Weekly.md", "# Fresh"]]);
  assert.equal(saved(env).value, "# Fresh");
});

test("formatted previews pin the saved text, preserve safe authored links and omit proof chips", async () => {
  const env = setup();
  const href = "/meetings/m1?notes_source=s1&segment=seg1#transcript";
  const markdown = "# Weekly\n\n## Intro\n\nHello & welcome.\n\n## Action Items\n\n"
    + `- Follow [the source](${href})\n\n[bad](javascript:alert%281%29) ![beacon](https://example.test/pixel)\n\n`
    + '<img src="https://example.test/pixel">';
  const proof = { section: "Action Items", text: `Follow [the source](${href})`, sources: [{ id: "seg2", start_seconds: 10 }] };
  const note = revision({ markdown, citations: [proof], edit_version: 2 });
  await opened(env, { revisions: [revision()] });
  env.dispatch("draft-edited", { value: "unsaved" });
  for (const action of ["copy", "export", "rich-preview", "email-preview"]) env.dispatch(`${action}-clicked`);
  assert.equal(env.waiting(env.is("GET", "/api/notes/r1")).length, 0);
  env.dispatch("edit-canceled");
  env.dispatch("email-preview-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, note);
  assert.equal(env.hostCalls.filter(([name]) => name === "copyFormatted").length, 0);
  const preview = selectView(env.state()).copyPreview.content;
  assert.equal(preview.noteId, "r1");
  assert.equal(preview.editVersion, 2);
  assert.equal(preview.subject, "Weekly");
  assert.deepEqual(preview.blocks.filter((block) => block.kind === "section").map((block) => block.section), ["intro", "actions"]);
  env.context({ ...meeting(), metadata_version: 4 });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision({ markdown: "# Newer", edit_version: 3 })]));
  env.hostResults.copyFormatted = async () => "plain";
  env.dispatch("preview-copy-clicked");
  env.dispatch("preview-copy-clicked");
  await tick();
  const writes = env.hostCalls.filter(([name]) => name === "copyFormatted");
  assert.equal(writes.length, 1);
  assert.equal(writes[0][2], markdown);
  const html = writes[0][1];
  assert.ok(html.includes('<section aria-label="Action Items">'));
  assert.ok(html.includes(`href="${ORIGIN}/meetings/m1?notes_source=s1&amp;segment=seg1#transcript"`));
  assert.ok(html.includes("&lt;img"));
  assert.doesNotMatch(html, /<img|href="javascript:|seg2|<h1>Newer|Outro/);
  assert.match(text(selectView(env.state()).copyPreview.status), /văn bản Markdown/);
});

test("a save that started after a read prevents a stale authoritative update", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("copy-clicked");
  env.dispatch("draft-edited", { value: "mine" });
  env.dispatch("save-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision({ markdown: "other", edit_version: 2 }));
  assert.equal(notes(env).revisions[0].edit_version, 1);
  assert.equal(saved(env).value, "mine");
  assert.ok(!env.hostCalls.some(([name]) => name === "copy"));
  await env.answer(env.is("PATCH", "/api/notes/r1"), 200, revision({ markdown: "mine", edit_version: 2 }));
  assert.equal(saved(env).conflict, null);
});

test("a formatted preview is dismissed when the selected content becomes unsaved", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("rich-preview-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision());
  assert.ok(selectView(env.state()).copyPreview);
  env.dispatch("draft-edited", { value: "unsaved" });
  assert.equal(selectView(env.state()).copyPreview, null);
  env.dispatch("preview-copy-clicked");
  assert.ok(!env.hostCalls.some(([name]) => name === "copyFormatted"));
});

test("leaving and returning to the Notes tab aborts only the preview and rejects its late reply", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  const action = { type: "notes/email-preview-clicked" };
  const { meta } = reduce(env.state(), action).effects.find((effect) => effect.type === "fetch");
  env.dispatch("email-preview-clicked");
  env.dispatch("export-clicked");
  const [preview, download] = env.waiting(env.is("GET", "/api/notes/r1"));
  const meetingShown = env.state().meeting;
  env.context(meetingShown, { detail: { view: "meeting", meeting: meetingShown, tab: "transcript" } });
  assert.equal(preview.signal.aborted, true);
  assert.equal(download.signal.aborted, false);
  env.context(meetingShown);
  const before = env.state();
  const response = { type: "notes/response", meta, response: { body: revision({ markdown: "# Obsolete", edit_version: 2 }) } };
  assert.equal(reduce(before, response).state, before);
  assert.equal(reduce(before, { type: "notes/request-failed", meta, error: { status: 500 } }).state, before);
  preview.resolve(reply(200, revision({ markdown: "# Obsolete", edit_version: 2 })));
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision());
  assert.equal(selectView(env.state()).copyPreview, null);
  assert.deepEqual(env.hostCalls.find(([name]) => name === "download"), ["download", "Weekly.md", "# Plan\n- one"]);
  env.dispatch("email-preview-clicked");
  await env.answer(env.is("GET", "/api/notes/r1"), 200, revision());
  assert.equal(selectView(env.state()).copyPreview.content.kind, "email");
});

test("a literal inline fragment alone forces the raw editor without changing the draft", async () => {
  const env = setup();
  const markdown = "# Plan\n\nSend [literal fragment] **soon**.";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const work = selectView(env.state()).savedWork;
  assert.equal(work.mode, "write");
  assert.equal(work.richFallback, true);
  assert.equal(work.draftValue, markdown);
});

test("rich fallback stays in Write through a repair and resets for a new revision", async () => {
  const env = setup();
  const unsupported = "# Plan\n\nSend [literal fragment] **soon**.";
  await opened(env, { revisions: [
    revision({ markdown: unsupported }),
    revision({ id: "r2", markdown: "# Ready" }),
  ] });
  env.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(env.state()).savedWork.mode, "write");
  assert.equal(env.state().richFallback, true);
  env.dispatch("draft-edited", { value: "# Plan\n\nRepaired" });
  assert.equal(selectView(env.state()).savedWork.mode, "write");
  assert.equal(env.state().richFallback, true);

  env.dispatch("revision-picked", { id: "r2" });
  assert.equal(env.state().richFallback, false);
  assert.equal(selectView(env.state()).savedWork.mode, "rich");
  env.dispatch("revision-picked", { id: "r1" });
  assert.equal(selectView(env.state()).savedWork.mode, "rich");
  assert.equal(env.state().richFallback, false);

  env.context(meeting("m2"));
  assert.equal(env.state().richFallback, false);
  await env.answer(env.is("GET", "/api/meetings/m2/notes"), 200, notesBody([
    revision({ id: "r3", meeting_id: "m2", markdown: "# New meeting" }),
  ]));
  assert.equal(selectView(env.state()).savedWork.mode, "rich");
  assert.equal(env.state().richFallback, false);
});
