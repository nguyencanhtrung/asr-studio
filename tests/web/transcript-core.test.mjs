import assert from "node:assert/strict";
import test from "node:test";

import { initialState as historyInitial, reduce as reduceHistory } from "../../src/asr_studio/web/history-core.js";
import { combine, createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  SAVED,
  SAVING,
  displayed,
  draftOf,
  hasUnsavedDrafts,
  initialState,
  navigation,
  needsReview,
  selectSegments,
  searchTimestamp,
  reduce,
} from "../../src/asr_studio/web/transcript-core.js";

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

const segment = (id, extra = {}) => ({ id, text: `text ${id}`, speaker_id: "A", assignment_version: 1, start_seconds: 0, end_seconds: 1, ...extra });
const speaker = (id, name) => ({ speaker_id: id, display_name: name });
const generation = (id, number, extra = {}) => ({
  id,
  generation_number: number,
  source_version: 5,
  status: "complete",
  segments: [segment("s1"), segment("s2", { speaker_id: "B" })],
  speakers: [speaker("A", "Speaker 1"), speaker("B", "Bob")],
  ...extra,
});
const meeting = (id = "m1", current = generation("g2", 2), history = [generation("g1", 1)]) => ({
  id,
  title: `Meeting ${id}`,
  metadata_version: 1,
  meeting_date: "2025-03-04",
  language_hint: "en",
  notes_source_locked: false,
  transcription: { generation: current, generation_history: history, task: null },
});

const FOCUS_TEXT = { kind: "segment-text", generationId: "g2", segmentId: "s1", control: "submit" };
const FOCUS_NAME = { kind: "speaker-name", generationId: "g2", speakerId: "A", control: "submit" };

// The page as the view wires it: the real store and runner over fakes, the facts of the meeting on screen stamped on
// every action, and the bridge the view makes from history-core's `installRev` and `left.rev`.
async function setup({ lock = false } = {}) {
  const requests = [];
  const calls = [];
  const run = createRunner({
    fetch: (url, init) =>
      new Promise((resolve, reject) => {
        requests.push({ url, method: init.method, body: init.body && JSON.parse(init.body), resolve, reject, done: false, signal: init.signal });
      }),
    setTimeout: () => 0,
    clearTimeout: () => {},
    calls: new Proxy({}, { get: (_, name) => async (...args) => { calls.push([name, ...args]); return "copied"; } }),
  });
  let store;
  const shown = () => {
    const state = store.getState().history;
    const current = state.meeting;
    if (!current) return null;
    return { id: current.id, meeting: current, view: state.view, installRev: state.installRev, blocked: !state.notes.lockKnown || state.notes.locked, task: null, generation: true };
  };
  const stamp = (action) => ({ at: { pathname: "/", search: "", hash: "", tab: null }, ...action, shown: shown() });
  store = createStore({
    reduce: combine({ history: reduceHistory, transcript: reduce }),
    initialState: { history: historyInitial, transcript: initialState },
    run: (effect, send) => run(effect, (action) => send(stamp(action))),
  });
  const send = (type, extra = {}) => store.dispatch(stamp({ type, ...extra }));
  let seen = store.getState().history;
  store.subscribe((root) => {
    const was = seen;
    seen = root.history;
    if (seen.installRev !== was.installRev) send("transcript/meeting-installed");
    if (seen.left.rev !== was.left.rev && seen.left.kind === "switch") send("transcript/meeting-switched");
  });
  const open = (match) => (r) => !r.done && !r.signal.aborted && match(r);
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
  const env = {
    store,
    send,
    requests,
    calls,
    answer,
    fail,
    t: () => store.getState().transcript,
    history: () => store.getState().history,
    // The writes and reads of the transcript on the wire (not the sidebar list the page also loads).
    pending: (method) => requests.filter((r) => !r.done && !r.signal.aborted && r.url !== "/api/meetings" && (!method || r.method === method)),
    // The paint commands since the last call, acknowledged as the view does.
    ops() {
      const taken = store.getState().transcript.ops;
      if (taken.length) send("transcript/painted", { through: taken.at(-1).n });
      return taken.map(({ n, ...op }) => op);
    },
    named: (name) => env.ops().filter((op) => op.op === name),
    install(next) {
      send("history/meeting-fetched", { meeting: next });
    },
    notice: () => text(store.getState().transcript.notice),
  };
  // Open meeting m1 as the page does: boot at its address and answer the list and the meeting.
  send("history/booted", { today: "2025-03-04" });
  store.dispatch(stamp({ type: "history/meeting-loaded", meta: { loadId: store.getState().history.loadId, meetingId: "m1" }, response: { body: meeting("m1") } }));
  send("history/notes-source-lock-reported", { meetingId: "m1", locked: lock, known: true });
  assert.equal(env.history().view, "meeting");
  env.ops();
  return env;
}

const textSave = (extra = {}) => ({ generationId: "g2", segmentId: "s1", text: "edited", focus: { ...FOCUS_TEXT, control: "editor" }, status: FOCUS_TEXT, ...extra });
const PATCH_TEXT = (r) => r.method === "PATCH" && r.url === "/api/meetings/m1/transcript-segments/s1/text";

async function openEditorOn(env, segmentId = "s1") {
  env.send("transcript/segment-edit-opened", { segmentId });
  assert.deepEqual(env.t().editing, { generationId: "g2", segmentId });
}

// ---- the generation on screen ----

test("the current transcript is selected, and a new current one takes the selection from the old", async () => {
  const env = await setup();
  assert.equal(env.t().installed, env.history().installRev, "the drafts are reconciled with the installed meeting");
  assert.equal(env.t().selected, "g2");
  env.send("transcript/generation-chosen", { id: "g1" });
  assert.equal(env.t().selected, "g1");
  env.install(meeting("m1", generation("g3", 3), [generation("g1", 1), generation("g2", 2)]));
  assert.equal(env.t().selected, "g1", "a past transcript the user chose stays on screen");
  assert.equal(env.t().lastCurrent, "g3");
  env.send("transcript/generation-chosen", { id: "g3" });
  env.install(meeting("m1", generation("g4", 4), [generation("g1", 1), generation("g2", 2), generation("g3", 3)]));
  assert.equal(env.t().selected, "g4", "a selection on the old current moves to the new one");
});

test("a selection that no longer exists falls back, and changing it clears the typed speaker name", async () => {
  const env = await setup();
  env.send("transcript/add-name-typed", { value: "Zed" });
  env.send("transcript/generation-chosen", { id: "g1" });
  assert.equal(env.t().addName, "", "the name belongs to the transcript it was typed for");
  env.send("transcript/add-name-typed", { value: "Zed" });
  env.send("transcript/generation-chosen", { id: "g1" });
  assert.equal(env.t().addName, "Zed", "choosing the same transcript changes nothing");
  const transcription = env.history().meeting.transcription;
  assert.equal(navigation({ ...env.t(), selected: "gone" }, transcription).selected, "g2", "falls back to the current transcript");
  assert.equal(navigation({ ...env.t(), selected: "gone" }, { ...transcription, generation: null }).selected, "g1", "else the latest one");
  assert.equal(displayed({ ...env.t(), selected: "g2" }, transcription).editable, true);
  assert.equal(displayed({ ...env.t(), selected: "g1" }, meeting().transcription).editable, false, "a past transcript only reads");
});

