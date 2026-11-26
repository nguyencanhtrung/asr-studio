import assert from "node:assert/strict";
import test from "node:test";

import { createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  blockRuns,
  hasUnsavedDraft,
  failureMessage,
  generationChoices,
  initialState,
  normalizePendingRequest,
  outputLanguageStorageKey,
  pendingRequestStorageKey,
  reduce,
  requestStorageKey,
  savedVersionFromHref,
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
      const raw = init.body;
      requests.push({ url, method: init.method, raw, body: raw && JSON.parse(raw), resolve, reject, done: false, signal: init.signal });
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
async function opened(env, { revisions = [], task = null, catalog = CATALOG, preferences = PREFS } = {}) {
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody(revisions, task));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, preferences);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, catalog);
}

async function openedMeeting(env, customMeeting, { revisions = [], catalog = CATALOG, preferences = PREFS } = {}) {
  env.context(customMeeting);
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody(revisions));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, preferences);
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

for (const action of ["saved-history-closed", "saved-history-picked"]) {
  for (const detailStatus of ["pending", "failed"]) {
    test(`returning to latest through ${action} keeps the linked revision after ${detailStatus} detail`, async () => {
      const env = setup();
      const href = `${ORIGIN}/meetings/m1?notes_revision=r1&notes_edit=2#notes`;
      const linked = revision({ edit_version: 4, markdown: "# Latest of linked revision" });
      env.context(meeting(), { href });
      await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([
        revision({ id: "r2", markdown: "# Another generated revision" }), linked,
      ]));
      const detail = env.waiting(env.is("GET", "/api/notes/r1/versions/2"))[0];
      assert.ok(detail);
      if (detailStatus === "failed") {
        await env.answer(env.is("GET", "/api/notes/r1/versions/2"), 404, {});
      }

      env.dispatch(action, { href, editVersion: null });
      assert.equal(selectView(env.state()).savedWork.pinned, true);
      if (detailStatus === "pending") assert.equal(detail.signal.aborted, false);
      await env.answer(targetRead, 200, linked);
      const returned = selectView(env.state());
      assert.equal(returned.savedWork.pinned, false);
      assert.equal(returned.savedWork.revision.id, linked.id);
      assert.equal(returned.savedWork.draftValue, linked.markdown);
      assert.equal(env.hostCalls.find(([name]) => name === "replaceUrl")[1], "/meetings/m1#notes");
      if (detailStatus === "pending") assert.equal(detail.signal.aborted, true);
      assert.equal(returned.savedHistory.open, action === "saved-history-picked");
    });
  }
}

const fixedHref = `${ORIGIN}/meetings/m1?notes_revision=r1&notes_edit=2#notes`;
const targetRead = (request) => request.method === "GET" && request.url === "/api/notes/r1";
const savedEdit = (version) => revision({ edit_version: version, markdown: `# Saved edit ${version}` });
const versionIndex = (latest) => ({
  id: "r1", meeting_id: "m1", latest_edit_version: latest,
  versions: [1, 2, 3, 4, 5].slice(0, latest).map((edit_version) => ({
    note_id: "r1", edit_version, origin: edit_version === 1 ? "ai" : "operator", saved_at: "2026-11-25 00:00:00",
  })),
});

const meetingRead = (r) => r.url === "/api/meetings/m1/notes";
const historyIndex = (r) => r.url === "/api/notes/r1/versions";
const fixedDetail = (r) => r.url === "/api/notes/r1/versions/2";

for (const action of ["copy", "export", "rich-preview", "email-preview"]) {
  test(`fixed ${action} survives meeting list population, updates and omission`, async () => {
    const env = setup();
    const view = () => selectView(env.state());
    const fixed = revision({ edit_version: 2, markdown: "# Fixed two\n\nKeep **these bytes**.\n" });
    env.context(meeting(), { href: fixedHref });
    await env.answer(fixedDetail, 200, fixed);
    env.dispatch(`${action}-clicked`);
    const [pending] = env.waiting(fixedDetail);
    await env.answer(meetingRead, 200, notesBody([savedEdit(3)]));
    assert.equal(pending.signal.aborted, false);
    await env.answer(historyIndex, 200, versionIndex(4));
    env.dispatch("meeting-change-received", { meetingId: "m1" });
    await env.answer(meetingRead, 200, notesBody([savedEdit(5)]));

    let finishClipboard;
    env.hostResults.copy = env.hostResults.copyFormatted = () => new Promise((resolve) => { finishClipboard = resolve; });
    await env.answer(fixedDetail, 200, fixed);
    const preview = action.endsWith("preview");
    if (preview) {
      assert.equal(view().copyPreview.content.kind, action.split("-")[0]);
      assert.equal(view().copyPreview.content.editVersion, 2);
      env.dispatch("preview-copy-clicked");
    }
    env.dispatch("meeting-change-received", { meetingId: "m1" });
    await env.answer(meetingRead, 200, notesBody([revision({ id: "r2" })]));
    assert.deepEqual(notes(env).revisions.map(({ id }) => id), ["r2"]);
    assert.equal(view().savedWork.revision.markdown, fixed.markdown);

    if (finishClipboard) finishClipboard("html");
    await tick();
    const shareCalls = env.hostCalls.filter(([name]) => ["copy", "download", "copyFormatted"].includes(name));
    assert.equal(shareCalls.length, 1);
    if (preview) {
      const [[name, html, plain]] = shareCalls;
      assert.equal(name, "copyFormatted");
      assert.match(html, /Keep <strong>these bytes<\/strong>/);
      assert.equal(plain, fixed.markdown);
      assert.equal(view().copyPreview.copying, false);
      assert.match(text(view().copyPreview.status), /Đã sao chép nội dung có định dạng/);
    } else {
      assert.deepEqual(shareCalls, action === "copy"
        ? [["copy", fixed.markdown]] : [["download", "Weekly.md", fixed.markdown]]);
      assert.match(text(view().actionMessage), action === "copy" ? /Đã sao chép.*edit 2/ : /Đã tải.*edit 2/);
    }
  });
}

for (const control of ["saved-history-closed", "saved-history-picked"]) {
  test(`${control} remembers a newly projected v5 across stale and omitting lists`, async () => {
    const env = setup();
    const view = () => selectView(env.state());
    env.context(meeting(), { href: fixedHref });
    await env.answer(fixedDetail, 200, savedEdit(2));
    env.dispatch(control, { href: fixedHref, editVersion: null });
    assert.equal(view().savedHistory.latestPhase, "pending");
    await env.answer(meetingRead, 200, notesBody([savedEdit(5), revision({ id: "r2" })]));
    await env.answer(historyIndex, 200, versionIndex(3));
    for (const rows of [[revision({ id: "r3" })], [savedEdit(3)], []]) {
      env.dispatch("meeting-change-received", { meetingId: "m1" });
      await env.answer(meetingRead, 200, notesBody(rows));
      assert.deepEqual(notes(env).revisions.map(({ id }) => id), rows.map(({ id }) => id));
    }
    await env.answer(targetRead, 200, savedEdit(4));
    assert.equal(view().savedHistory.latestPhase, "failed");
    assert.equal(view().savedHistory.selected, "2");
    assert.equal(view().savedWork.revision.markdown, "# Saved edit 2");
    assert.equal(view().savedWork.saveDisabled, true);
    assert.ok(!env.hostCalls.some(([name]) => name === "replaceUrl"));

    env.dispatch("saved-history-closed", { href: fixedHref });
    await env.answer(targetRead, 200, savedEdit(5));
    assert.equal(view().savedWork.pinned, false);
    assert.equal(view().savedWork.draftValue, "# Saved edit 5");
    assert.equal(view().savedHistory.open, control === "saved-history-picked");
    assert.deepEqual(env.hostCalls.filter(([name]) => name === "replaceUrl"), [["replaceUrl", "/meetings/m1#notes"]]);
    env.dispatch("meeting-change-received", { meetingId: "m1" });
    await env.answer(meetingRead, 200, notesBody());
    assert.deepEqual(notes(env).revisions, []);
  });
}

test("choosing another fixed edit closes the preview and fences the previous share read", async () => {
  const env = setup();
  env.context(meeting(), { href: fixedHref });
  await env.answer(meetingRead, 200, notesBody([savedEdit(3)]));
  await env.answer(fixedDetail, 200, savedEdit(2));
  env.dispatch("email-preview-clicked");
  await env.answer(fixedDetail, 200, savedEdit(2));
  assert.ok(selectView(env.state()).copyPreview);
  const action = { type: "notes/copy-clicked" };
  const { meta } = reduce(env.state(), action).effects.find((effect) => effect.type === "fetch");
  env.store.dispatch(action);
  const [pending] = env.waiting(fixedDetail);
  env.dispatch("saved-history-picked", { editVersion: 1, href: fixedHref });
  assert.equal(pending.signal.aborted, true);
  assert.equal(selectView(env.state()).copyPreview, null);
  env.dispatch("location-changed", { href: fixedHref });
  await env.answer(fixedDetail, 200, savedEdit(2));
  const before = env.state();
  env.dispatch("response", { meta, response: { status: 200, body: savedEdit(2) } });
  env.dispatch("request-failed", { meta, error: { status: 401, body: { detail: "Authentication required" } } });
  assert.equal(env.state(), before);
  assert.ok(!env.hostCalls.some(([name]) => name === "copy"));
  assert.equal(selectView(env.state()).actionMessage, "");
});

for (const mismatch of [{ id: "r2" }, { meeting_id: "m2" }, { edit_version: 3 }, { markdown: null }]) {
  test(`fixed copy rejects a mismatched ${Object.keys(mismatch)[0]}`, async () => {
    const env = setup();
    env.context(meeting(), { href: fixedHref });
    await env.answer(fixedDetail, 200, savedEdit(2));
    env.dispatch("copy-clicked");
    await env.answer(meetingRead, 200, notesBody([savedEdit(3)]));
    await env.answer(fixedDetail, 200, { ...savedEdit(2), ...mismatch });
    const view = selectView(env.state());
    assert.equal(view.savedWork.revision.markdown, "# Saved edit 2");
    assert.match(text(view.actionMessage), /Không thể đọc|không khớp|không trả về/);
    assert.ok(!env.hostCalls.some(([name]) => name === "copy"));
  });
}