test("a meeting without transcripts has nothing selected; another meeting starts over", async () => {
  const env = await setup();
  env.send("transcript/segment-edit-opened", { segmentId: "s1" });
  env.send("transcript/add-name-typed", { value: "Zed" });
  env.send("transcript/meeting-switched");
  assert.deepEqual([env.t().selected, env.t().editing, env.t().error, env.t().addName], [null, null, null, ""]);
  const empty = { ...meeting("m1"), transcription: { generation: null, generation_history: [], task: null } };
  assert.deepEqual(navigation({ ...env.t(), selected: "g2", lastCurrent: "g2" }, empty.transcription).selected, null);
});

// ---- the one open editor ----

test("opening a second editor closes the first and keeps its unsaved text as a draft", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "typed" });
  await openEditorOn(env, "s2");
  assert.deepEqual(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "typed");
  const rows = env.named("row");
  assert.deepEqual(rows.map((op) => op.segmentId), ["s1", "s2"], "the row it leaves is painted first");
  assert.deepEqual(env.t().focus.target, { kind: "segment-editor", generationId: "g2", segmentId: "s2" });
  assert.equal(env.t().focus.caret, "end");
});

test("an editor opens only on the current transcript, with the source unlocked and no write on the wire", async () => {
  const locked = await setup({ lock: true });
  locked.send("transcript/segment-edit-opened", { segmentId: "s1" });
  assert.equal(locked.t().editing, null, "a locked notes source blocks editing");
  const env = await setup();
  env.send("transcript/generation-chosen", { id: "g1" });
  env.send("transcript/segment-edit-opened", { segmentId: "s1" });
  assert.equal(env.t().editing, null, "a past transcript is read-only");
  env.send("transcript/generation-chosen", { id: "g2" });
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("transcript/segment-edit-opened", { segmentId: "s2" });
  assert.equal(env.t().editing.segmentId, "s1", "no other editor opens while a save is on the wire");
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  assert.equal(env.t().renaming, null);
});

test("Hủy drops the typed text but keeps a draft a conflict marked", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "typed" });
  env.ops();
  env.send("transcript/segment-edit-cancelled", { segmentId: "s1" });
  assert.equal(env.t().editing, null);
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1"), null, "the typed text is gone");
  assert.deepEqual(env.named("review").map((op) => op.itemId), ["s1"], "its review goes with it");
  assert.deepEqual(env.t().focus.target, { kind: "segment-edit-button", generationId: "g2", segmentId: "s1", orTimestamp: true });
  assert.equal(env.t().focus.mode, "now");

  await openEditorOn(env, "s1");
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "typed" });
  env.send("transcript/segment-saved", textSave({ text: "typed" }));
  await env.answer(PATCH_TEXT, 409, { detail: { code: "source_version_changed" } });
  env.send("transcript/segment-edit-cancelled", { segmentId: "s1" });
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, "source_version_changed", "the only copy of the text stays");
  assert.equal(hasUnsavedDrafts(env.t()), true);
});

// ---- saving the text of a segment ----

test("a saved segment text goes to the server, then into the meeting; the editor closes and focus follows to Sửa", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave());
  assert.equal(env.pending().length, 1);
  assert.equal(env.pending()[0].url, "/api/meetings/m1/transcript-segments/s1/text");
  assert.deepEqual(env.pending()[0].body, { expected_generation_id: "g2", expected_source_version: 5, text: "edited" });
  assert.equal(env.t().mutation.kind, "segmentText");
  assert.equal(env.notice(), "Đang lưu thay đổi transcript.");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "edited", "the text is a draft while it is on the wire");
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  const generation = env.history().meeting.transcription.generation;
  assert.equal(generation.segments[0].text, "edited");
  assert.equal(generation.source_version, 6);
  assert.equal(env.t().mutation, null);
  assert.equal(env.t().editing, null);
  assert.equal(hasUnsavedDrafts(env.t()), false, "the saved draft is gone");
  assert.equal(env.notice(), "Đã lưu.");
  assert.deepEqual(env.t().focus.target, { kind: "segment-edit-button", generationId: "g2", segmentId: "s1" });
  assert.equal(env.t().focus.mode, "settle");
  assert.deepEqual(env.t().focus.from, { ...FOCUS_TEXT, control: "editor", meetingId: "m1" });
  assert.equal(env.t().focus.sameMeeting, true);
  const ops = env.ops().map((op) => op.op);
  for (const name of ["recovery", "clear-reload", "key", "row", "status"]) assert.ok(ops.includes(name), name);
});

test("saving the text already saved only closes the editor", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave({ text: "text s1" }));
  assert.equal(env.pending().length, 0);
  assert.equal(env.t().editing, null);
  assert.equal(env.notice(), "Không có thay đổi.");
  const { rev, ...focus } = env.t().focus;
  assert.ok(rev > 0);
  assert.deepEqual(focus, { mode: "now", target: { kind: "segment-edit-button", generationId: "g2", segmentId: "s1" }, from: null, sameMeeting: true });
});

test("a second save while one is on the wire is ignored, and so is a save for a stale row", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("transcript/segment-saved", textSave({ text: "again" }));
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: null });
  env.send("transcript/assignment-chosen", { generationId: "g2", segmentId: "s2", value: "A", assignmentVersion: "1", reviewed: false, focus: null, status: null });
  env.send("transcript/reload-clicked", { anchor: null });
  assert.equal(env.pending().length, 1, "one write at a time");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "edited", "the refused save leaves the draft alone");
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  env.send("transcript/segment-saved", textSave({ generationId: "g1", text: "x" }));
  env.send("transcript/segment-saved", textSave({ segmentId: "nope", text: "x" }));
  assert.equal(env.pending().length, 0, "a row of another transcript, or one that is gone, is not saved");
});

test("a failed save keeps the editor open and uses a safe localized fallback", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave());
  await env.fail(PATCH_TEXT);
  assert.equal(env.t().mutation, null);
  assert.equal(env.t().editing.segmentId, "s1");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "edited");
  assert.equal(env.notice(), "Không kết nối được với máy chủ trên máy này.");
  const status = env.named("status").at(-1);
  assert.equal(status.error, true);
  assert.equal(text(status.text), "Không kết nối được với máy chủ trên máy này.");
  assert.equal(env.t().error, null, "a network failure is no conflict");
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "text s1");

  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 400, { detail: "Đoạn quá dài." });
  assert.equal(env.notice(), "Máy chủ chưa thể hoàn tất yêu cầu. Hãy kiểm tra thông tin rồi thử lại.");
  assert.doesNotMatch(env.notice(), /Đoạn quá dài/);
});

test("a 409 marks the draft and puts the reload beside the open editor", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 409, { detail: { code: "source_version_changed" } });
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, "source_version_changed");
  assert.equal(env.notice(), "Transcript đã thay đổi. Bản nháp vẫn được giữ; hãy xem lại trước khi lưu lần nữa.");
  const reload = env.named("reload-button");
  assert.deepEqual(reload.map((op) => op.target.kind), ["segment-text"]);
  assert.equal(env.t().error, null, "the open editor holds the reload, the panel stays empty");
  // A conflict-marked draft is not saved again until it is edited.
  env.send("transcript/segment-saved", textSave());
  assert.equal(env.pending().length, 0);
  assert.equal(env.notice(), "Transcript đã thay đổi. Xem bản mới nhất rồi chỉnh bản nháp trước khi lưu.");
  // Editing it while the saved text is the one it started from keeps the mark; once the page holds a newer text, a
  // reviewed edit takes that text as its base and lifts the mark.
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "edited again" });
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, "source_version_changed");
  const newer = meeting("m1");
  newer.transcription.generation = { ...newer.transcription.generation, source_version: 6, segments: [segment("s1", { text: "theirs" }), segment("s2", { speaker_id: "B" })] };
  env.install(newer);
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "edited after theirs" });
  const rebased = draftOf(env.t().drafts, "g2", "segmentText", "s1");
  assert.deepEqual([rebased.conflictCode, rebased.baseValue, rebased.baseSourceVersion], [null, "theirs", 6]);
});