for (const [confirmation, projection, control] of [
  ["detail", "pending", "saved-history-closed"],
  ["detail", "failed", "saved-history-picked"],
  ["detail", "omitted", "saved-history-closed"],
  ["index", "pending", "saved-history-picked"],
  ["index", "failed", "saved-history-closed"],
  ["index", "omitted", "saved-history-picked"],
]) {
  test(`${control} uses confirmed ${confirmation} with a ${projection} meeting projection`, async () => {
    const env = setup();
    const view = () => selectView(env.state());
    env.context(meeting(), { href: fixedHref });
    const other = revision({ id: "r2", markdown: "# Other revision" });
    if (projection === "failed") await env.answer(meetingRead, 503, {});
    if (projection === "omitted") await env.answer(meetingRead, 200, notesBody([other]));
    if (confirmation === "detail") {
      await env.answer(fixedDetail, 200, savedEdit(2));
      if (projection === "failed") await env.answer(historyIndex, 503, {});
    } else {
      await env.answer(historyIndex, 200, versionIndex(4));
      if (projection === "failed") await env.answer(fixedDetail, 404, {});
    }
    assert.equal(view().savedHistory.options[0].disabled, false);
    assert.equal(view().savedWork.selected, "r1");
    assert.equal(text(view().savedWork.options.find(({ value }) => value === "r1").text), "Bản lưu 2");
    assert.equal(view().savedWork.revision?.edit_version, confirmation === "detail" ? 2 : undefined);
    assert.deepEqual(notes(env).revisions.map((item) => item.id), projection === "omitted" ? ["r2"] : []);

    env.dispatch(control, { href: fixedHref, editVersion: null });
    assert.equal(view().savedHistory.latestPhase, "pending");
    assert.equal(view().savedHistory.selected, "2");
    const floor = confirmation === "detail" ? 2 : 4;
    await env.answer(targetRead, 200, savedEdit(floor - 1));
    assert.equal(view().savedHistory.latestPhase, "failed");
    assert.equal(view().savedWork.pinned, true);
    assert.equal(env.hostCalls.some(([name]) => name === "replaceUrl"), false);
    env.dispatch("saved-history-closed", { href: fixedHref });
    await env.answer(targetRead, 200, savedEdit(4));
    assert.equal(view().savedWork.pinned, false);
    assert.equal(view().savedWork.selected, "r1");
    assert.equal(view().savedWork.draftValue, "# Saved edit 4");
    assert.equal(view().savedHistory.open, control === "saved-history-picked");
    assert.deepEqual(env.hostCalls.find(([name]) => name === "replaceUrl"), ["replaceUrl", "/meetings/m1#notes"]);
    assert.equal(notes(env).known, projection !== "pending");
    assert.equal(Boolean(notes(env).readError), projection === "failed");
    if (projection === "pending") {
      await env.answer(meetingRead, 200, notesBody([other]));
      assert.deepEqual(notes(env).revisions.map((item) => item.id), ["r2", "r1"]);
      assert.equal(view().savedWork.draftValue, "# Saved edit 4");
    }
  });
}

test("an earlier projection can omit the reconciled Note while still replacing unrelated rows", async () => {
  const env = setup();
  const other = revision({ id: "r2", markdown: "# Other revision" });
  await opened(env, { revisions: [other, revision({ id: "r3" })] });
  env.dispatch("mode-chosen", { mode: "write" });
  env.dispatch("draft-edited", { value: "# Unrelated draft" });
  const draft = env.state().drafts.r2;
  env.dispatch("meeting-change-received", { meetingId: "m1" });
  env.dispatch("location-changed", { href: fixedHref });
  await env.answer(fixedDetail, 200, savedEdit(2));
  env.dispatch("saved-history-closed", { href: fixedHref });
  await env.answer(targetRead, 200, savedEdit(4));
  await env.answer(meetingRead, 200, notesBody([other]));
  assert.deepEqual(notes(env).revisions.map((item) => item.id), ["r2", "r1"]);
  assert.equal(selectView(env.state()).savedWork.draftValue, "# Saved edit 4");
  assert.equal(env.state().drafts.r2, draft);

  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(meetingRead, 200, notesBody([other]));
  assert.deepEqual(notes(env).revisions.map((item) => item.id), ["r2"]);
  assert.equal(env.state().drafts.r2, draft);
});

test("a pending current GET keeps the floor of a cached Note that the meeting list omits", async () => {
  const env = setup();
  await opened(env, { revisions: [savedEdit(4)] });
  env.dispatch("location-changed", { href: fixedHref });
  env.dispatch("saved-history-closed", { href: fixedHref });
  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(meetingRead, 200, notesBody());
  await env.answer(targetRead, 200, savedEdit(3));
  assert.equal(selectView(env.state()).savedHistory.latestPhase, "failed");

  env.dispatch("saved-history-closed", { href: fixedHref });
  await env.answer(targetRead, 200, { ...savedEdit(4), markdown: null });
  assert.equal(selectView(env.state()).savedHistory.latestPhase, "failed");
  assert.equal(env.hostCalls.some(([name]) => name === "replaceUrl"), false);

  env.dispatch("saved-history-closed", { href: fixedHref });
  await env.answer(targetRead, 200, savedEdit(4));
  assert.equal(selectView(env.state()).savedWork.draftValue, "# Saved edit 4");
});

async function historyAheadOfMeeting(env, { dirty = false } = {}) {
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([
    revision({ id: "r2", markdown: "# A different Notes revision" }), savedEdit(3),
  ]));
  if (dirty) {
    env.dispatch("revision-picked", { id: "r1" });
    env.dispatch("mode-chosen", { mode: "write" });
    env.dispatch("draft-edited", { value: "# Local draft" });
  }
  env.dispatch("location-changed", { href: fixedHref });
  await env.answer((r) => r.url === "/api/notes/r1/versions", 200, versionIndex(4));
  await env.answer((r) => r.url === "/api/notes/r1/versions/2", 200, savedEdit(2));
}

for (const action of ["saved-history-closed", "saved-history-picked"]) {
  test(`${action} reconciles a newer history index before enabling latest Notes`, async () => {
    const env = setup();
    await historyAheadOfMeeting(env);
    const view = () => selectView(env.state());
    const effects = (name) => env.hostCalls.filter(([call]) => call === name);
    env.dispatch(action, { href: fixedHref, editVersion: null });
    assert.equal(view().savedWork.pinned, true);
    assert.equal(view().savedWork.revision.markdown, "# Saved edit 2");
    assert.equal(effects("replaceUrl").length, 0);
    await env.answer(targetRead, 503, {});
    assert.equal(view().savedWork.pinned, true);
    assert.equal(view().savedWork.saveDisabled, true);
    assert.equal(view().savedHistory.selected, "2");
    assert.match(text(view().savedHistory.status), /Không thể đọc bản Notes mới nhất/);
    assert.equal(effects("replaceUrl").length, 0);
    env.dispatch("copy-clicked");
    await env.answer((r) => r.url === "/api/notes/r1/versions/2", 200, savedEdit(2));
    assert.deepEqual(effects("copy").at(-1), ["copy", "# Saved edit 2"]);

    env.dispatch("saved-history-closed", { href: fixedHref });
    await env.answer(targetRead, 200, savedEdit(4));
    assert.equal(view().savedHistory.open, action === "saved-history-picked");
    assert.equal(view().savedWork.pinned, false);
    assert.equal(view().savedWork.revision.id, "r1");
    assert.equal(view().savedWork.draftValue, "# Saved edit 4");
    assert.deepEqual(effects("replaceUrl"), [["replaceUrl", "/meetings/m1#notes"]]);
    env.dispatch("copy-clicked");
    await env.answer(targetRead, 200, savedEdit(4));
    assert.deepEqual(effects("copy").at(-1), ["copy", "# Saved edit 4"]);
  });

  for (const fixedDetail of ["visible", "loading"]) {
    test(`${action} refreshes latest when the first index is pending and fixed detail is ${fixedDetail}`, async () => {
      const env = setup();
      env.context();
      await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([savedEdit(3)]));
      env.dispatch("location-changed", { href: fixedHref });
      const [index] = env.waiting((r) => r.url === "/api/notes/r1/versions");
      if (fixedDetail === "visible") {
        await env.answer((r) => r.url === "/api/notes/r1/versions/2", 200, savedEdit(2));
      }
      env.dispatch(action, { href: fixedHref, editVersion: null });
      assert.equal(selectView(env.state()).savedWork.pinned, true);
      assert.equal(selectView(env.state()).savedWork.revision?.edit_version, fixedDetail === "visible" ? 2 : undefined);
      assert.equal(index.signal.aborted, false);
      await env.answer(targetRead, 200, savedEdit(4));
      const view = selectView(env.state());
      assert.equal(view.savedWork.pinned, false);
      assert.equal(view.savedWork.revision.edit_version, 4);
      assert.equal(view.savedHistory.open, action === "saved-history-picked");
      assert.equal(index.signal.aborted, true);
      env.dispatch("copy-clicked");
      await env.answer(targetRead, 200, savedEdit(4));
      assert.deepEqual(env.hostCalls.find(([name]) => name === "copy"), ["copy", "# Saved edit 4"]);
    });
  }
}

test("closing a chooser already on latest does not request Notes content", async () => {
  const env = setup();
  await opened(env, { revisions: [savedEdit(4)] });
  env.dispatch("saved-history-opened");
  env.dispatch("saved-history-closed");
  assert.equal(selectView(env.state()).savedHistory.open, false);
  assert.equal(env.requests.some(targetRead), false);
});

test("latest history reconciliation keeps a dirty draft and survives a late v3 meeting read", async () => {
  const env = setup();
  await historyAheadOfMeeting(env, { dirty: true });
  env.dispatch("saved-history-picked", { href: fixedHref, editVersion: null });
  await env.answer(targetRead, 200, savedEdit(3));
  assert.equal(selectView(env.state()).savedWork.pinned, true);
  env.dispatch("saved-history-closed", { href: fixedHref });
  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(targetRead, 200, savedEdit(4));
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([savedEdit(3)]));
  const work = selectView(env.state()).savedWork;
  assert.equal(work.revision.markdown, "# Saved edit 4");
  assert.equal(work.draftValue, "# Local draft");
  assert.equal(work.copyDisabled, true);
  assert.match(text(work.conflict.message), /edit 4/);
  env.dispatch("use-latest-clicked");
  assert.equal(selectView(env.state()).savedWork.draftValue, "# Saved edit 4");
});

for (const destination of ["another note", "another saved version", "another meeting", "Transcript", "home"]) {
  test(`latest history response is discarded after navigating to ${destination}`, async () => {
    const env = setup();
    if (destination === "another meeting" || destination === "Transcript") {
      env.context(meeting(), { href: fixedHref });
      await env.answer(historyIndex, 200, versionIndex(4));
    } else {
      await historyAheadOfMeeting(env);
    }
    const action = { type: "notes/saved-history-closed", href: fixedHref };
    const { meta } = reduce(env.state(), action).effects.find(targetReadEffect);
    env.store.dispatch(action);
    const [pending] = env.waiting(targetRead);
    assert.ok(pending);

    if (destination === "another note") {
      env.dispatch("revision-picked", { id: "r2", href: fixedHref });
    } else if (destination === "another saved version") {
      env.dispatch("saved-history-picked", { editVersion: 1, href: fixedHref });
    } else if (destination === "another meeting") {
      env.context(meeting("m2"));
    } else if (destination === "Transcript") {
      env.context(meeting(), { detail: { view: "meeting", meeting: meeting(), tab: "transcript" }, href: fixedHref });
    } else {
      env.dispatch("context", { detail: { view: "home" } });
    }
    assert.equal(pending.signal.aborted, true);
    const before = env.state();
    const calls = env.hostCalls.length;
    // Exercise the reducer fence independently of the runner's abort.
    env.dispatch("response", { meta, response: { status: 200, body: savedEdit(4) } });
    env.dispatch("request-failed", { meta, error: { status: 503 } });
    assert.equal(env.state(), before);
    assert.equal(env.hostCalls.length, calls);
  });
}

function targetReadEffect(effect) {
  return effect.type === "fetch" && effect.init.method === "GET" && effect.url === "/api/notes/r1";
}

const historyAuthError = { status: 401, body: { detail: "Authentication required" } };

for (const destination of ["close", "current chooser", "another edit", "another fixed link", "another meeting"]) {
  for (const op of ["saved-history-list", "saved-history-detail"]) {
    test(`${op} cannot end the session after ${destination}`, async () => {
      const env = setup();
      await opened(env, { revisions: [savedEdit(3), revision({ id: "r2" })] });
      const open = { type: "notes/location-changed", href: fixedHref };
      const { meta } = reduce(env.state(), open).effects.find((effect) => effect.meta?.op === op);
      env.store.dispatch(open);
      const [pending] = env.waiting(op === "saved-history-list" ? historyIndex : fixedDetail);
      if (destination === "close") {
        env.dispatch("saved-history-closed", { href: fixedHref });
        await env.answer(targetRead, 200, savedEdit(4));
      } else if (destination === "current chooser") {
        env.dispatch("saved-history-opened");
      } else if (destination === "another edit") {
        env.dispatch("saved-history-picked", { editVersion: 1, href: fixedHref });
      } else if (destination === "another fixed link") {
        env.dispatch("location-changed", { href: fixedHref.replace("r1", "r2") });
      } else {
        env.context(meeting("m2"));
      }

      const before = env.state();
      const failed = reduce(before, { type: "notes/request-failed", meta, error: historyAuthError });
      assert.deepEqual(failed, { state: before });
      const body = op === "saved-history-list" ? versionIndex(5) : savedEdit(2);
      assert.deepEqual(reduce(before, { type: "notes/response", meta, response: { body } }), { state: before });
      assert.equal(pending.signal.aborted, true);
    });
  }
}

for (const op of ["saved-history-list", "saved-history-detail"]) {
  test(`only the active ${op} can end the session`, async () => {
    const env = setup();
    await opened(env, { revisions: [savedEdit(3)], task: TASK });
    const open = { type: "notes/location-changed", href: fixedHref };
    const { meta } = reduce(env.state(), open).effects.find((effect) => effect.meta?.op === op);
    env.store.dispatch(open);
    const state = env.state();
    const failure = { type: "notes/request-failed", error: historyAuthError };
    for (const mismatch of [
      { meetingId: "m2" }, { noteId: "r2" }, { requestId: meta.requestId - 1 },
      ...(op === "saved-history-detail" ? [{ editVersion: 1 }] : []),
    ]) {
      assert.deepEqual(reduce(state, { ...failure, meta: { ...meta, ...mismatch } }), { state });
    }
    const expired = reduce(state, { ...failure, meta });
    assert.ok(expired.effects.some((effect) => effect.type === "navigate" && effect.url === "/access"));
    assert.equal(expired.state.poll.scheduled, false);
    assert.equal(expired.state.drafts, state.drafts);
    const body = op === "saved-history-list" ? versionIndex(4) : savedEdit(2);
    env.dispatch("response", { meta, response: { body } });
    const settled = env.state();
    assert.deepEqual(reduce(settled, { ...failure, meta }), { state: settled });
  });
}

test("a post-save history refresh supersedes the old list for the same selection", async () => {
  const env = setup();
  await opened(env, { revisions: [savedEdit(3)] });
  const open = { type: "notes/saved-history-opened" };
  const { meta } = reduce(env.state(), open).effects.find((effect) => effect.meta?.op === "saved-history-list");
  env.store.dispatch(open);
  const [pending] = env.waiting(historyIndex);
  env.dispatch("draft-edited", { value: "# Saved edit 4" });
  env.dispatch("save-clicked");
  await env.answer((r) => r.method === "PATCH", 200, savedEdit(4));

  const state = env.state();
  assert.deepEqual(reduce(state, {
    type: "notes/request-failed", meta, error: historyAuthError,
  }), { state });
  assert.equal(pending.signal.aborted, true);
  await env.answer(historyIndex, 200, versionIndex(4));
  assert.ok(selectView(env.state()).savedHistory.options.some((option) => option.value === "4"));
});

test("fixed Notes links accept a trailing slash and reject decoded slashes", () => {
  assert.deepEqual(savedVersionFromHref(fixedHref.replace("/m1?", "/m%31/?")), {
    meetingId: "m1", noteId: "r1", editVersion: 2,
  });
  assert.equal(savedVersionFromHref(fixedHref.replace("/m1?", "/m1%2Fm2?")), null);
});

test("explicit equivalent fixed navigation cancels latest, while background context preserves it", async () => {
  const env = setup();
  await historyAheadOfMeeting(env);
  const current = { type: "notes/saved-history-closed", href: fixedHref };
  const { meta } = reduce(env.state(), current).effects.find(targetReadEffect);
  env.store.dispatch(current);
  const [pending] = env.waiting(targetRead);
  const equivalent = `${ORIGIN}/meetings/m1?notes_edit=2&notes_revision=r1#notes`;
  env.context(meeting(), { href: equivalent });
  assert.equal(pending.signal.aborted, false);
  assert.equal(selectView(env.state()).savedHistory.latestPhase, "pending");

  env.dispatch("location-changed", { href: equivalent });
  assert.equal(pending.signal.aborted, true);
  const state = env.state();
  assert.equal(selectView(state).savedHistory.latestPhase, "");
  assert.equal(selectView(state).savedWork.revision.edit_version, 2);
  assert.deepEqual(reduce(state, {
    type: "notes/response", meta, response: { body: savedEdit(5) },
  }), { state });
  assert.deepEqual(reduce(state, {
    type: "notes/request-failed", meta, error: historyAuthError,
  }), { state });
});

test("an address change to current Notes retains the fixed selection until latest is verified", async () => {
  const env = setup();
  await historyAheadOfMeeting(env, { dirty: true });
  const href = `${ORIGIN}/meetings/m1#notes`;
  env.context(meeting(), { href });
  assert.equal(selectView(env.state()).savedWork.revision.edit_version, 2);
  assert.equal(env.waiting(targetRead).length, 0);

  env.dispatch("location-changed", { href });
  assert.equal(selectView(env.state()).savedHistory.latestPhase, "pending");
  await env.answer(targetRead, 200, savedEdit(3));
  assert.equal(selectView(env.state()).savedHistory.latestPhase, "failed");
  assert.equal(selectView(env.state()).savedWork.revision.edit_version, 2);
  env.dispatch("saved-history-closed", { href });
  await env.answer(targetRead, 200, savedEdit(4));
  const work = selectView(env.state()).savedWork;
  assert.equal(work.pinned, false);
  assert.equal(work.revision.edit_version, 4);
  assert.equal(work.draftValue, "# Local draft");
  assert.ok(work.conflict);
  assert.equal(env.hostCalls.some(([name]) => name === "replaceUrl"), false);
});

test("latest history reconciliation respects a higher version learned while the read is pending", async () => {
  const env = setup();
  await historyAheadOfMeeting(env);
  env.dispatch("saved-history-picked", { editVersion: 1, href: fixedHref });
  await env.answer((r) => r.url === "/api/notes/r1/versions/1", 200, savedEdit(1));
  const href = fixedHref.replace("notes_edit=2", "notes_edit=1");
  env.dispatch("saved-history-closed", { href });
  await env.answer((r) => r.url === "/api/notes/r1/versions", 200, versionIndex(5));
  await env.answer(targetRead, 200, savedEdit(4));
  assert.equal(selectView(env.state()).savedWork.pinned, true);
  assert.equal(selectView(env.state()).savedWork.revision.edit_version, 1);
  env.dispatch("saved-history-closed", { href });
  await env.answer(targetRead, 200, savedEdit(5));
  assert.equal(selectView(env.state()).savedWork.revision.edit_version, 5);
});

test("a retry requires its own response for the exact meeting and Notes revision", async () => {
  const env = setup();
  env.context(meeting(), { href: fixedHref });
  await env.answer(historyIndex, 200, versionIndex(4));
  await env.answer(fixedDetail, 200, savedEdit(2));
  const action = { type: "notes/saved-history-closed", href: fixedHref };
  const { meta } = reduce(env.state(), action).effects.find(targetReadEffect);
  env.store.dispatch(action);
  await env.answer(targetRead, 200, { ...savedEdit(4), id: "r2" });
  assert.equal(selectView(env.state()).savedWork.pinned, true);

  env.store.dispatch(action);
  env.dispatch("response", { meta, response: { status: 200, body: savedEdit(4) } });
  assert.equal(selectView(env.state()).savedWork.pinned, true);
  await env.answer(targetRead, 200, { ...savedEdit(4), meeting_id: "m2" });
  assert.equal(selectView(env.state()).savedWork.pinned, true);

  env.store.dispatch(action);
  await env.answer(targetRead, 200, savedEdit(4));
  assert.equal(selectView(env.state()).savedWork.pinned, false);
});

for (const mismatch of [{ id: "r2" }, { meeting_id: "m2" }]) {
  test(`fixed-history replies with the wrong ${Object.keys(mismatch)[0]} cannot confirm the Note`, async () => {
    const env = setup();
    env.context(meeting(), { href: fixedHref });
    await env.answer(historyIndex, 200, { ...versionIndex(4), ...mismatch });
    await env.answer(fixedDetail, 200, { ...savedEdit(2), ...mismatch });
    const requested = selectView(env.state());
    assert.equal(requested.savedWork.revision, null);
    assert.equal(requested.savedHistory.options[0].disabled, true);
    assert.equal(text(requested.savedHistory.closeLabel), "Đóng lịch sử");
    env.dispatch("saved-history-closed", { href: fixedHref });
    assert.equal(env.requests.some((r) => /^\/api\/notes\/[^/]+$/.test(r.url)), false);
    assert.equal(selectView(env.state()).savedHistory.open, false);
  });
}