test("a 409 for a draft whose editor is closed puts the reload in the panel, and reopening moves it back", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 409, { detail: { code: "current_generation_changed" } });
  env.ops();
  // Another row's editor opens: the first row closes and its pending conflict moves to the panel.
  await openEditorOn(env, "s2");
  assert.equal(text(env.t().error.message), "Phiên bản transcript đã đổi. Bản nháp vẫn được giữ để sao chép.");
  assert.deepEqual([env.t().error.meetingId, env.t().error.generationId, env.t().error.reloadLatest], ["m1", "g2", true]);
  assert.equal(env.named("error").length, 1, "the panel is painted for the moved notice");
  await openEditorOn(env, "s1");
  assert.equal(env.t().error, null, "the open editor takes the reload back");
});

test("a 409 whose editor is gone shows the notice in the panel", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("transcript/meeting-switched");
  await env.answer(PATCH_TEXT, 409, { detail: { code: "source_version_changed" } });
  assert.equal(env.t().mutation, null);
  assert.equal(env.t().editing, null);
  assert.equal(text(env.t().error.message), "Transcript đã thay đổi. Bản nháp vẫn được giữ để xem lại.");
  assert.match(env.notice(), /Transcript đã thay đổi/);
  assert.notEqual(env.notice(), "Thay đổi transcript đã hoàn tất.");
});

// ---- the stale answer ----

test("an answer for a write the page no longer waits on is dropped", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  const before = env.t();
  env.send("transcript/mutation-succeeded", { meta: { id: 99, kind: "segmentText", meetingId: "m1", generationId: "g2", field: "segmentText", itemId: "s1", draftValue: "edited" }, response: { body: { text: "edited", source_version: 6 } } });
  assert.equal(env.t().mutation, before.mutation);
  env.send("transcript/mutation-failed", { meta: { id: 99 }, error: { message: "x" } });
  assert.equal(env.t().mutation, before.mutation);
});

test("an answer after the user left for another meeting settles the write without touching the new one", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave());
  // Another meeting becomes the one on screen while the save is on the wire.
  env.send("transcript/meeting-switched");
  env.install(meeting("m1")); // the page shows m1 still (the load of m2 is under way); then m2 arrives
  env.send("history/meeting-loaded", { meta: { loadId: env.history().loadId, meetingId: "m2" }, response: { body: meeting("m2", generation("g9", 1), []) } });
  assert.equal(env.history().meeting.id, "m2");
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.history().meeting.id, "m2");
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "text s1", "the other meeting is not edited");
  assert.equal(env.t().mutation, null);
  assert.equal(env.notice(), "Thay đổi transcript đã hoàn tất.", "the shared line does not carry the old meeting's words");
  assert.equal(env.t().focus.sameMeeting, false);
  assert.equal(hasUnsavedDrafts(env.t()), false, "the saved text is not kept as a draft");
  assert.ok(env.calls.some(([name, meetingId, reason]) => name === "meeting.change" && meetingId === "m1" && reason === "transcript"));
});

test("the same answer reaches the fresher copy of the meeting a poll or a refresh installed meanwhile", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  env.send("transcript/segment-saved", textSave());
  // A refresh installs a copy with another edit by someone else on the same segment (version 6).
  const other = meeting("m1");
  other.transcription.generation = { ...other.transcription.generation, source_version: 6, segments: [segment("s1", { text: "theirs" }), segment("s2", { speaker_id: "B" })] };
  env.install(other);
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, "source_version_changed", "the base moved on");
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 7 });
  const generation = env.history().meeting.transcription.generation;
  assert.deepEqual([generation.segments[0].text, generation.source_version], ["edited", 7]);
  const ops = env.ops().map((op) => op.op);
  assert.equal(ops.filter((name) => name === "key").length, 2, "a copy installed meanwhile is painted again, then the key follows");
  assert.equal(hasUnsavedDrafts(env.t()), false);
});

test("an answer that arrives while the page shows the home view paints no key", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("history/home-shown");
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "edited", "the meeting still takes the saved text");
  assert.equal(env.ops().filter((op) => op.op === "key").length, 1, "only the editor's own close paints the key");
});

test("nothing of the transcript is repainted for a speaker choice or a refreshed copy while the home view shows", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  env.send("history/home-shown");
  env.ops();
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "B", assignment_version: 2, source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].speaker_id, "B");
  assert.deepEqual(env.ops().map((op) => op.op).filter((name) => ["key", "assignment-synced"].includes(name)), ["assignment-synced"]);

  const refreshed = await setup();
  await openEditorOn(refreshed, "s1");
  refreshed.send("transcript/segment-saved", textSave());
  refreshed.send("history/home-shown");
  const newer = meeting("m1");
  newer.transcription.generation = { ...newer.transcription.generation, source_version: 6 };
  refreshed.install(newer);
  refreshed.ops();
  await refreshed.answer(PATCH_TEXT, 200, { text: "edited", source_version: 7 });
  assert.equal(refreshed.ops().filter((op) => op.op === "key").length, 1);
});

test("a save that lands on the meeting it started from updates the render key once", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.ops().filter((op) => op.op === "key").length, 1);
});

test("an answer with the version the page already holds is applied", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 5 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "edited");
});

test("an answer older than the version the page holds is not applied", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  const newer = meeting("m1");
  newer.transcription.generation = { ...newer.transcription.generation, source_version: 9, segments: [segment("s1", { text: "newest" }), segment("s2", { speaker_id: "B" })] };
  env.install(newer);
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "newest");
  assert.equal(env.history().meeting.transcription.generation.source_version, 9);
});

// ---- the speaker name ----

test("a speaker renamed: the chip returns, the labels follow, focus goes to the chip", async () => {
  const env = await setup();
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  assert.deepEqual(env.t().renaming, { generationId: "g2", speakerId: "A" });
  assert.equal(env.t().focus.caret, "select");
  env.ops();
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: { ...FOCUS_NAME, control: "input" }, status: FOCUS_NAME });
  const request = env.pending()[0];
  assert.equal(request.url, "/api/meetings/m1/transcript-generations/g2/speakers/A");
  assert.deepEqual(request.body, { expected_source_version: 5, display_name: "Ann" });
  await env.answer((r) => r.method === "PATCH" && r.url === request.url, 200, { display_name: "Ann", source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.speakers[0].display_name, "Ann");
  assert.equal(env.t().renaming, null);
  assert.equal(env.notice(), "Đã lưu tên người nói.");
  assert.deepEqual(env.t().focus.target, { kind: "speaker-chip", generationId: "g2", speakerId: "A" });
  const ops = env.ops();
  assert.ok(ops.some((op) => op.op === "chip" && op.speakerId === "A"));
  assert.ok(ops.some((op) => op.op === "labels" && op.speakerId === "A"));
});