test("a fixed link without a matching revision offers a separate exit", async () => {
  const env = setup();
  const href = `${ORIGIN}/meetings/m1?notes_revision=missing&notes_edit=2#notes`;
  env.context(meeting(), { href });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer((r) => r.url === "/api/notes/missing/versions", 404, {});
  await env.answer((r) => r.url === "/api/notes/missing/versions/2", 404, {});
  const requested = selectView(env.state());
  assert.equal(requested.savedWork.selected, "missing");
  assert.equal(requested.savedWork.revision, null);
  assert.equal(requested.savedHistory.options.find((item) => item.value === "").disabled, true);
  assert.equal(text(requested.savedHistory.closeLabel), "Đóng lịch sử");
  env.dispatch("saved-history-picked", { href, editVersion: null });
  assert.equal(selectView(env.state()).savedWork.pinned, true);

  env.dispatch("saved-history-closed", { href });
  const closed = selectView(env.state());
  assert.equal(closed.savedHistory.open, false);
  assert.equal(closed.savedWork.revision.id, "r1");
});

test("a retained legacy baseline explains unavailable earlier Saves after selecting a later one", async () => {
  const env = setup();
  await opened(env, { revisions: [revision({ edit_version: 5 })] });
  env.dispatch("saved-history-opened");
  const versions = {
    id: "r1", meeting_id: "m1",
    versions: [
      { note_id: "r1", edit_version: 4, origin: "legacy", saved_at: "2026-11-01 10:00:00" },
      { note_id: "r1", edit_version: 5, origin: "operator", saved_at: "2026-11-02 12:00:00" },
    ],
  };
  await env.answer((r) => r.url === "/api/notes/r1/versions", 200, versions);
  assert.match(text(selectView(env.state()).savedHistory.legacyNotice), /bản lưu 4.*Bản AI gốc/);
  env.dispatch("saved-history-picked", { editVersion: 5, href: `${ORIGIN}/meetings/m1` });
  await env.answer(env.is("GET", "/api/notes/r1/versions/5"), 200, revision({ edit_version: 5 }));
  const selected = selectView(env.state());
  assert.equal(selected.savedHistory.selected, "5");
  assert.match(text(selected.savedHistory.legacyNotice), /bản lưu 4.*Bản AI gốc/);
});

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
  const read = env.is("GET", "/api/meetings/m1/notes");
  env.fire(1000); // The worker was running when this poll started.
  env.dispatch("cancel-clicked");
  env.dispatch("cancel-clicked"); // busy: ignored
  assert.equal(env.requests.filter(env.is("POST", "/api/tasks/t1/cancel")).length, 1);
  assert.equal(selectView(env.state()).task.cancelDisabled, true);
  await env.answer(env.is("POST", "/api/tasks/t1/cancel"), 200, { ...TASK, status: "stopping" });
  await env.answer(read, 200, notesBody([], TASK));
  assert.equal(notes(env).task.status, "stopping");
  assert.equal(selectView(env.state()).task.cancelDisabled, true);
  assert.equal(notes(env).sourceLocked, true);
  env.dispatch("cancel-clicked");
  assert.equal(posts(env, "/cancel").length, 1);

  env.fire(1000);
  await env.answer(read, 200, notesBody([], { ...TASK, status: "canceled" }));
  assert.equal(notes(env).task.status, "canceled");
  assert.equal(notes(env).sourceLocked, false);
});

test("a late cancellation receipt leaves a different active task and its lock in place", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  const later = { ...TASK, id: "task-newer", request_id: "other-operator" };
  const read = env.is("GET", "/api/meetings/m1/notes");
  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(read, 200, notesBody([revision()], later));
  await env.answer(env.is("POST", "/api/tasks/t1/cancel"), 200, { ...TASK, status: "canceled" });

  assert.equal(notes(env).task.id, later.id);
  assert.equal(notes(env).sourceLocked, true);
  assert.equal(selectView(env.state()).task.cancelDisabled, false);
  env.dispatch("cancel-clicked");
  assert.equal(posts(env, "/cancel").at(-1).url, "/api/tasks/task-newer/cancel");
});

test("Cancel remains available for the current task while an older cancellation is pending", async () => {
  for (const oldStatus of [200, 503]) {
    const env = setup();
    await opened(env, { task: TASK });
    env.dispatch("cancel-clicked");
    const later = { ...TASK, id: "task-newer", request_id: "other-operator" };
    env.dispatch("meeting-change-received", { meetingId: "m1" });
    await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()], later));

    assert.equal(selectView(env.state()).task.cancelDisabled, false);
    env.dispatch("cancel-clicked");
    env.dispatch("cancel-clicked");
    assert.deepEqual(posts(env, "/cancel").map(({ url }) => url), [
      "/api/tasks/t1/cancel", "/api/tasks/task-newer/cancel",
    ]);

    await env.answer(env.is("POST", "/api/tasks/t1/cancel"), oldStatus, { ...TASK, status: "canceled" });
    assert.equal(notes(env).task.id, later.id);
    assert.equal(selectView(env.state()).task.cancelDisabled, true);
    assert.equal(text(notes(env).runError), "");
    await env.fail(env.is("POST", "/api/tasks/task-newer/cancel"));
    assert.equal(selectView(env.state()).task.cancelDisabled, false);
    assert.ok(text(notes(env).runError));
  }
});

test("a failed cancel request is reported and the button works again", async () => {
  const env = setup();
  await opened(env, { task: TASK });
  env.dispatch("cancel-clicked");
  await env.fail(env.is("POST", "/api/tasks/t1/cancel"));
  assert.equal(selectView(env.state()).task.cancelDisabled, false);
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

test("regenerate dialog exposes current and historical transcript sources and closes without a request", async () => {
  const current = { ...GENERATION, generation_number: 2, source_version: 3 };
  const historical = { id: "g0", status: "complete", generation_number: 1, source_version: 1, segments: [] };
  const customMeeting = {
    ...meeting(),
    transcription: { generation: current, generation_history: [historical], task: null },
  };
  const env = setup();
  await openedMeeting(env, customMeeting, {
    revisions: [revision()],
    catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["medium", "high"] }] },
  });
  assert.deepEqual(generationChoices(customMeeting).map((item) => item.id), ["g0", "g1"]);
  env.dispatch("regenerate-clicked");
  const open = selectView(env.state()).regenerateDialog;
  assert.equal(open.open, true);
  assert.deepEqual(open.sourceItems.map((item) => item.value), ["g0", "g1"]);
  assert.equal(open.generationId, "g1");
  assert.equal(open.sourceVersion, 3);
  env.dispatch("regenerate-cancelled");
  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  assert.equal(posts(env, "/notes-runs").length, 0);
  assert.equal(env.requests.filter((request) => request.method === "PUT").length, 0);
});

test("explicit regeneration sends only the typed run recipe, preserves legacy guards, and writes no preference", async () => {
  const current = { ...GENERATION, generation_number: 2, source_version: 3 };
  const historical = { id: "g0", status: "complete", generation_number: 1, source_version: 1, segments: [] };
  const customMeeting = {
    ...meeting(),
    transcription: { generation: current, generation_history: [historical], task: null },
  };
  const env = setup();
  await openedMeeting(env, customMeeting, {
    revisions: [revision()],
    catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["medium", "high"] }] },
    preferences: { provider: "chatgpt", models: {}, reasoning_effort: "default" },
  });
  env.dispatch("regenerate-clicked");
  assert.equal(env.state().choice.preferences.models.chatgpt, undefined);
  env.dispatch("regenerate-effort-picked", { reasoningEffort: "high" });
  env.dispatch("regenerate-source-picked", { generationId: "g0" });
  env.dispatch("regenerate-language-picked", { language: "vi" });
  env.dispatch("regenerate-submit", { requestId: "req-run-0001" });
  const body = posts(env, "/notes-runs")[0].body;
  assert.deepEqual(body, {
    request_id: "req-run-0001",
    expected_generation_id: "g1",
    expected_source_version: 3,
    expected_metadata_version: 3,
    output_language: "vi",
    run: {
      provider: "chatgpt",
      model: "m1",
      reasoning_effort: "high",
      generation_id: "g0",
      source_version: 1,
    },
  });
  assert.equal(Object.hasOwn(body, "provider"), false);
  assert.equal(Object.hasOwn(body, "expected_model"), false);
  assert.equal(Object.hasOwn(body, "expected_reasoning_effort"), false);
  assert.deepEqual(env.state().pending.m1, body);
  assert.equal(env.requests.filter((request) => request.method === "PUT").length, 0);
});

test("explicit regeneration keeps the exact request across an uncertain retry and ignores a stale response after cancel", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()], catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["high"] }] } });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-effort-picked", { reasoningEffort: "high" });
  env.dispatch("regenerate-submit", { requestId: "req-run-0002" });
  const firstBody = posts(env, "/notes-runs")[0].body;
  await env.fail(env.is("POST", "/notes-runs"));
  assert.equal(notes(env).runErrorUncertain, true);
  assert.deepEqual(env.state().pending.m1, firstBody);
  assert.equal(
    text(selectView(env.state()).regenerateDialog.status),
    "Yêu cầu này đã được giữ nguyên. Thử lại sẽ dùng cùng các lựa chọn.",
  );
  assert.doesNotMatch(text(selectView(env.state()).regenerateDialog.status), /JSON|capability/);
  assert.equal(selectView(env.state()).regenerateDialog.submitLabel.text, "Xác minh yêu cầu notes");
  env.dispatch("regenerate-submit", { requestId: "req-run-replaced" });
  assert.deepEqual(posts(env, "/notes-runs")[1].body, firstBody);

  const beforeCancel = env.state();
  env.dispatch("regenerate-cancelled");
  assert.equal(notes(env).starting, false);
  assert.deepEqual(env.state().pending.m1, firstBody);
  const cancelledMessage = notes(env).runError;
  env.store.dispatch({
    type: "notes/response",
    meta: { op: "start", meetingId: "m1", requestId: firstBody.request_id, explicit: true, attempt: beforeCancel.startAttempts.m1 },
    response: { status: 202, body: { ...TASK, status: "accepted" } },
  });
  assert.equal(notes(env).runError, cancelledMessage);
  assert.deepEqual(env.state().pending.m1, firstBody);
});