test("clearing a speaker name asks for the default name and a failure keeps the rename open", async () => {
  const env = await setup();
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "", reviewed: true, focus: null, status: FOCUS_NAME });
  assert.equal(env.pending()[0].body.display_name, "");
  await env.answer((r) => r.method === "PATCH", 500, { detail: "Lỗi máy chủ." });
  assert.equal(env.t().renaming.speakerId, "A");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A").value, "");
  assert.equal(env.notice(), "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.");
  assert.equal(text(env.named("status").at(-1).text), "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.");
});

test("a speaker name conflict marks the draft; the rename form shows the reload", async () => {
  const env = await setup();
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  await env.answer((r) => r.method === "PATCH", 409, { detail: { code: "source_version_changed" } });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A").conflictCode, "source_version_changed");
  assert.deepEqual(env.named("reload-button").map((op) => op.target.kind), ["speaker-name"]);
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  assert.equal(env.pending().length, 0, "not saved again before the draft is reviewed");
  const message = env.named("status").at(-1);
  assert.equal(text(message.text), "Tên người nói đã thay đổi. Xem tên mới nhất rồi chỉnh bản nháp trước khi lưu.");
  assert.equal(env.notice(), "Transcript đã thay đổi. Bản nháp vẫn được giữ; hãy xem lại trước khi lưu lần nữa.");
  // Cancelling the rename keeps the marked draft and moves the reload to the panel.
  env.send("transcript/speaker-rename-cancelled", { speakerId: "A" });
  assert.equal(env.t().renaming, null);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A").value, "Ann");
  assert.equal(env.t().error.reloadLatest, true);
});

// ---- a new speaker ----

test("a new speaker is added to the transcript, the typed name clears and focus goes to its chip", async () => {
  const env = await setup();
  env.send("transcript/add-name-typed", { value: "Zed" });
  env.ops();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: { kind: "speaker-add", control: "submit" }, status: { kind: "speaker-add", control: "submit" } });
  const request = env.pending()[0];
  assert.equal(request.url, "/api/meetings/m1/transcript-generations/g2/speakers");
  assert.deepEqual(request.body, { expected_source_version: 5, display_name: "Zed" });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").value, "Zed");
  await env.answer((r) => r.method === "POST", 200, { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 6, generation_id: "g2" });
  const generation = env.history().meeting.transcription.generation;
  assert.deepEqual(generation.speakers.map((item) => item.speaker_id), ["A", "B", "C"]);
  assert.equal(generation.source_version, 6);
  assert.equal(env.t().addName, "");
  assert.equal(hasUnsavedDrafts(env.t()), false);
  assert.equal(env.notice(), "Đã thêm người nói.");
  assert.deepEqual(env.t().focus.target, { kind: "speaker-chip", generationId: "g2", speakerId: "C" });
  const ops = env.ops();
  assert.ok(ops.some((op) => op.op === "generation" && op.editable === true));
});

test("the typed name stays when the user typed something else meanwhile; a failure keeps the draft", async () => {
  const env = await setup();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  env.send("transcript/add-name-typed", { value: "Other" });
  await env.fail((r) => r.method === "POST");
  assert.equal(env.t().addName, "Other");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").value, "Zed");
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  await env.answer((r) => r.method === "POST", 200, { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 6, generation_id: "g2" });
  assert.equal(env.t().addName, "Other");
});

test("an add that conflicts keeps the draft marked and is reapplied to the new current transcript", async () => {
  const env = await setup();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  await env.answer((r) => r.method === "POST", 409, { detail: { code: "current_generation_changed" } });
  const draft = draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name");
  assert.equal(draft.conflictCode, "current_generation_changed");
  assert.deepEqual(env.named("reload-button").map((op) => op.target.kind), ["speaker-add"]);
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: null });
  assert.equal(env.pending().length, 0, "not sent again while the conflict stands");
  env.install(meeting("m1", generation("g3", 3, { source_version: 7 }), [generation("g1", 1), generation("g2", 2)]));
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").conflictCode, null, "the new current transcript lifts the mark");
  env.send("transcript/speaker-add-reapplied", { generationId: "g2", focus: null, status: { kind: "speaker-add", control: "submit" } });
  assert.equal(env.pending()[0].url, "/api/meetings/m1/transcript-generations/g3/speakers");
  assert.equal(env.pending()[0].body.display_name, "Zed");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").baseSourceVersion, 7, "the draft starts over from the current version");
  assert.equal(env.pending()[0].body.expected_source_version, 7);
  await env.answer((r) => r.method === "POST", 200, { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 8 });
  assert.deepEqual(env.history().meeting.transcription.generation.speakers.map((item) => item.speaker_id), ["A", "B", "C"]);
  assert.equal(hasUnsavedDrafts(env.t()), false, "the draft of the old transcript is gone");
});

test("adding a speaker is refused while the source is locked, before a first transcript or while a reload runs", async () => {
  const locked = await setup({ lock: true });
  locked.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: null });
  assert.equal(locked.pending().length, 0);
  const env = await setup();
  env.send("transcript/reload-clicked", { anchor: null });
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: null });
  assert.equal(env.pending("POST").length, 0);
  env.send("transcript/speaker-add-reapplied", { generationId: "g2", focus: null, status: null });
  assert.equal(env.pending("POST").length, 0, "nothing to reapply");
  const idle = await setup();
  assert.doesNotThrow(() => idle.send("transcript/speaker-add-reapplied", { generationId: "g2", focus: null, status: null }));
  assert.equal(idle.pending("POST").length, 0, "a button for a draft that is gone does nothing");
});

// ---- the speaker of a segment ----

const ASSIGN = (extra = {}) => ({ generationId: "g2", segmentId: "s1", value: "B", assignmentVersion: "1", reviewed: false, focus: null, status: { kind: "speaker-assignment", generationId: "g2", segmentId: "s1" }, ...extra });
const PATCH_SPEAKER = (r) => r.method === "PATCH" && r.url === "/api/meetings/m1/transcript-segments/s1/speaker";

test("a speaker choice goes to the server and into the meeting", async () => {
  const env = await setup();
  env.ops();
  env.send("transcript/assignment-chosen", ASSIGN());
  assert.deepEqual(env.pending()[0].body, { expected_generation_id: "g2", expected_source_version: 5, expected_assignment_version: 1, speaker_id: "B" });
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "B", assignment_version: 2, source_version: 6 });
  const segmentNow = env.history().meeting.transcription.generation.segments[0];
  assert.deepEqual([segmentNow.speaker_id, segmentNow.assignment_version], ["B", 2]);
  assert.equal(hasUnsavedDrafts(env.t()), false);
  assert.equal(env.notice(), "Đã lưu lựa chọn người nói.");
  const ops = env.ops().map((op) => op.op);
  assert.ok(ops.includes("assignment-synced") && ops.includes("key"));
});

test("choosing no speaker sends null; choosing the speaker already assigned sends nothing", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN({ value: "" }));
  assert.equal(env.pending()[0].body.speaker_id, null);
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: null, assignment_version: 2, source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].speaker_id, null);
  env.send("transcript/assignment-chosen", ASSIGN({ segmentId: "s2", value: "B", status: null }));
  assert.equal(env.pending().length, 0);
  assert.ok(env.ops().some((op) => op.op === "recovery"));
});

test("a failed speaker choice is forgotten and the select goes back; a conflict keeps it for review", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 500, { detail: "Lỗi." });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1"), null);
  assert.deepEqual(env.named("assignment-failed").map((op) => op.conflict), [false]);
  assert.deepEqual([text(env.t().error.message), env.t().error.reloadLatest], ["Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.", false]);
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "assignment_version_changed" } });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1").conflictCode, "assignment_version_changed");
  assert.equal(env.t().error.reloadLatest, true);
  const ops = env.ops();
  assert.ok(ops.some((op) => op.op === "generation" && op.force === true && op.editable === true), "the row is built again");
  // A conflict-marked choice is not sent again unless the user reviewed it.
  env.send("transcript/assignment-chosen", ASSIGN());
  assert.equal(env.pending().length, 0);
  assert.equal(env.ops().filter((op) => op.op === "assignment-note").length, 1);
});

// ---- the latest copy ----

const GET_M1 = (r) => r.method === "GET" && r.url === "/api/meetings/m1";

test("reloading the latest copy installs it, clears the panel and builds the transcript again", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "source_version_changed" } });
  assert.ok(env.t().error);
  env.ops();
  const before = env.history().installRev;
  env.send("transcript/reload-clicked", { anchor: null });
  assert.equal(env.t().reload.meetingId, "m1");
  const first = env.t().reload;
  env.send("transcript/reload-clicked", { anchor: null });
  assert.equal(env.t().reload, first, "a second click while it loads does nothing");
  assert.equal(env.pending().length, 1);
  const latest = meeting("m1");
  latest.transcription.generation = { ...latest.transcription.generation, source_version: 8, segments: [segment("s1", { speaker_id: "B" }), segment("s2", { speaker_id: "B" })] };
  await env.answer(GET_M1, 200, { meeting: latest });
  assert.equal(env.history().installRev, before + 1);
  assert.equal(env.history().meeting.transcription.generation.source_version, 8);
  assert.equal(env.t().reload, null);
  assert.equal(env.t().error, null);
  assert.equal(env.t().installed, env.history().installRev);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1"), null, "the latest copy already holds the chosen speaker");
  const ops = env.ops();
  assert.deepEqual(ops.map((op) => op.op).filter((name) => ["clear-reload-all", "generation"].includes(name)), ["clear-reload-all", "generation"]);
});

test("a reload answered after another copy was installed is dropped; a bad or failed one reports beside the button", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "source_version_changed" } });
  env.ops();
  env.send("transcript/reload-clicked", { anchor: null });
  env.install(meeting("m1"));
  await env.answer(GET_M1, 200, { meeting: { ...meeting("m1"), title: "stale" } });
  assert.notEqual(env.history().meeting.title, "stale");
  assert.equal(env.t().reload, null);
  assert.ok(env.t().error, "the conflict panel stays: the latest copy was not shown");
  assert.deepEqual(env.ops().map((op) => op.op).filter((name) => ["clear-reload-all", "generation"].includes(name)), []);
  env.ops();
  env.send("transcript/reload-clicked", { anchor: FOCUS_NAME });
  await env.answer(GET_M1, 200, { meeting: { id: "m9", transcription: {} } });
  const note = env.named("reload-note");
  assert.deepEqual(note.map((op) => text(op.text)), ["Không tải được trạng thái transcript mới nhất."]);
  assert.deepEqual(note[0].anchor, FOCUS_NAME);
  env.send("transcript/reload-clicked", { anchor: null });
  await env.fail(GET_M1);
  assert.deepEqual(env.named("reload-note").map((op) => text(op.text)), ["Không kết nối được với máy chủ trên máy này."]);
  assert.equal(env.t().reload, null);
});

test("a reload does not start while a write is on the wire", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("transcript/reload-clicked", { anchor: null });
  assert.equal(env.t().reload, null);
  assert.equal(env.pending("GET").length, 0);
});

// ---- drafts ----

test("typing keeps a draft until it equals the saved text; its review follows", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.ops();
  const type = (value) => env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value });
  type("a");
  assert.deepEqual(draftOf(env.t().drafts, "g2", "segmentText", "s1"), { value: "a", baseValue: "text s1", baseSourceVersion: 5, conflictCode: null });
  type("ab");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").baseValue, "text s1", "the base stays the saved text");
  type("text s1");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1"), null);
  assert.equal(env.ops().filter((op) => op.op === "review").length, 3);
  assert.equal(hasUnsavedDrafts(env.t()), false);
});

test("drafts live in plain objects and are never changed in place", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  const empty = env.t().drafts;
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "a" });
  const first = env.t().drafts;
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s2", value: "b" });
  assert.deepEqual(empty, {});
  assert.deepEqual(Object.keys(first.g2.segmentText), ["s1"]);
  assert.deepEqual(Object.keys(env.t().drafts.g2.segmentText), ["s1", "s2"]);
  assert.ok(Object.isFrozen === Object.isFrozen);
});

test("a draft survives the meeting being loaded again, and is checked against the new value", async () => {
  const env = await setup();
  const type = (itemId, value) => env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId, value });
  type("s1", "mine one");
  type("s2", "same as theirs");
  const next = meeting("m1");
  next.transcription.generation = {
    ...next.transcription.generation,
    source_version: 6,
    segments: [segment("s1", { text: "theirs one" }), segment("s2", { text: "same as theirs", speaker_id: "B" })],
  };
  env.ops();
  env.install(next);
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, "source_version_changed", "the base moved on");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s2"), null, "a draft the new copy already holds is dropped");
  assert.deepEqual(env.named("review").map((op) => op.itemId), ["s2"]);
  // The newer saved value is the base once the user reviews the draft, and the mark is lifted.
  type("s1", "mine, reviewed");
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, null);
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").baseValue, "theirs one");
  // Back to the value it started from: the mark goes with the conflict.
  const back = meeting("m1");
  back.transcription.generation = { ...back.transcription.generation, source_version: 7, segments: [segment("s1", { text: "theirs one" }), segment("s2", { speaker_id: "B" })] };
  env.install(back);
  assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").conflictCode, null);
});

test("a conflict mark lifts when a newer copy still holds the text the draft started from", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 409, { detail: { code: "source_version_changed" } });
  const next = meeting("m1");
  next.transcription.generation = { ...next.transcription.generation, source_version: 6 }; // another row changed, this text did not
  env.install(next);
  const draft = draftOf(env.t().drafts, "g2", "segmentText", "s1");
  assert.deepEqual([draft.conflictCode, draft.baseValue, draft.value], [null, "text s1", "edited"]);
});

test("a speaker name draft and a choice draft are reconciled the same way", async () => {
  const env = await setup();
  env.send("transcript/draft-typed", { generationId: "g2", field: "speakerName", itemId: "A", value: "Ann" });
  env.send("transcript/draft-typed", { generationId: "g2", field: "speakerName", itemId: "B", value: "Robert" });
  env.send("transcript/draft-typed", { generationId: "g2", field: "speakerAssignment", itemId: "s1", value: "B" });
  const next = meeting("m1");
  next.transcription.generation = {
    ...next.transcription.generation,
    source_version: 6,
    speakers: [speaker("A", "Ann"), speaker("B", "Bobby")],
    segments: [segment("s1", { speaker_id: "B" }), segment("s2", { speaker_id: "B" })],
  };
  env.install(next);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A"), null, "the new copy holds that name");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "B").conflictCode, "source_version_changed");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1"), null);
});