test("explicit regeneration blocks disconnected providers and catalog failures before POST", async () => {
  const disconnected = setup();
  await opened(disconnected, { revisions: [revision()] });
  disconnected.dispatch("regenerate-clicked");
  disconnected.dispatch("regenerate-provider-picked", { provider: "antigravity" });
  disconnected.dispatch("regenerate-submit", { requestId: "req-run-0003" });
  assert.equal(posts(disconnected, "/notes-runs").length, 0);
  assert.match(text(selectView(disconnected.state()).regenerateDialog.error), /chưa sẵn sàng/);

  const failed = setup();
  failed.context();
  await failed.answer(failed.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await failed.answer(failed.is("GET", "/api/providers"), 200, PROVIDERS);
  await failed.answer(failed.is("GET", "/api/notes-preferences"), 200, PREFS);
  await failed.fail(failed.is("GET", "/api/providers/chatgpt/models"));
  failed.dispatch("regenerate-clicked");
  failed.dispatch("regenerate-submit", { requestId: "req-run-0004" });
  assert.equal(posts(failed, "/notes-runs").length, 0);
  assert.match(text(selectView(failed.state()).regenerateDialog.error), /Chưa tải được danh sách model/);
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
  assert.equal(selectView(env.state()).task.cancelDisabled, false);
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
  const explicit = {
    ...base,
    expected_generation_id: "current",
    expected_source_version: 2,
    expected_metadata_version: 3,
    run: {
      provider: "chatgpt",
      model: "m1",
      reasoning_effort: "high",
      generation_id: "historical",
      source_version: 1,
    },
  };
  assert.deepEqual(normalizePendingRequest(JSON.stringify(explicit)), explicit);
  assert.equal(normalizePendingRequest(JSON.stringify({ ...explicit, provider: "chatgpt" })), null);
  assert.equal(normalizePendingRequest(JSON.stringify({ ...explicit, expected_model: "m1" })), null);
  assert.equal(normalizePendingRequest(JSON.stringify({ ...explicit, expected_reasoning_effort: "high" })), null);
  assert.equal(normalizePendingRequest(JSON.stringify({ ...explicit, run: { ...explicit.run, extra: true } })), null);
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

test("a superseded source seek reports the newer timestamp and ignores a late card answer", async () => {
  const env = setup();
  await opened(env);
  const ref = { meetingId: "m1", snapshotId: "s9", segmentId: "seg1" };
  env.dispatch("source-opened", { reference: ref, fromClick: false });
  await env.answer(env.is("GET", "/api/notes-sources/s9"), 200, SNAPSHOT("s9"));
  const id = env.state().source.id;
  env.dispatch("source-sought", { meta: { id }, value: { superseded: true } });
  assert.match(text(env.state().source.status), /Tua tới đây/);
  const before = env.state().source.status;
  env.dispatch("source-seek-answered", {
    meta: { id: id - 1, source: env.state().source.current },
    value: true,
  });
  assert.equal(env.state().source.status, before);
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

test("raw deletion keeps the unselected newline spelling when line endings are mixed", async () => {
  for (const [anchor, expected] of [[1, "A\nB"], [2, "A\r\nB"]]) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown: "A\r\n\nB" })] });
    env.dispatch("mode-chosen", { mode: "write" });
    env.dispatch("draft-edited", { value: "A\nB", selection: { anchor, focus: anchor + 1 } });
    env.dispatch("save-clicked");
    assert.equal(env.waiting(env.is("PATCH", "/api/notes/r1"))[0].body.markdown, expected);
  }
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

test("a pipe-containing list line interrupts a table and keeps its citation scope", async () => {
  const env = setup();
  const bullet = "Follow | now";
  const markdown = "# Decisions\n\nA | B\n--- | ---\n- Follow | now\n";
  await opened(env, { revisions: [revision({
    markdown,
    display_citations: [{ section: "Decisions", text: bullet, sources: [{ id: "seg2", start_seconds: 10 }] }],
  })] });
  const view = selectView(env.state()).savedWork;
  const table = view.blocks.find((block) => block.kind === "table");
  const list = view.blocks.find((block) => block.kind === "list");
  assert.equal(table.rows.length, 1);
  assert.equal(list.items[0].nodes.map((node) => node.text).join(""), bullet);
  assert.equal(list.items[0].chips[0].segmentId, "seg2");
});

test("a non-one ordered marker stays paragraph text unless separated by a blank line", async () => {
  for (const [separator, expectedKinds] of [["\n", ["heading", "raw"]], ["\n\n", ["heading", "paragraph", "list"]]]) {
    const env = setup();
    const markdown = `# Notes\n\nSchedule${separator}2. is tentative\n8. is confirmed\n`;
    await opened(env, { revisions: [revision({ markdown })] });
    const view = selectView(env.state()).savedWork;
    assert.deepEqual(view.blocks.map((block) => block.kind), expectedKinds);
    if (separator === "\n") {
      assert.equal(view.blocks[1].text, "Schedule\n2. is tentative\n8. is confirmed");
      env.dispatch("mode-chosen", { mode: "rich" });
      assert.equal(selectView(env.state()).savedWork.richFallback, true);
      assert.equal(saved(env).value, markdown);
    } else {
      assert.equal(view.blocks[2].start, 2);
    }
  }
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

test("rich projection decodes supported amp entities once and preserves authored offsets", async () => {
  const env = setup();
  const markdown = "# Notes\n\n[Report](https://example.invalid/?a=1&amp;b=2) and **R&amp;D** and \\&amp;.\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  let runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
  const link = runs.find((node) => node.kind === "link" && node.text === "Report");
  assert.equal(link.href, "https://example.invalid/?a=1&b=2");
  const strongIndex = runs.findIndex((node) => node.kind === "strong" && node.text === "R&D");
  assert.notEqual(strongIndex, -1);
  assert.ok(runs.some((node) => node.kind === "text" && node.text.includes("&amp;")));

  env.dispatch("rich-edited", { index: strongIndex, before: "R&D", value: "R&DX" });
  assert.equal(saved(env).value, markdown.replace("R&amp;D", "R&amp;DX"));
});

test("rich selection maps deletion to the selected authored entity", async () => {
  for (const [selected, remaining] of [[1, "&AMP;"], [2, "&amp;"]]) {
    const env = setup();
    const markdown = "# Notes\n\nA&amp;&AMP;B\n";
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const runs = selectView(env.state()).savedWork.blocks.flatMap(blockRuns);
    const index = runs.findIndex((node) => node.text === "A&&B");
    env.dispatch("rich-edited", {
      index,
      before: "A&&B",
      value: "A&B",
      selection: { anchor: selected, focus: selected + 1 },
    });
    assert.equal(saved(env).value, `# Notes\n\nA${remaining}B\n`);
  }
});

test("rich edits without a usable selection do not guess between authored entities", async () => {
  for (const markdown of ["# Notes\n\nA&amp;&AMP;B\n", "# Notes\n\nA&amp;&AMP;&AMP;B\n", "# Notes\n\nA&&amp;B\n"]) {
    const env = setup();
    await opened(env, { revisions: [revision({ markdown })] });
    env.dispatch("mode-chosen", { mode: "rich" });
    const index = selectView(env.state()).savedWork.blocks.flatMap(blockRuns)
      .findIndex((node) => node.text === "A&&B");
    env.dispatch("rich-edited", { index, before: "A&&B", value: "A&B" });
    assert.equal(saved(env).value, markdown);
  }
});

test("unsupported entities stay literal instead of entering rich emphasis or links", async () => {
  const env = setup();
  const markdown = "# Notes\n\n**R&#169;D** [link](https://example.invalid/?a=1&copy;b=2)\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const work = selectView(env.state()).savedWork;
  assert.equal(work.mode, "write");
  assert.equal(work.richFallback, true);
  assert.equal(work.draftValue, markdown);
  assert.ok(work.blocks.some((block) => block.kind === "paragraph"
    && block.lines.flat().some((node) => node.kind === "raw" && node.text.includes("**R&#169;D**"))));

  const literals = setup();
  await opened(literals, { revisions: [revision({ markdown: "# Notes\n\n&AMP; &Amp; &frac12;\n" })] });
  const line = selectView(literals.state()).savedWork.blocks.find((block) => block.kind === "paragraph").lines[0];
  assert.equal(line.map((node) => node.text).join(""), "& &Amp; &frac12;");
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

test("initial projection keeps intraword punctuation delimiters literal", async () => {
  const env = setup();
  const markdown = "# Notes\n\nNet a*.*b\n";
  await opened(env, { revisions: [revision({ markdown })] });
  env.dispatch("mode-chosen", { mode: "rich" });
  const work = selectView(env.state()).savedWork;
  const line = work.blocks.find((block) => block.kind === "paragraph").lines[0];
  assert.equal(line.map((node) => node.text).join(""), "Net a*.*b");
  assert.equal(line.filter((node) => ["em", "strong"].includes(node.kind)).length, 0);
  assert.equal(work.mode, "write");
  assert.equal(work.richFallback, true);
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

test("loose ordered lists fall back as one block while other list markers stay stable", async () => {
  const loose = setup();
  const looseMarkdown = "# Notes\n\n1. First\n\n1. Second\n";
  await opened(loose, { revisions: [revision({ markdown: looseMarkdown })] });
  loose.dispatch("mode-chosen", { mode: "rich" });
  const looseWork = selectView(loose.state()).savedWork;
  assert.equal(looseWork.mode, "write");
  assert.equal(looseWork.richFallback, true);
  assert.equal(looseWork.blocks.filter((block) => block.kind === "list").length, 0);
  assert.equal(looseWork.blocks.filter((block) => block.kind === "raw").length, 1);
  assert.match(looseWork.blocks.find((block) => block.kind === "raw").text, /1\. First\n\n1\. Second/);

  const valid = setup();
  const validMarkdown = "# Notes\n\n3. First\n4. Second\n";
  await opened(valid, { revisions: [revision({ markdown: validMarkdown })] });
  valid.dispatch("mode-chosen", { mode: "rich" });
  const validList = selectView(valid.state()).savedWork.blocks.find((block) => block.kind === "list");
  assert.equal(validList.start, 3);
  assert.deepEqual(validList.items.map((item) => item.nodes.map((node) => node.text).join("")), ["First", "Second"]);

  const distinct = setup();
  const distinctMarkdown = "# Notes\n\n1. First\n\n1) Second\n";
  await opened(distinct, { revisions: [revision({ markdown: distinctMarkdown })] });
  distinct.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(distinct.state()).savedWork.blocks.filter((block) => block.kind === "list").length, 2);

  const bullets = setup();
  const bulletMarkdown = "# Notes\n\n- First\n\n- Second\n";
  await opened(bullets, { revisions: [revision({ markdown: bulletMarkdown })] });
  bullets.dispatch("mode-chosen", { mode: "rich" });
  assert.equal(selectView(bullets.state()).savedWork.blocks.filter((block) => block.kind === "list").length, 2);
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

// Preview repair: exercise asynchronous loading through the real runner.
async function reloadRunChoices(env, { providers = PROVIDERS, preferences = PREFS, catalog = CATALOG } = {}) {
  env.dispatch("regenerate-reload-clicked");
  await env.answer(env.is("GET", "/api/providers"), 200, providers);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, preferences);
  const provider = providers.providers.find((item) => item.status === "connected").id;
  await env.answer(env.is("GET", `/api/providers/${provider}/models`), 200, catalog);
}

test("regenerate catalog retry fills untouched defaults without a global model", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, { models: {} });
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 503, {});
  env.dispatch("regenerate-clicked");
  assert.equal(selectView(env.state()).regenerateDialog.reloadVisible, true);
  await reloadRunChoices(env, { preferences: { models: {}, reasoning_effort: "high" }, catalog: { models: [{ id: "m2", display_name: "M2", reasoning_efforts: ["high"] }] } });
  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.model, "m2");
  assert.equal(view.reasoningEffort, "high");
  assert.equal(view.submitDisabled, false);
  assert.equal(text(view.error), "");
  assert.deepEqual(env.state().choice.preferences.models, {});
  assert.equal(env.requests.some((request) => request.method === "PUT"), false);
});

test("regenerate auto-load replaces an untouched disconnected provider", async () => {
  const env = setup();
  env.context();
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 503, {});
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  env.dispatch("regenerate-clicked");
  await env.answer(env.is("GET", "/api/providers"), 200, { providers: [{ id: "antigravity", status: "connected" }] });
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, { models: {} });
  await env.answer(env.is("GET", "/api/providers/antigravity/models"), 200, { models: [{ id: "gemini", display_name: "Gemini" }] });
  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.provider, "antigravity");
  assert.equal(view.model, "gemini");
  assert.equal(view.submitDisabled, false);
});