test("a choice draft kept for review loses its mark when the source version moves on, except for another transcript", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "current_generation_changed" } });
  const next = meeting("m1");
  next.transcription.generation = { ...next.transcription.generation, source_version: 6 };
  env.install(next);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1").conflictCode, "current_generation_changed", "kept with its historical transcript");
  env.send("transcript/assignment-chosen", ASSIGN({ segmentId: "s2", value: "A", status: null }));
  await env.answer((r) => r.method === "PATCH" && r.url.endsWith("/s2/speaker"), 409, { detail: { code: "assignment_version_changed" } });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s2").conflictCode, "assignment_version_changed");
  const later = meeting("m1");
  later.transcription.generation = { ...later.transcription.generation, source_version: 7 };
  env.install(later);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s2").conflictCode, null, "the version moved on: it can be applied again");
});

test("copying a draft is a call; the answer says how it went", async () => {
  const env = await setup();
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "mine" });
  env.send("transcript/copy-clicked", { generationId: "g2", field: "segmentText", itemId: "s1" });
  await tick();
  assert.deepEqual(env.calls, [["transcript.copy", "mine", { generationId: "g2", field: "segmentText", itemId: "s1" }]]);
  assert.deepEqual(env.named("copy-status").map((op) => [op.itemId, op.how]), [["s1", "copied"]]);
  env.send("transcript/copy-clicked", { generationId: "g2", field: "segmentText", itemId: "none" });
  await tick();
  assert.equal(env.calls.length, 1, "no draft, no copy");
});

// ---- the shared status line, focus and paint commands ----

test("the shared line says saving, then the result; the saved words become 'done' when nothing else was said", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN({ segmentId: "s2", value: "B" }));
  assert.equal(env.t().mutation, null, "nothing to send for the speaker already assigned");
  env.send("transcript/assignment-chosen", ASSIGN({ value: "B" }));
  assert.equal(env.t().notice, SAVING);
  // The choice lands and is reported; the next one is answered after the user switched to another meeting.
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "B", assignment_version: 2, source_version: 6 });
  assert.equal(env.t().notice.text, "Đã lưu lựa chọn người nói.");
  env.send("transcript/assignment-chosen", ASSIGN({ value: "A", assignmentVersion: "2" }));
  env.send("transcript/meeting-switched");
  env.send("history/meeting-loaded", { meta: { loadId: env.history().loadId, meetingId: "m2" }, response: { body: meeting("m2", generation("g9", 1), []) } });
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "A", assignment_version: 3, source_version: 7 });
  assert.equal(env.t().notice, SAVED);
});

test("focus returns to the control the write began on, unless the result moved it", async () => {
  const env = await setup();
  const rev = env.t().focus.rev;
  env.send("transcript/assignment-chosen", ASSIGN({ focus: { kind: "speaker-assignment", generationId: "g2", segmentId: "s1" } }));
  assert.equal(env.t().focus.rev, rev, "focus is read when the write ends");
  await env.fail(PATCH_SPEAKER);
  assert.equal(env.t().focus.rev, rev + 1);
  assert.equal(env.t().focus.mode, "settle");
  assert.deepEqual(env.t().focus.from, { kind: "speaker-assignment", generationId: "g2", segmentId: "s1", meetingId: "m1" });
  assert.equal(env.t().focus.target, null);
});

test("paint commands are kept until the view acknowledges them, in order, across several actions", async () => {
  const env = await setup();
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "a" });
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s2", value: "b" });
  const ops = env.t().ops;
  assert.deepEqual(ops.map((op) => [op.n, op.itemId]), [[ops[0].n, "s1"], [ops[0].n + 1, "s2"]]);
  env.send("transcript/painted", { through: ops[0].n });
  assert.deepEqual(env.t().ops.map((op) => op.itemId), ["s2"]);
  env.send("transcript/painted", { through: ops[1].n });
  assert.deepEqual(env.t().ops, []);
  const quiet = env.t();
  env.send("transcript/painted", { through: 1000 });
  assert.equal(env.t(), quiet, "an acknowledgement of nothing changes nothing");
});

// ---- round 2: answers that are no object, answers after the page moved on, and the branches the first round left open ----

const POST = (r) => r.method === "POST";
const ADDED = { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 6, generation_id: "g2" };
const NETWORK = "Không kết nối được với máy chủ trên máy này.";

test("a 2xx body that is no object is a failed write in every kind: the draft stays and nothing is applied", async () => {
  for (const body of ["<html>login</html>", null, []]) {
    const env = await setup();
    const before = env.history().meeting;
    env.send("transcript/segment-edit-opened", { segmentId: "s1" });
    env.send("transcript/segment-saved", textSave());
    await env.answer(PATCH_TEXT, 200, body);
    assert.equal(env.t().mutation, null);
    assert.equal(env.t().editing.segmentId, "s1", "the editor stays open");
    assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "edited");
    assert.equal(env.notice(), "Không thể lưu đoạn transcript.");
    assert.equal(env.named("status").at(-1).error, true);
    env.send("transcript/speaker-rename-opened", { speakerId: "A" });
    env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
    await env.answer((r) => r.method === "PATCH" && r.url.includes("/speakers/A"), 200, body);
    assert.equal(env.t().renaming.speakerId, "A");
    assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A").value, "Ann");
    assert.equal(env.notice(), "Không thể lưu tên người nói.");
    env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
    await env.answer(POST, 200, body);
    assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").value, "Zed");
    assert.equal(env.notice(), "Không thể thêm người nói.");
    env.send("transcript/assignment-chosen", ASSIGN());
    await env.answer(PATCH_SPEAKER, 200, body);
    assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1"), null);
    assert.deepEqual([text(env.t().error.message), env.t().error.reloadLatest], ["Không thể lưu người nói.", false]);
    assert.equal(env.t().mutation, null, "the controls are free again");
    assert.equal(env.history().meeting, before, "the meeting is untouched");
  }
});

test("a save that lands after the meeting was deleted drops its draft", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("history/deletion-succeeded", { meta: { meetingId: "m1" }, response: { body: { meeting_id: "m1", status: "deleted" } }, at: { pathname: "/meetings/m1", search: "", hash: "", tab: null } });
  assert.equal(env.history().meeting, null);
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.t().mutation, null);
  assert.equal(hasUnsavedDrafts(env.t()), false, "no prompt on leaving for a draft of a meeting that is gone");
  assert.equal(env.notice(), "Thay đổi transcript đã hoàn tất.");
});

test("a speaker added after the meeting was deleted drops its typed name", async () => {
  const env = await setup();
  env.send("transcript/add-name-typed", { value: "Zed" });
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  env.send("history/deletion-succeeded", { meta: { meetingId: "m1" }, response: { body: { meeting_id: "m1", status: "deleted" } }, at: { pathname: "/meetings/m1", search: "", hash: "", tab: null } });
  await env.answer(POST, 201, { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 6, generation_id: "g2" });
  assert.equal(env.t().mutation, null);
  assert.equal(hasUnsavedDrafts(env.t()), false, "the old success path cleared the add draft");
  assert.equal(env.notice(), "Thay đổi transcript đã hoàn tất.");
});