test("regenerate reload retains explicit unavailable provider, model and effort choices", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-model-picked", { model: "removed" });
  env.dispatch("regenerate-effort-picked", { reasoningEffort: "high" });
  await reloadRunChoices(env);
  assert.equal(env.state().regenerate.model, "removed");
  assert.equal(env.state().regenerate.reasoningEffort, "high");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /Model đã chọn/);
  env.dispatch("regenerate-provider-picked", { provider: "antigravity" });
  await reloadRunChoices(env);
  assert.equal(env.state().regenerate.provider, "antigravity");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /chưa sẵn sàng/);
});

test("regenerate confirmed defaults remain visible when a refreshed catalog drops them", async () => {
  const env = setup();
  await opened(env, {
    catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["high"] }] },
    preferences: { ...PREFS, reasoning_effort: "high" },
  });
  env.dispatch("regenerate-clicked");
  await reloadRunChoices(env, { catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["medium"] }] } });
  assert.equal(env.state().regenerate.reasoningEffort, "high");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /Mức suy nghĩ đã chọn/);
  await reloadRunChoices(env, { catalog: { models: [{ id: "m2", display_name: "M2", reasoning_efforts: ["medium"] }] } });
  assert.equal(env.state().regenerate.model, "m1");
  assert.equal(env.state().regenerate.reasoningEffort, "high");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /Model đã chọn/);
  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.submitDisabled, true);
  assert.deepEqual(view.modelItems.map((item) => ({ value: item.value, text: text(item.text), disabled: Boolean(item.disabled) })), [
    { value: "m1", text: "m1 — không khả dụng với tài khoản này", disabled: true },
    { value: "m2", text: "M2", disabled: false },
  ]);
});

test("regenerate output-language edits keep a confirmed model provider across status refresh", async () => {
  const env = setup();
  await opened(env, {
    catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["high"] }] },
    preferences: { ...PREFS, reasoning_effort: "high" },
  });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-language-picked", { language: "vi" });

  await reloadRunChoices(env, {
    providers: { providers: [{ id: "chatgpt", status: "disconnected" }, { id: "antigravity", status: "connected" }] },
    preferences: { provider: "antigravity", models: { antigravity: "gemini" }, reasoning_effort: "medium" },
    catalog: { models: [{ id: "gemini", display_name: "Gemini", reasoning_efforts: ["medium"] }] },
  });

  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.provider, "chatgpt");
  assert.equal(view.model, "m1");
  assert.equal(view.outputLanguage, "vi");
  assert.ok(view.providerItems.some((item) => item.value === "chatgpt" && item.disabled));
  assert.equal(view.submitDisabled, true);
  assert.match(text(view.error), /ChatGPT chưa sẵn sàng/);
  assert.equal(posts(env, "/notes-runs").length, 0);
});

test("regenerate stale source requires explicit adoption without submitting", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: "req-source-repair" });
  await env.answer(env.is("POST", "/notes-runs"), 409, { detail: { code: "source_version_changed" } });
  assert.ok(text(env.state().regenerate.error));
  assert.equal(text(notes(env).runError), "");
  env.context({ ...meeting(), transcription: { generation: { ...GENERATION, source_version: 3 }, task: null } });
  const stale = selectView(env.state()).regenerateDialog;
  assert.equal(stale.sourceVersion, 2);
  assert.match(text(stale.sourceItems.find((item) => item.value === stale.generationId).text), /nguồn 2$/);
  assert.deepEqual(stale.sourceRefresh, { generationId: "g1", sourceVersion: 3 });
  assert.match(text(stale.sourceRefreshLabel), /phiên bản 3/);
  assert.equal(stale.submitDisabled, true);
  const requestCount = env.requests.length;
  env.dispatch("regenerate-source-adopted");
  const adopted = selectView(env.state()).regenerateDialog;
  assert.equal(adopted.sourceVersion, 3);
  assert.match(text(adopted.sourceItems.find((item) => item.value === adopted.generationId).text), /nguồn 3$/);
  assert.equal(adopted.sourceRefresh, null);
  assert.equal(adopted.sourceRefreshLabel, "");
  assert.equal(adopted.submitDisabled, false);
  assert.equal(text(adopted.error), "");
  assert.equal(env.requests.length, requestCount);
  env.context({ ...meeting(), transcription: { generation: { ...GENERATION, id: "g2" }, task: null } });
  assert.equal(selectView(env.state()).regenerateDialog.sourceRefresh, null);
  env.dispatch("regenerate-source-adopted");
  assert.equal(env.state().regenerate.generationId, "g1");
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, true);
});

test("regenerate errors clear on unlocked edits and reopening; validation takes precedence", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("start-clicked", { requestId: "req-legacy-error" });
  await env.answer(env.is("POST", "/notes-runs"), 400, { detail: { code: "invalid_request" } });
  assert.ok(text(notes(env).runError));
  env.dispatch("regenerate-clicked");
  assert.equal(text(selectView(env.state()).regenerateDialog.error), "");
  for (const [type, extra] of [
    ["regenerate-provider-picked", { provider: "chatgpt" }],
    ["regenerate-model-picked", { model: "m1" }],
    ["regenerate-effort-picked", { reasoningEffort: "default" }],
    ["regenerate-source-picked", { generationId: "g1" }],
    ["regenerate-language-picked", { language: "vi" }],
  ]) {
    env.dispatch("regenerate-submit", { requestId: `req-error-${type}` });
    await env.answer(env.is("POST", "/notes-runs"), 400, { detail: { code: "invalid_request" } });
    assert.ok(text(env.state().regenerate.error));
    env.dispatch(type, extra);
    assert.equal(text(selectView(env.state()).regenerateDialog.error), "");
  }
  env.dispatch("regenerate-submit", { requestId: "req-error-reopen" });
  await env.answer(env.is("POST", "/notes-runs"), 400, { detail: { code: "invalid_request" } });
  env.dispatch("regenerate-cancelled");
  env.dispatch("regenerate-clicked");
  assert.equal(text(selectView(env.state()).regenerateDialog.error), "");
  const state = env.state();
  const invalid = { ...state, regenerate: { ...state.regenerate, model: "removed", error: "old dialog error" }, notes: { ...state.notes, m1: { ...notes(env), runError: "old legacy error" } } };
  assert.match(text(selectView(invalid).regenerateDialog.error), /Model đã chọn/);
});

test("regenerate async reload and adoption preserve frozen uncertain retry", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: "req-frozen-repair" });
  const original = posts(env, "/notes-runs")[0].body;
  await env.fail(env.is("POST", "/notes-runs"));
  const values = env.state().regenerate;
  await reloadRunChoices(env, { catalog: { models: [{ id: "m2", display_name: "M2" }] } });
  env.context({ ...meeting(), transcription: { generation: { ...GENERATION, source_version: 3 }, task: null } });
  assert.equal(selectView(env.state()).regenerateDialog.sourceRefresh, null);
  for (const [type, extra] of [["regenerate-source-adopted", {}], ["regenerate-model-picked", { model: "m2" }], ["regenerate-provider-picked", { provider: "antigravity" }], ["regenerate-effort-picked", { reasoningEffort: "high" }]]) env.dispatch(type, extra);
  assert.deepEqual(env.state().regenerate, values);
  env.dispatch("regenerate-submit", { requestId: "req-replacement" });
  assert.deepEqual(posts(env, "/notes-runs").at(-1).body, original);
});

test("regenerate retains an explicit effort that a refreshed model no longer supports", async () => {
  const env = setup();
  await opened(env, { catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["high"] }] } });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-effort-picked", { reasoningEffort: "high" });
  await reloadRunChoices(env, { catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["medium"] }] } });
  assert.equal(env.state().regenerate.reasoningEffort, "high");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /Mức suy nghĩ đã chọn/);
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, true);
});

test("regenerate choices picked while catalogs load survive the arriving response", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-reload-clicked");
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  env.dispatch("regenerate-model-picked", { model: "" });
  env.dispatch("regenerate-effort-picked", { reasoningEffort: "high" });
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(env.state().regenerate.model, "");
  assert.equal(env.state().regenerate.reasoningEffort, "high");
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, true);
});

test("regenerate mapped rejection reloads choices and a fresh open clears its submission error", async () => {
  const env = setup();
  await opened(env);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: "req-mapped-recovery" });
  await env.answer(env.is("POST", "/notes-runs"), 422, { detail: { code: "model_unavailable" } });
  assert.ok(text(env.state().regenerate.error));
  assert.equal(text(notes(env).runError), "");
  env.dispatch("regenerate-cancelled");
  env.dispatch("regenerate-clicked");
  assert.match(text(selectView(env.state()).regenerateDialog.error), /Đang đọc trạng thái/);
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, false);
  assert.equal(text(selectView(env.state()).regenerateDialog.error), "");
});


test("definitive Verify rejection keeps the restored destination until an explicit correction", async () => {
  const env = setup();
  const frozen = {
    request_id: TASK.request_id,
    expected_generation_id: "g1",
    expected_source_version: 2,
    expected_metadata_version: 3,
    output_language: "vi",
    run: { provider: "antigravity", model: "previous-gemini", reasoning_effort: "high", generation_id: "g1", source_version: 2 },
  };
  env.context(meeting(), { stored: { pending: JSON.stringify(frozen) } });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);

  env.dispatch("start-clicked", { requestId: "ignored" });
  await env.answer(env.is("POST", "/notes-runs"), 503, {});
  env.dispatch("regenerate-submit", { requestId: "also-ignored" });
  await env.answer(env.is("POST", "/notes-runs"), 409, { detail: { code: "provider_disconnected" } });
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));

  const refused = selectView(env.state()).regenerateDialog;
  assert.equal(refused.provider, frozen.run.provider);
  assert.equal(refused.model, frozen.run.model);
  assert.equal(refused.reasoningEffort, frozen.run.reasoning_effort);
  assert.equal(refused.outputLanguage, frozen.output_language);
  assert.equal(refused.submitDisabled, true);
  env.dispatch("regenerate-submit", { requestId: "must-not-send" });
  assert.deepEqual(posts(env, "/notes-runs").map(({ body }) => body), [frozen, frozen]);

  env.dispatch("regenerate-provider-picked", { provider: "chatgpt" });
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, false);
  env.dispatch("regenerate-submit", { requestId: "new-corrected-request" });
  const corrected = posts(env, "/notes-runs").at(-1).body;
  assert.equal(corrected.request_id, "new-corrected-request");
  assert.equal(corrected.run.provider, "chatgpt");
  assert.equal(corrected.run.model, "m1");
  assert.equal(corrected.output_language, "vi");
  assert.equal(env.requests.some((entry) => entry.method === "PUT"), false);
});

test("regenerate model or effort picks pin their provider across status refresh without a provider pick", async () => {
  for (const [type, extra] of [
    ["regenerate-model-picked", { model: "m1" }],
    ["regenerate-effort-picked", { reasoningEffort: "high" }],
  ]) {
    const env = setup();
    await opened(env, { catalog: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["high"] }] } });
    env.dispatch("regenerate-clicked");
    env.dispatch(type, extra);
    const before = selectView(env.state()).regenerateDialog;
    await reloadRunChoices(env, {
      providers: { providers: [{ id: "chatgpt", status: "disconnected" }, { id: "antigravity", status: "connected" }] },
      preferences: { provider: "antigravity", models: { antigravity: "gemini" } },
      catalog: { models: [{ id: "gemini", display_name: "Gemini" }] },
    });
    const view = selectView(env.state()).regenerateDialog;
    assert.equal(view.provider, "chatgpt");
    assert.ok(view.providerItems.some((item) => item.value === "chatgpt" && item.disabled));
    assert.equal(view.model, before.model);
    assert.equal(view.reasoningEffort, before.reasoningEffort);
    assert.equal(view.submitDisabled, true);
    assert.match(text(view.error), /ChatGPT chưa sẵn sàng/);
    assert.equal(posts(env, "/notes-runs").length, 0);
  }
});

test("a background failure settles the originating Notes request without changing the other dialog", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-language-picked", { language: "vi" });
  env.dispatch("regenerate-submit", { requestId: TASK.request_id });
  const frozen = env.state().pending.m1;

  env.context(meeting("m2"));
  await env.answer(env.is("GET", "/api/meetings/m2/notes"), 200, notesBody([revision({ id: "r2", meeting_id: "m2" })]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: "req-other-meeting" });
  const otherDialog = env.state().regenerate;
  await env.fail(env.is("POST", "/api/meetings/m1/notes-runs"));

  assert.equal(env.state().meetingId, "m2");
  assert.deepEqual(env.state().regenerate, otherDialog);
  assert.equal(notes(env, "m2").starting, true);
  assert.equal(env.state().pending.m2.request_id, "req-other-meeting");
  assert.equal(notes(env).starting, false);
  assert.deepEqual(env.state().pending.m1, frozen);

  env.context(meeting());
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  assert.equal(selectView(env.state()).run.disabled, false);
  env.dispatch("start-clicked", { requestId: "ignored-retry-id" });
  assert.deepEqual(posts(env, "/notes-runs").at(-1).body, frozen);
});

test("a Notes read confirming the exact request settles its dialog before a late POST response", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: TASK.request_id });
  const post = env.waiting(env.is("POST", "/notes-runs"))[0];
  assert.ok(post);

  env.context(meeting());
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()], TASK, true));
  assert.equal(env.state().pending.m1, undefined);
  assert.equal(notes(env).starting, false);
  assert.equal(notes(env).task.id, TASK.id);
  assert.equal(selectView(env.state()).regenerateDialog.open, false);

  // The confirming read wins even if the old POST callback arrives with a network error.
  post.done = true;
  post.reject(new TypeError("offline"));
  await tick();
  assert.equal(notes(env).task.id, TASK.id);
  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  assert.equal(env.state().pending.m1, undefined);
});

test("an older admission receipt settles without replacing the task observed during the request", async () => {
  for (const status of ["running", "succeeded"]) {
    const env = setup();
    await opened(env, { revisions: [revision()] });
    env.dispatch("regenerate-clicked");
    env.dispatch("regenerate-submit", { requestId: TASK.request_id });
    const later = { ...TASK, id: "task-newer", request_id: "other-operator", status };
    const revisions = [revision({ id: "published-a" }), revision()];
    const read = env.is("GET", "/api/meetings/m1/notes");
    env.dispatch("meeting-change-received", { meetingId: "m1" });
    await env.answer(read, 200, notesBody(revisions, later));
    await env.answer(env.is("POST", "/notes-runs"), 202, { ...TASK, status: "accepted" });

    assert.equal(notes(env).task.id, later.id);
    assert.equal(selectView(env.state()).regenerateDialog.open, false);
    assert.equal(env.state().pending.m1, undefined);
    assert.equal(selectView(env.state()).savedWork.options.length, 2);
    assert.equal(notes(env).sourceLocked, status === "running");
    assert.equal(env.waiting(read).length, 1);
    if (status === "running") {
      env.dispatch("cancel-clicked");
      assert.equal(posts(env, "/cancel").at(-1).url, "/api/tasks/task-newer/cancel");
    }
  }
});

test("a read issued before Notes admission cannot hide the newly accepted run", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.context(meeting());
  const read = env.is("GET", "/api/meetings/m1/notes");
  env.dispatch("regenerate-submit", { requestId: TASK.request_id });
  await env.answer(env.is("POST", "/notes-runs"), 202, TASK);
  await env.answer(read, 200, notesBody([revision()]));

  const running = selectView(env.state());
  assert.equal(notes(env).task?.id, TASK.id);
  assert.equal(notes(env).sourceLocked, true);
  assert.equal(running.task.cancelHidden, false);
  assert.equal(running.run.disabled, true);
  assert.deepEqual(env.live(), [1000]);

  env.fire(1000);
  await env.answer(read, 200, notesBody([revision({ id: "r2" }), revision()], { ...TASK, status: "succeeded" }));
  const complete = selectView(env.state());
  assert.equal(complete.task.cancelHidden, true);
  assert.equal(complete.savedWork.options.length, 2);
  assert.equal(notes(env).sourceLocked, false);
  assert.deepEqual(env.live(), []);
});

test("a background admission leaves the current meeting's outstanding Notes read valid", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: TASK.request_id });
  env.context(meeting("m2"));
  await env.answer(env.is("POST", "/api/meetings/m1/notes-runs"), 202, TASK);
  await env.answer(env.is("GET", "/api/meetings/m2/notes"), 200, notesBody([revision({ id: "r2", meeting_id: "m2" })]));

  const view = selectView(env.state());
  assert.equal(notes(env, "m1").task.id, TASK.id);
  assert.equal(view.savedWork.revision.id, "r2");
  assert.equal(view.task, null);
  assert.deepEqual(env.live(), []);
});

test("an uncertain retry after reload displays the exact frozen recipe even without current options", async () => {
  const env = setup();
  const frozen = {
    request_id: TASK.request_id,
    expected_generation_id: "g1",
    expected_source_version: 2,
    expected_metadata_version: 3,
    output_language: "vi",
    run: {
      provider: "antigravity",
      model: "previous-model",
      reasoning_effort: "high",
      generation_id: "saved-transcript",
      source_version: 7,
    },
  };
  env.context(meeting(), { stored: { pending: JSON.stringify(frozen) } });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);

  const retry = selectView(env.state());
  assert.equal(retry.choice.planHidden, false);
  assert.equal(retry.choice.provider, frozen.run.provider);
  assert.equal(retry.choice.providerDisabled, true);
  assert.equal(retry.choice.modelValue, frozen.run.model);
  assert.equal(retry.choice.modelDisabled, true);
  assert.match(text(retry.choice.plan), /Google Antigravity · previous-model · mức suy nghĩ Cao/);
  assert.match(text(retry.choice.policy), /Google Antigravity/);
  assert.match(retry.run.describedBy, /notes-run-plan/);

  env.dispatch("start-clicked", { requestId: "ignored-retry-id" });
  assert.deepEqual(posts(env, "/notes-runs").at(-1).body, frozen);
  await env.fail(env.is("POST", "/notes-runs"));

  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.open, true);
  assert.equal(view.locked, true);
  assert.equal(view.provider, frozen.run.provider);
  assert.equal(view.model, frozen.run.model);
  assert.equal(view.reasoningEffort, frozen.run.reasoning_effort);
  assert.equal(view.generationId, frozen.run.generation_id);
  assert.equal(view.sourceVersion, frozen.run.source_version);
  assert.equal(view.outputLanguage, frozen.output_language);
  assert.ok(view.modelItems.some((item) => item.value === frozen.run.model));
  assert.ok(view.sourceItems.some((item) => item.value === frozen.run.generation_id));
  assert.deepEqual(env.state().pending.m1, frozen);
});