test("a text save that lands after a new current transcript was installed is applied to the transcript it asked", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.install(meeting("m1", generation("g3", 3, { source_version: 7 }), [generation("g1", 1), generation("g2", 2)]));
  assert.equal(env.t().selected, "g3", "the page followed the new current transcript");
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  const now = env.history().meeting.transcription;
  assert.equal(now.generation_history.find((item) => item.id === "g2").segments[0].text, "edited");
  assert.equal(now.generation.segments[0].text, "text s1", "the new current transcript is not edited");
  const names = env.ops().map((op) => op.op);
  assert.ok(!names.includes("generation") && !names.includes("labels"), "g3 is not built again");
  assert.deepEqual(env.history().meeting.transcription.generation.id, "g3");
  assert.equal(env.notice(), "Đã lưu.");
});

test("a text save that lands after another transcript was chosen leaves the screen alone", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.send("transcript/generation-chosen", { id: "g1" });
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "edited");
  assert.equal(env.t().selected, "g1");
  const names = env.ops().map((op) => op.op);
  assert.ok(!names.includes("generation") && !names.includes("labels"), "the transcript on screen is not built again");
  assert.equal(env.notice(), "Đã lưu.");
});

test("a text save that lands on a transcript that is no longer the current one repaints it read-only", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  env.install(meeting("m1", generation("g3", 3, { source_version: 7 }), [generation("g1", 1), generation("g2", 2)]));
  env.send("transcript/generation-chosen", { id: "g2" });
  env.ops();
  await env.answer(PATCH_TEXT, 200, { text: "edited", source_version: 6 });
  assert.deepEqual(env.named("generation").map((op) => op.editable), [false]);
});

test("a speaker added again lands only on the current transcript it asked for", async () => {
  const env = await setup();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  await env.answer(POST, 409, { detail: { code: "current_generation_changed" } });
  env.install(meeting("m1", generation("g3", 3, { source_version: 7 }), [generation("g1", 1), generation("g2", 2)]));
  env.send("transcript/speaker-add-reapplied", { generationId: "g2", focus: null, status: { kind: "speaker-add", control: "submit" } });
  env.install(meeting("m1", generation("g4", 4, { source_version: 9 }), [generation("g1", 1), generation("g2", 2), generation("g3", 3)]));
  env.ops();
  await env.answer(POST, 200, { speaker_id: "C", display_name: "Zed", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 8 });
  const now = env.history().meeting.transcription;
  assert.deepEqual(now.generation_history.find((item) => item.id === "g3").speakers.map((item) => item.speaker_id), ["A", "B"], "g3 is no longer current");
  assert.deepEqual(now.generation.speakers.map((item) => item.speaker_id), ["A", "B"]);
  assert.equal(hasUnsavedDrafts(env.t()), false);
  assert.equal(env.notice(), "Đã thêm người nói.");
  assert.ok(!env.ops().some((op) => op.op === "generation"));
});

test("a speaker name or a choice lost to the network keeps what the user did", async () => {
  const env = await setup();
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  await env.fail((r) => r.method === "PATCH");
  assert.equal(env.t().renaming.speakerId, "A", "the rename stays open");
  assert.equal(draftOf(env.t().drafts, "g2", "speakerName", "A").value, "Ann");
  assert.equal(env.notice(), NETWORK);
  const line = env.named("status").at(-1);
  assert.deepEqual([text(line.text), line.error, line.target.kind], [NETWORK, true, "speaker-name"]);
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.fail(PATCH_SPEAKER);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1"), null);
  assert.equal(env.notice(), NETWORK);
  assert.deepEqual([text(env.t().error.message), env.t().error.reloadLatest], [NETWORK, false]);
  const ops = env.ops();
  assert.deepEqual(ops.filter((op) => op.op === "assignment-failed").map((op) => op.conflict), [false]);
  assert.ok(ops.some((op) => op.op === "recovery"), "the recovery panel is painted again");
});

test("a speaker choice reports on the shared line only, never on a row's own status line", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN({ status: { kind: "speaker-assignment", generationId: "g2", segmentId: "s1" } }));
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "B", assignment_version: 2, source_version: 6 });
  assert.equal(env.notice(), "Đã lưu lựa chọn người nói.");
  assert.deepEqual(env.named("status"), []);
  env.send("transcript/assignment-chosen", ASSIGN({ value: "A", assignmentVersion: "2" }));
  await env.fail(PATCH_SPEAKER);
  assert.equal(env.notice(), NETWORK);
  assert.deepEqual(env.named("status"), []);
});

test("a saved choice clears the panel a failed one filled", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 500, { detail: "Lỗi." });
  assert.notEqual(env.t().error, null);
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 200, { speaker_id: "B", assignment_version: 2, source_version: 6 });
  assert.equal(env.t().error, null);
});

test("a choice conflict builds the transcript again only while that transcript is the one on screen", async () => {
  const env = await setup();
  env.send("transcript/assignment-chosen", ASSIGN());
  env.send("transcript/generation-chosen", { id: "g1" });
  env.ops();
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "assignment_version_changed" } });
  assert.deepEqual(env.named("generation"), []);
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAssignment", "s1").conflictCode, "assignment_version_changed");
});