test("a locked recovered recipe names its pinned version when the same generation has changed", async () => {
  const env = setup();
  const frozen = {
    request_id: TASK.request_id,
    expected_generation_id: "g1",
    expected_source_version: 2,
    expected_metadata_version: 3,
    output_language: "vi",
    run: {
      provider: "chatgpt",
      model: "m1",
      reasoning_effort: "default",
      generation_id: "g1",
      source_version: 1,
    },
  };
  env.context(meeting(), { stored: { pending: JSON.stringify(frozen) } });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()]));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  env.dispatch("regenerate-clicked");

  const view = selectView(env.state()).regenerateDialog;
  assert.equal(view.locked, true);
  assert.equal(view.sourceVersion, 1);
  const displayed = view.sourceItems.find((item) => item.value === view.generationId);
  assert.match(text(displayed.text), /nguồn 1(?:\D|$)/);
  assert.deepEqual(env.state().pending.m1, frozen);
});

test("confirming a request after the dialog was closed clears its unconfirmed status", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: TASK.request_id });
  env.dispatch("regenerate-cancelled");
  assert.equal(notes(env).runErrorUncertain, true);
  assert.match(text(selectView(env.state()).run.label), /Xác minh yêu cầu notes/);

  env.context(meeting());
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200, notesBody([revision()], TASK, true));
  const view = selectView(env.state());
  assert.equal(env.state().pending.m1, undefined);
  assert.equal(notes(env).runErrorUncertain, false);
  assert.equal(text(view.error), "");
  assert.doesNotMatch(text(view.status), /Xác minh yêu cầu notes/);
  assert.match(text(view.task.status), /đang tạo notes/);
  assert.equal(view.regenerateDialog.open, false);
});

test("restoring a pending per-run language keeps the meeting's default after completion", async () => {
  const submitting = setup();
  await opened(submitting, { revisions: [revision()] });
  submitting.dispatch("regenerate-clicked");
  submitting.dispatch("regenerate-language-picked", { language: "vi" });
  submitting.dispatch("regenerate-submit", { requestId: TASK.request_id });
  const frozen = submitting.state().pending.m1;

  const restored = setup();
  restored.context(meeting(), { stored: { pending: JSON.stringify(frozen), language: "en" } });
  assert.equal(selectView(restored.state()).outputLanguage, "vi");
  await restored.answer(restored.is("GET", "/api/meetings/m1/notes"), 200,
    notesBody([revision()], { ...TASK, status: "succeeded", output_language: "vi" }));
  await restored.answer(restored.is("GET", "/api/providers"), 200, PROVIDERS);
  await restored.answer(restored.is("GET", "/api/notes-preferences"), 200, PREFS);
  await restored.answer(restored.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);

  assert.equal(restored.state().pending.m1, undefined);
  assert.equal(selectView(restored.state()).outputLanguage, "en");
  restored.dispatch("regenerate-clicked");
  assert.equal(selectView(restored.state()).regenerateDialog.outputLanguage, "en");
  assert.ok(!restored.written.some(([, op, key]) => op === "write" && key === outputLanguageStorageKey("m1")));
});

test("regenerate retains its draft while a concurrently admitted notes task holds the source", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-language-picked", { language: "vi" });
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, false);

  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200,
    notesBody([revision()], { ...TASK, status: "running", request_id: "other-operator" }, true));
  const blocked = selectView(env.state()).regenerateDialog;
  assert.equal(blocked.open, true);
  assert.equal(blocked.outputLanguage, "vi");
  assert.equal(blocked.submitDisabled, true);
  assert.match(text(blocked.status), /Chờ tác vụ kết thúc/);
  env.dispatch("regenerate-submit", { requestId: "our-rejected-attempt" });
  assert.equal(posts(env, "/notes-runs").length, 0);

  env.dispatch("meeting-change-received", { meetingId: "m1" });
  await env.answer(env.is("GET", "/api/meetings/m1/notes"), 200,
    notesBody([revision()], { ...TASK, status: "succeeded", request_id: "other-operator" }));
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
  const unblocked = selectView(env.state()).regenerateDialog;
  assert.equal(unblocked.open, true);
  assert.equal(unblocked.outputLanguage, "vi");
  assert.equal(unblocked.submitDisabled, false);
  assert.equal(text(unblocked.status), "");
  env.dispatch("regenerate-submit", { requestId: "our-accepted-attempt" });
  assert.equal(posts(env, "/notes-runs").length, 1);
  assert.equal(posts(env, "/notes-runs")[0].body.output_language, "vi");
});


test("late toolbar Verify keeps the error in Notes without opening a modal over Transcript", async () => {
  const env = setup();
  await opened(env, { revisions: [revision()] });
  env.dispatch("regenerate-clicked");
  env.dispatch("regenerate-submit", { requestId: "scoped-verify-first" });
  const original = posts(env, "/notes-runs")[0].raw;
  assert.ok(typeof original === "string" && original.length > 0);
  await env.fail(env.is("POST", "/notes-runs"));
  env.dispatch("regenerate-cancelled");
  env.dispatch("start-clicked", { requestId: "ignored-toolbar-id" });
  const current = env.state().meeting;
  env.context(current, { detail: { view: "meeting", meeting: current, tab: "transcript" } });
  await env.answer(env.is("POST", "/notes-runs"), 503, { detail: { code: "temporary_failure" } });

  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  assert.match(text(selectView(env.state()).status), /Yêu cầu chưa được tự gửi lại/);
  env.context(current);
  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  assert.equal(selectView(env.state()).run.disabled, false);
  env.dispatch("start-clicked", { requestId: "ignored-returned-id" });
  assert.deepEqual(posts(env, "/notes-runs").map((post) => post.raw), [original, original, original]);
});

test("definitive toolbar Verify rejection in Transcript lets Notes recover the exact recipe", async () => {
  const env = setup();
  const frozen = {
    request_id: "refused-background-verify",
    expected_generation_id: "g1",
    expected_source_version: 2,
    expected_metadata_version: 3,
    output_language: "vi",
    run: { provider: "antigravity", model: "gemini", reasoning_effort: "medium", generation_id: "g0", source_version: 7 },
  };
  const current = meeting();
  current.transcription.generation_history = [{ id: "g0", status: "complete", source_version: 7 }];
  await openedRestored(env, current, frozen);
  env.dispatch("start-clicked", { requestId: "ignored-toolbar-id" });
  env.context(current, { detail: { view: "meeting", meeting: current, tab: "transcript" } });
  await env.answer(env.is("POST", "/notes-runs"), 409, { detail: { code: "provider_disconnected" } });
  await answerSavedNotesChoices(env);
  await env.answer(env.is("GET", "/m1/notes"), 200, notesBody([revision()]));
  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  assert.equal(env.state().pending.m1, undefined);

  env.context(current);
  const returned = selectView(env.state());
  assert.equal(returned.regenerateDialog.open, false);
  assert.ok(returned.error);
  assert.equal(returned.run.action, "regenerate");
  env.dispatch("regenerate-clicked");
  const dialog = selectView(env.state()).regenerateDialog;
  assert.equal(dialog.open, true);
  assert.equal(dialog.locked, false);
  assert.deepEqual(
    [dialog.provider, dialog.model, dialog.reasoningEffort, dialog.generationId, dialog.sourceVersion, dialog.outputLanguage],
    ["antigravity", "gemini", "medium", "g0", 7, "vi"],
  );
  assert.equal(dialog.submitDisabled, true);
  const selectedProvider = dialog.providerItems.find((item) => item.value === dialog.provider);
  assert.equal(selectedProvider.disabled, true);
  env.dispatch("regenerate-submit", { requestId: "must-not-send" });
  assert.deepEqual(posts(env, "/notes-runs").map((post) => post.body), [frozen]);

  env.dispatch("regenerate-provider-picked", { provider: "chatgpt" });
  assert.equal(selectView(env.state()).regenerateDialog.submitDisabled, false);
  env.dispatch("regenerate-submit", { requestId: "corrected-background-verify" });
  const corrected = posts(env, "/notes-runs").at(-1).body;
  assert.deepEqual(corrected, {
    ...frozen,
    request_id: "corrected-background-verify",
    run: { ...frozen.run, provider: "chatgpt", model: "m1", reasoning_effort: "default" },
  });
  assert.equal(env.requests.some((request) => request.method === "PUT"), false);
  assert.equal(env.written.some(([, op, key]) => op === "write" && key === outputLanguageStorageKey("m1")), false);
});

test("a definitive refusal on another meeting preserves only the originating correction", async () => {
  const env = setup();
  const frozen = {
    request_id: "refused-other-meeting",
    expected_generation_id: "g1",
    expected_source_version: 2,
    expected_metadata_version: 3,
    output_language: "vi",
    run: { provider: "antigravity", model: "older-model", reasoning_effort: "high", generation_id: "g1", source_version: 2 },
  };
  const current = meeting();
  await openedRestored(env, current, frozen);
  env.dispatch("start-clicked", { requestId: "ignored-toolbar-id" });

  env.context(meeting("m2"));
  await env.answer(env.is("GET", "/m2/notes"), 200, notesBody([revision({ id: "r2", meeting_id: "m2" })]));
  await answerSavedNotesChoices(env);
  env.dispatch("regenerate-clicked");
  const otherDialog = selectView(env.state()).regenerateDialog;
  await env.answer(env.is("POST", "/m1/notes-runs"), 422, { detail: { code: "model_unavailable" } });
  assert.deepEqual(selectView(env.state()).regenerateDialog, otherDialog);
  assert.equal(env.state().pending.m1, undefined);
  assert.equal(notes(env, "m2").regenerateCorrection, null);

  env.context(current);
  await env.answer(env.is("GET", "/m1/notes"), 200, notesBody([revision()]));
  await answerSavedNotesChoices(env);
  assert.equal(selectView(env.state()).regenerateDialog.open, false);
  env.dispatch("regenerate-clicked");
  assert.equal(selectView(env.state()).regenerateDialog.model, "older-model");
  env.dispatch("regenerate-cancelled");
  assert.equal(selectView(env.state()).error, "");
  env.dispatch("regenerate-clicked");
  const fresh = selectView(env.state()).regenerateDialog;
  assert.deepEqual([fresh.provider, fresh.model, fresh.outputLanguage], ["chatgpt", "m1", "en"]);
  assert.deepEqual(posts(env, "/notes-runs").map((post) => post.body), [frozen]);
});

async function answerSavedNotesChoices(env) {
  await env.answer(env.is("GET", "/api/providers"), 200, PROVIDERS);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, CATALOG);
}

async function openedRestored(env, current, frozen) {
  env.context(current, { stored: { pending: JSON.stringify(frozen) } });
  await env.answer(env.is("GET", "/m1/notes"), 200, notesBody([revision()]));
  await answerSavedNotesChoices(env);
}