test("the new-speaker draft keeps the version it started from when it is typed again", async () => {
  const env = await setup();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  await env.fail(POST);
  env.install(meeting("m1", generation("g2", 2, { source_version: 7 }), [generation("g1", 1)]));
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  assert.equal(draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").baseSourceVersion, 5);
});

test("a text or name save held back by a conflict-marked draft reports an error line", async () => {
  const env = await setup();
  await openEditorOn(env, "s1");
  env.send("transcript/segment-saved", textSave());
  await env.answer(PATCH_TEXT, 409, { detail: { code: "source_version_changed" } });
  env.ops();
  env.send("transcript/segment-saved", textSave());
  assert.equal(env.named("status").at(-1).error, true);
  env.send("transcript/speaker-rename-opened", { speakerId: "A" });
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  await env.answer((r) => r.method === "PATCH", 409, { detail: { code: "source_version_changed" } });
  env.ops();
  env.send("transcript/speaker-name-saved", { generationId: "g2", speakerId: "A", name: "Ann", reviewed: false, focus: null, status: FOCUS_NAME });
  assert.equal(env.named("status").at(-1).error, true);
});

test("a reload that read the latest copy builds the transcript again, whatever it looked like", async () => {
  const env = await setup();
  env.send("transcript/reload-clicked", { anchor: null });
  await env.answer(GET_M1, 200, { meeting: meeting("m1") });
  assert.ok(env.ops().some((op) => op.op === "generation" && op.force === true));
});

test("the name draft and the choice draft lose their mark by the same rules as the text draft", async () => {
  const env = await setup();
  const copy = (version, extra) => {
    const next = meeting("m1");
    next.transcription.generation = { ...next.transcription.generation, source_version: version, ...extra };
    return next;
  };
  const drafts = () => [draftOf(env.t().drafts, "g2", "speakerName", "A")?.conflictCode, draftOf(env.t().drafts, "g2", "speakerAssignment", "s1")?.conflictCode];
  env.send("transcript/draft-typed", { generationId: "g2", field: "speakerName", itemId: "A", value: "Ann" });
  env.install(copy(6, { speakers: [speaker("A", "Alice"), speaker("B", "Bob")] }));
  assert.equal(drafts()[0], "source_version_changed");
  env.install(copy(7));
  assert.equal(drafts()[0], null, "the name is back to the one the draft started from");
  // A choice draft that a source-version conflict marked is unmarked only once the version moved on.
  env.send("transcript/assignment-chosen", ASSIGN());
  await env.answer(PATCH_SPEAKER, 409, { detail: { code: "source_version_changed" } });
  assert.equal(drafts()[1], "source_version_changed");
  env.install(copy(7));
  assert.equal(drafts()[1], "source_version_changed", "same version: still marked");
  env.install(copy(8));
  assert.equal(drafts()[1], null);
  // An assignment-version conflict likewise.
  const second = () => draftOf(env.t().drafts, "g2", "speakerAssignment", "s2")?.conflictCode;
  env.send("transcript/assignment-chosen", ASSIGN({ segmentId: "s2", value: "A", status: null }));
  await env.answer((r) => r.method === "PATCH" && r.url.endsWith("/s2/speaker"), 409, { detail: { code: "assignment_version_changed" } });
  env.install(copy(8));
  assert.equal(second(), "assignment_version_changed", "same version: still marked");
  env.install(copy(9));
  assert.equal(second(), null);
});

test("a new-speaker draft loses its mark when its transcript moved on, and not before", async () => {
  const env = await setup();
  env.send("transcript/speaker-add-submitted", { name: "Zed", focus: null, status: { kind: "speaker-add", control: "submit" } });
  await env.answer(POST, 409, { detail: { code: "source_version_changed" } });
  const code = () => draftOf(env.t().drafts, "g2", "speakerAddName", "speaker-add-name").conflictCode;
  env.install(meeting("m1"));
  assert.equal(code(), "source_version_changed", "same version: still marked");
  env.install(meeting("m1", generation("g2", 2, { source_version: 6 }), [generation("g1", 1)]));
  assert.equal(code(), null);
});

test("paint commands for a meeting that was left are dropped, and the shared line is written again for a repeated result", async () => {
  const env = await setup();
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "a" });
  assert.ok(env.t().ops.length > 0);
  env.send("transcript/meeting-switched");
  assert.deepEqual(env.t().ops, []);
  const same = { generationId: "g2", segmentId: "s1", text: "text s1", focus: null, status: FOCUS_TEXT };
  env.send("transcript/segment-edit-opened", { segmentId: "s1" });
  env.send("transcript/segment-saved", same);
  const first = env.t();
  env.send("transcript/segment-edit-opened", { segmentId: "s1" });
  env.send("transcript/segment-saved", same);
  assert.equal(env.t().notice.text, first.notice.text);
  assert.equal(env.t().noticeRev, first.noticeRev + 1, "the view writes the same words again");
});

test("an action the transcript slice does not know changes nothing", () => {
  const result = reduce(initialState, { type: "transcript/unknown" });
  assert.equal(result.state, initialState);
  assert.deepEqual(result.effects, []);
});


test("search selects saved text, speaker names/IDs and timestamp ranges literally", () => {
  const g = generation("g", 1, { segments: [
    segment("a", { text: "Launch <script>", start_seconds: 1.234, end_seconds: 3 }),
    segment("b", { text: "Costs", speaker_id: "identity-b", start_seconds: 3661.25, end_seconds: 3663 }),
  ], speakers: [speaker("A", "Speaker 1"), speaker("identity-b", "Bob")] });
  const ids = (query) => selectSegments({ ...initialState, query }, g).segments.map((s) => s.id);
  assert.deepEqual(ids(" LAUNCH "), ["a"]);
  assert.deepEqual(ids("<script>"), ["a"]);
  assert.deepEqual(ids("Bob"), ["b"]);
  assert.deepEqual(ids("identity-b"), ["b"]);
  assert.deepEqual(ids("0:01.23"), ["a"]);
  assert.deepEqual(ids("00:01.23"), ["a"]);
  assert.deepEqual(ids("00:01"), ["a"]);
  assert.deepEqual(ids("1:01:01.25"), ["b"]);
  assert.deepEqual(ids("1:01:03"), ["b"]);
  assert.deepEqual(ids("absent"), []);
  assert.equal(searchTimestamp(59.9996), "1:00.00");
  assert.deepEqual(selectSegments(initialState, null), { segments: [], review: [], total: 0 });
});

test("review navigation follows flags and reasons, wraps both ways and ignores processing status", () => {
  const g = generation("g", 1, { segments: [
    segment("a", { uncertain: true }), segment("b", { review_reasons: ["overlap"] }),
    segment("c", { status: "complete", reviewed: true, review_reasons: [] }),
    segment("d", { review_reasons: ["future-reason"] }),
  ] });
  const shown = { meeting: meeting("m", g, []) };
  let state = reduce(initialState, { type: "transcript/review-filter-changed", value: true, shown }).state;
  assert.deepEqual(selectSegments(state, g).segments.map((s) => s.id), ["a", "b", "d"]);
  assert.equal(needsReview(g.segments[2]), false);
  for (const [direction, expected] of [[-1, "d"], [1, "a"], [1, "b"], [1, "d"], [-1, "b"]]) {
    const result = reduce(state, { type: "transcript/review-stepped", direction, shown });
    state = result.state;
    assert.deepEqual(result.effects, []);
    assert.equal(state.ops.at(-1).id, expected);
    assert.equal(state.ops.at(-1).op, "reveal-segment");
  }
  state = reduce(state, { type: "transcript/query-typed", value: "absent", shown }).state;
  assert.equal(state.reviewCursor, null);
  assert.equal(reduce(state, { type: "transcript/review-stepped", direction: 1, shown }).state, state);
  assert.deepEqual(selectSegments(state, g).segments, []);
});

test("filters and generation toggles preserve an open draft without any write or storage effect", async () => {
  const env = await setup();
  await openEditorOn(env);
  env.send("transcript/draft-typed", { generationId: "g2", field: "segmentText", itemId: "s1", value: "Unsaved only" });
  const requests = env.requests.length;
  for (const [type, extra] of [
    ["transcript/query-typed", { value: "no matches" }],
    ["transcript/review-filter-changed", { value: true }],
    ["transcript/generation-chosen", { id: "g1" }],
    ["transcript/generation-chosen", { id: "g2" }],
    ["transcript/filters-cleared", { segmentId: "s1" }],
  ]) {
    const result = reduce(env.t(), { type, ...extra, shown: { meeting: env.history().meeting } });
    assert.deepEqual(result.effects, []);
    env.send(type, extra);
    assert.equal(draftOf(env.t().drafts, "g2", "segmentText", "s1").value, "Unsaved only");
    assert.equal(draftOf(env.t().drafts, "g1", "segmentText", "s1"), null);
    assert.deepEqual(env.t().editing, { generationId: "g2", segmentId: "s1" });
  }
  assert.equal(env.requests.length, requests);
  assert.equal(env.history().meeting.transcription.generation.segments[0].text, "text s1");
  assert.equal(env.t().ops.at(-1).id, "s1");
  assert.equal(env.t().query, "");
  assert.equal(env.t().reviewOnly, false);
  env.ops();
  env.send("transcript/filters-cleared", { segmentId: "missing" });
  assert.deepEqual(env.ops(), []);
  env.send("transcript/query-typed", { value: "other meeting query" });
  env.send("transcript/review-filter-changed", { value: true });
  env.send("transcript/meeting-switched");
  assert.equal(env.t().query, "");
  assert.equal(env.t().reviewOnly, false);
});
