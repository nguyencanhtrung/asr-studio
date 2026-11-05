import assert from "node:assert/strict";
import test from "node:test";

import { combine, createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  TAB_PENDING_MS,
  addDays,
  defaultTab,
  displayDate,
  formatRecordingDuration,
  generationsFor,
  initialState,
  locatedTab,
  meetingIdFromPathname,
  mergeTranscription,
  monthGrid,
  reduce,
  failureOf,
  selectCalendar,
  selectDeletion,
  selectEditor,
  selectHeading,
  selectSidebar,
  validMeetingDate,
  validYear,
} from "../../src/asr_studio/web/history-core.js";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const HOME = { pathname: "/", search: "", hash: "", tab: null };
const at = (path = "/", extra = {}) => ({ ...HOME, pathname: path, ...extra });

const GENERATION = { id: "g1", generation_number: 1, source_version: 2, status: "complete", segments: [], speakers: [] };
const meeting = (id = "m1", extra = {}) => ({
  id,
  title: `Meeting ${id}`,
  metadata_version: 3,
  meeting_date: "2025-03-04",
  language_hint: "en",
  notes_source_locked: false,
  transcription: { generation: GENERATION, task: null },
  ...extra,
});
const LIST = { meetings: [meeting("m1"), meeting("m2", { meeting_date: null })], recent: [meeting("m1")], total: 2 };

// The real store and runner over fakes: requests wait in `requests` until a test answers them. The address is a
// variable the test moves, and every action is stamped with it as the view does.
function setup({ path = "/", search = "", hash = "" } = {}) {
  const requests = [];
  const timers = [];
  const calls = [];
  const address = { pathname: path, search, hash, tab: null };
  const fetch = (url, init) =>
    new Promise((resolve, reject) => {
      requests.push({ url, method: init.method, body: init.body && JSON.parse(init.body), resolve, reject, done: false, signal: init.signal });
    });
  const run = createRunner({
    fetch,
    setTimeout: (callback, delay) => timers.push({ callback, delay, live: true }) - 1,
    clearTimeout: (id) => {
      timers[id].live = false;
    },
    calls: new Proxy({}, { get: (_, name) => (...args) => calls.push([name, ...args]) }),
  });
  const stamp = (action) => ({ ...action, at: { ...address } });
  const store = createStore({
    reduce: combine({ history: reduce }),
    initialState: { history: initialState },
    run: (effect, send) => run(effect, (action) => send(stamp(action))),
  });
  const dispatch = (type, extra = {}) => store.dispatch(stamp({ type: `history/${type}`, ...extra }));
  const open = (item) => (r) => !r.done && !r.signal.aborted && item(r);
  const is = (method, path_) => (r) => r.method === method && r.url === path_;
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
  const waiting = (match) => requests.filter(open(match));
  const fire = (delay) => {
    const timer = timers.find((item) => item.live && item.delay === delay);
    assert.ok(timer, `no timer ${delay}`);
    timer.live = false;
    timer.callback();
  };
  const live = () => timers.filter((item) => item.live).map((item) => item.delay);
  return { store, state: () => store.getState().history, dispatch, address, requests, answer, fail, waiting, fire, live, is, calls };
}

const text = (message) => {
  if (message === null || message === undefined) return "";
  if (typeof message !== "object") return String(message);
  if (Array.isArray(message)) return message.map(text).join("");
  let out = message.text;
  for (const [name, value] of Object.entries(message.params ?? {})) out = out.replaceAll(`{${name}}`, text(value));
  return out;
};

// Boot at `path`, answer the list, and (for a meeting address) the meeting.
async function booted(path = "/", id = null) {
  const env = setup({ path });
  env.dispatch("booted", { today: "2025-03-04" });
  await env.answer(env.is("GET", "/api/meetings"), 200, LIST);
  if (id) await env.answer(env.is("GET", `/api/meetings/${id}`), 200, meeting(id));
  return env;
}

const GET = (id) => (r) => r.method === "GET" && r.url === `/api/meetings/${id}`;

// ---- date helpers ----

test("date helpers are pure date arithmetic", () => {
  assert.equal(addDays("2024-02-28", 2), "2024-03-01");
  assert.equal(addDays("2025-01-01", -1), "2024-12-31");
  assert.equal(addDays("2025-03-04", 7), "2025-03-11");
  assert.equal(displayDate("2025-03-04"), "04/03/2025");
  const grid = monthGrid(2025, 0); // January 2025 starts on a Wednesday
  assert.equal(grid.length, 42);
  assert.deepEqual(grid.slice(0, 3), ["2024-12-30", "2024-12-31", "2025-01-01"]);
  assert.equal(monthGrid(2025, 5)[0], "2025-05-26"); // June 2025 starts on a Sunday
  assert.equal(monthGrid(1000, 0).length, 42);
  assert.equal(validYear("2025"), 2025);
  assert.equal(validYear("999"), null);
  assert.equal(validYear("20250"), null);
  assert.equal(validYear("0999"), null);
});

test("recording duration reads in seconds, minutes, or hours and minutes", () => {
  assert.equal(text(formatRecordingDuration(45)), "45 giây");
  assert.equal(text(formatRecordingDuration(59.5)), "1 phút");
  assert.equal(text(formatRecordingDuration(720)), "12 phút");
  assert.equal(text(formatRecordingDuration(5040)), "1 giờ 24 phút");
});

test("the meeting id comes from the address path", () => {
  assert.equal(meetingIdFromPathname("/meetings/abc"), "abc");
  assert.equal(meetingIdFromPathname("/meetings/abc/"), "abc");
  assert.equal(meetingIdFromPathname("/meetings/a%20b"), "a b");
  assert.equal(meetingIdFromPathname("/meetings/a%2Fb"), null);
  assert.equal(meetingIdFromPathname("/meetings/%E0"), null);
  assert.equal(meetingIdFromPathname("/"), null);
});

test("the tab comes from the hash, the entry, or a saved-source link, else null", () => {
  assert.equal(locatedTab(at("/meetings/m1", { hash: "#notes" })), "notes");
  assert.equal(locatedTab(at("/meetings/m1", { tab: "transcript" })), "transcript");
  assert.equal(locatedTab(at("/meetings/m1", { hash: "#other", tab: "notes" })), "notes");
  assert.equal(locatedTab(at("/meetings/m1", { search: "?notes_source=s&segment=x" })), "transcript");
  assert.equal(locatedTab(at("/meetings/m1", { search: "?notes_source=s" })), null);
});

test("transcriptions merge: the page keeps the newer generation and its edits", () => {
  const old = { ...GENERATION, source_version: 5, segments: [{ id: "s", text: "edited" }] };
  const previous = { id: "m1", transcription: { generation: old, generation_history: [] } };
  const incoming = { id: "m1", transcription: { generation: { ...GENERATION, source_version: 3 }, generation_history: [], task: "t" } };
  const merged = mergeTranscription(previous, incoming);
  assert.equal(merged.transcription.generation, old);
  assert.equal(merged.transcription.task, "t");
  assert.equal(incoming.transcription.generation.source_version, 3, "the server's object is not touched");
  assert.equal(mergeTranscription(null, incoming), incoming);
  assert.equal(mergeTranscription({ id: "other", transcription: previous.transcription }, incoming), incoming);
  assert.deepEqual(generationsFor(null), []);
});

// ---- boot, home, calendar ----

test("boot at home shows home, fetches the list and keeps today selected", async () => {
  const env = await booted("/");
  const s = env.state();
  assert.equal(s.view, "home");
  assert.deepEqual(s.calendar, { year: 2025, month: 2, selected: "2025-03-04" });
  assert.equal(s.total, 2);
  assert.equal(s.contextRev, 1, "home tells notes.js");
  assert.equal(text(selectHeading(s).title), "Cuộc họp");
  assert.equal(selectHeading(s).documentTitle, "Workspace · ASR Studio");
});

test("the calendars share one state: month steps, year entry, month choice, today and undated", async () => {
  const env = await booted("/");
  env.dispatch("calendar-step-clicked", { step: -3 });
  assert.deepEqual(env.state().calendar, { year: 2024, month: 11, selected: "2025-03-04" });
  env.dispatch("calendar-year-entered", { text: "199" });
  assert.equal(env.state().calendar.year, 2024, "partial text is left alone");
  env.dispatch("calendar-year-entered", { text: "2031" });
  assert.equal(env.state().calendar.year, 2031);
  env.dispatch("calendar-month-chosen", { month: "5" });
  assert.equal(env.state().calendar.month, 5);
  env.dispatch("calendar-date-clicked", { date: "2025-01-02", mini: false });
  assert.deepEqual(env.state().calendar, { year: 2025, month: 0, selected: "2025-01-02" });
  env.dispatch("calendar-arrow-pressed", { date: "2025-01-02", step: 7 });
  assert.equal(env.state().calendar.selected, "2025-01-09");
  env.dispatch("calendar-arrow-pressed", { date: "9999-12-31", step: 1 });
  assert.equal(env.state().calendar.selected, "2025-01-09", "the year range ends at 9999");
  env.dispatch("calendar-undated-clicked", { mini: false });
  assert.equal(env.state().calendar.selected, "undated");
  assert.equal(env.state().view, "home");
  env.dispatch("calendar-today-clicked", { today: "2025-03-04" });
  assert.deepEqual(env.state().calendar, { year: 2025, month: 2, selected: "2025-03-04" });
  env.dispatch("calendar-step-clicked", { step: 1 });
  for (let step = 0; step < 3; step += 1) env.dispatch("calendar-year-entered", { text: "9999" });
  env.dispatch("calendar-month-chosen", { month: "11" });
  env.dispatch("calendar-step-clicked", { step: 1 });
  assert.deepEqual([env.state().calendar.year, env.state().calendar.month], [9999, 11], "no month past the last year");
});

test("the calendar selector counts the month, the undated group and the day panel", async () => {
  const env = await booted("/");
  const view = selectCalendar(env.state());
  assert.equal(text(view.summary), "2 cuộc họp trong tháng".replace("2", "1"));
  assert.equal(view.undatedCount, 1);
  assert.equal(view.day.meetings.length, 1);
  assert.equal(view.cells.length, 42);
  env.dispatch("calendar-undated-clicked", { mini: false });
  const undated = selectCalendar(env.state());
  assert.equal(text(undated.day.title), "Chưa đặt ngày");
  assert.equal(undated.day.meetings[0].id, "m2");
  env.dispatch("calendar-date-clicked", { date: "2025-03-20", mini: false });
  assert.equal(text(selectCalendar(env.state()).day.emptyText), "Không có cuộc họp trong ngày này.");
});

test("a mini calendar click opens the large calendar once, refreshing the list", async () => {
  const env = await booted("/");
  env.dispatch("calendar-date-clicked", { date: "2025-03-05", mini: true });
  const s = env.state();
  assert.equal(s.view, "calendar");
  assert.equal(text(selectHeading(s).title), "Lịch cuộc họp");
  assert.equal(s.focus.target, "calendar-title");
  assert.equal(env.waiting(env.is("GET", "/api/meetings")).length, 1);
  const rev = s.contextRev;
  env.dispatch("calendar-date-clicked", { date: "2025-03-06", mini: true });
  assert.equal(env.state().contextRev, rev, "already open: nothing more happens");
  assert.equal(env.state().calendar.selected, "2025-03-06");
});

// ---- list and search ----

test("a failed list refresh keeps the last known list", async () => {
  const env = await booted("/");
  env.dispatch("calendar-opened");
  await env.fail(env.is("GET", "/api/meetings"));
  assert.equal(env.state().total, 2);
  assert.equal(env.state().recent.length, 1);
  env.dispatch("calendar-opened");
  env.dispatch("home-shown");
  env.dispatch("calendar-opened");
  await env.answer(env.is("GET", "/api/meetings"), 200, { meetings: [], recent: [], total: 0 });
  assert.equal(env.state().total, 0);
  assert.equal(selectSidebar(env.state()).emptyHidden, false);
});

test("a list refresh that overtakes an older one wins", async () => {
  const env = await booted("/");
  env.dispatch("calendar-opened");
  env.dispatch("home-shown");
  env.dispatch("calendar-opened");
  const pending = env.waiting(env.is("GET", "/api/meetings"));
  assert.equal(pending.length, 1, "the newer request replaced the older one");
  await env.answer(env.is("GET", "/api/meetings"), 200, { meetings: [], recent: [], total: 0 });
  assert.equal(env.state().total, 0);
});

test("typing searches after a pause; an older answer never replaces a newer one", async () => {
  const env = await booted("/");
  env.dispatch("search-input", { value: "we" });
  assert.deepEqual(env.live(), [120]);
  assert.equal(env.state().shownQuery, "", "the sidebar waits for the answer");
  env.dispatch("search-input", { value: "weekly" });
  assert.deepEqual(env.live(), [120], "typing again restarts the pause");
  env.fire(120);
  assert.equal(env.waiting(env.is("GET", "/api/meetings?q=weekly")).length, 1);
  // The user keeps typing; the first search is still on its way when a second one starts.
  env.dispatch("search-input", { value: "weekly s" });
  env.fire(120);
  assert.equal(env.waiting(env.is("GET", "/api/meetings?q=weekly")).length, 0, "the older search was replaced");
  await env.answer(env.is("GET", "/api/meetings?q=weekly%20s"), 200, { meetings: [meeting("m1")] });
  const s = env.state();
  assert.equal(s.shownQuery, "weekly s");
  assert.equal(selectSidebar(s).items.length, 1);
  assert.equal(text(selectSidebar(s).heading), "Kết quả");
});

test("a stale search answer is dropped by its id", async () => {
  const env = await booted("/");
  env.dispatch("search-input", { value: "a" });
  env.fire(120);
  const stale = env.waiting(env.is("GET", "/api/meetings?q=a"))[0];
  assert.ok(stale, "the first search went out");
  env.dispatch("search-input", { value: "b" });
  env.fire(120);
  // The runner already drops the replaced request; a late action with the old id must not apply either.
  env.store.dispatch({ type: "history/search-loaded", meta: { id: 1 } /* the replaced search */, response: { body: { meetings: [] } }, at: HOME });
  assert.equal(env.state().results.length, 0);
  assert.equal(env.state().shownQuery, "", "still waiting for b");
  await env.answer(env.is("GET", "/api/meetings?q=b"), 200, { meetings: [meeting("m2")] });
  assert.deepEqual(env.state().results.map((item) => item.id), ["m2"]);
});

test("a failed search shows the error and no results; emptying the box returns to recent at once", async () => {
  const env = await booted("/");
  env.dispatch("search-input", { value: "x" });
  env.fire(120);
  await env.fail(env.is("GET", "/api/meetings?q=x"));
  let view = selectSidebar(env.state());
  assert.equal(view.searchErrorHidden, false);
  assert.equal(view.noResultsHidden, true);
  env.dispatch("search-input", { value: "y" });
  env.dispatch("search-input", { value: "" });
  assert.deepEqual(env.live(), [], "the pending search is cancelled");
  view = selectSidebar(env.state());
  assert.equal(env.state().shownQuery, "");
  assert.equal(view.searchErrorHidden, true);
  assert.equal(text(view.heading), "Gần đây");
});

test("an answer that arrives after the box was emptied is dropped", async () => {
  const env = await booted("/");
  env.dispatch("search-input", { value: "x" });
  env.fire(120);
  env.dispatch("search-input", { value: "" });
  assert.equal(env.waiting(env.is("GET", "/api/meetings?q=x")).length, 0, "the request was cancelled");
  assert.equal(env.state().shownQuery, "");
});

test("a list refresh searches the typed query again and repaints with its answer", async () => {
  const env = await booted("/");
  env.dispatch("search-input", { value: "q" });
  env.dispatch("calendar-opened");
  await env.answer(env.is("GET", "/api/meetings"), 200, LIST);
  assert.equal(env.waiting(env.is("GET", "/api/meetings?q=q")).length, 1, "the list answer starts the search");
  await env.answer(env.is("GET", "/api/meetings?q=q"), 200, { meetings: [] });
  assert.equal(env.state().shownQuery, "q");
  assert.equal(selectSidebar(env.state()).noResultsHidden, false);
  assert.equal(text(selectSidebar(env.state()).noResultsText), "Không tìm thấy cuộc họp nào khớp “q”.");
});

// ---- loading a meeting ----

test("boot at a meeting address loads it, then shows it, tells notes.js and gets ready", async () => {
  const env = await booted("/meetings/m1", "m1");
  const s = env.state();
  assert.equal(s.view, "meeting");
  assert.equal(s.meeting.id, "m1");
  assert.equal(text(selectHeading(s).title), "Cuộc họp");
  assert.equal(selectHeading(s).documentTitle, "Meeting m1 · ASR Studio");
  assert.equal(s.readyRev, 1);
  assert.ok(s.contextRev > 0, "notes.js is told");
  assert.equal(s.paintRev, 1);
  assert.equal(s.installKind, "load");
  assert.equal(s.loadDone, s.loadId);
  assert.equal(s.notes.lockKnown, true);
});

test("opening another meeting shows loading and drops the editor; the old meeting stays until the new one lands", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("editor-opened");
  assert.ok(env.state().editor);
  env.dispatch("meeting-link-clicked", { meetingId: "m2" });
  let s = env.state();
  assert.equal(s.view, "loading");
  assert.equal(s.editor, null);
  assert.equal(s.meeting.id, "m1");
  assert.equal(s.left.kind, "switch");
  assert.deepEqual(env.calls.at(-1), ["address.push", "/meetings/m2", null, null]);
  await env.answer(GET("m2"), 200, meeting("m2"));
  s = env.state();
  assert.equal(s.view, "meeting");
  assert.equal(s.meeting.id, "m2");
});

test("a load that is overtaken by another meeting never paints", async () => {
  const env = await booted("/");
  env.dispatch("meeting-requested", { meetingId: "m1" });
  env.dispatch("meeting-requested", { meetingId: "m2" });
  assert.equal(env.waiting(GET("m1")).length, 0, "the first request was replaced");
  // A late answer for the first load, with its old load id, is dropped by the reducer too.
  env.store.dispatch({ type: "history/meeting-loaded", meta: { loadId: 2, meetingId: "m1" }, response: { status: 200, body: meeting("m1") }, at: HOME });
  assert.equal(env.state().view, "loading");
  assert.equal(env.state().meeting, null);
  await env.answer(GET("m2"), 200, meeting("m2"));
  assert.equal(env.state().meeting.id, "m2");
});

test("leaving for home or the calendar while a meeting loads drops its answer, whether it succeeds or fails", async () => {
  const env = await booted("/");
  // The answers are the real pending requests; the runner's own cancellation would hide a missing guard, so the
  // reducer has to drop them by id.
  const late = async (id, status, body) => {
    const request = env.requests.find((r) => r.method === "GET" && r.url === `/api/meetings/${id}` && !r.done);
    assert.ok(request, "the request was sent");
    request.done = true;
    request.resolve(reply(status, body));
    await tick();
  };
  env.dispatch("meeting-requested", { meetingId: "m1" });
  env.dispatch("home-shown");
  await late("m1", 200, meeting("m1"));
  assert.equal(env.state().view, "home");
  assert.equal(env.state().meeting, null);
  env.dispatch("meeting-requested", { meetingId: "m2" });
  env.dispatch("calendar-opened");
  await late("m2", 200, meeting("m2"));
  assert.equal(env.state().view, "calendar");
  assert.equal(env.state().meeting, null);
  env.dispatch("meeting-requested", { meetingId: "m1" });
  env.dispatch("home-shown");
  await late("m1", 500, { detail: { message: "boom" } });
  assert.equal(env.state().view, "home");
  assert.equal(env.state().loadError, null);
  env.dispatch("meeting-requested", { meetingId: "m1" });
  env.dispatch("calendar-opened");
  await late("m1", 500, { detail: { message: "boom" } });
  assert.equal(env.state().view, "calendar");
});

test("a load failure that arrives after another meeting was requested leaves the new load alone", async () => {
  const env = await booted("/");
  env.dispatch("meeting-requested", { meetingId: "m1" });
  env.dispatch("meeting-requested", { meetingId: "m2" });
  env.store.dispatch({ type: "history/meeting-load-failed", meta: { loadId: env.state().loadId - 1, meetingId: "m1" }, error: { message: "x" }, at: HOME });
  assert.equal(env.state().view, "loading");
  await env.answer(GET("m2"), 200, meeting("m2"));
  assert.equal(env.state().view, "meeting");
});

test("a load that fails shows the error with the server's words; a network failure says so", async () => {
  const env = await booted("/");
  env.dispatch("meeting-requested", { meetingId: "m1" });
  await env.answer(GET("m1"), 404, { detail: { code: "meeting_not_found", message: "Không có cuộc họp này." } });
  let s = env.state();
  assert.equal(s.view, "error");
  assert.equal(text(s.loadError), "Không có cuộc họp này. Hãy kiểm tra phiên truy cập hoặc mở lại cuộc họp từ workspace.");
  assert.equal(s.loadDone, s.loadId);
  assert.equal(s.heading, "loading", "the error view keeps the heading");
  env.dispatch("meeting-requested", { meetingId: "m1" });
  await env.fail(GET("m1"));
  s = env.state();
  assert.equal(text(s.loadError).startsWith("Không kết nối được với máy chủ trên máy này."), true);
});

test("an API error code in the alternate error envelope stays localized", () => {
  const failure = failureOf({
    status: 409,
    body: {
      error: {
        code: "notes_source_locked",
        message: "This transcript is locked while notes are being generated.",
      },
    },
  });
  assert.equal(failure.code, "notes_source_locked");
  assert.match(text(failure.message), /Nguồn đang được dùng để tạo notes/);
  assert.doesNotMatch(text(failure.message), /This transcript is locked/);
});

test("a load whose body is not a meeting is an error", async () => {
  const env = await booted("/");
  env.dispatch("meeting-requested", { meetingId: "m1" });
  await env.answer(GET("m1"), 200, { title: 5 });
  assert.equal(env.state().view, "error");
  assert.match(text(env.state().loadError), /metadata cuộc họp hợp lệ/);
  env.dispatch("meeting-requested", { meetingId: "m1" });
  await env.answer(GET("m1"), 200, meeting("m1", { language_hint: "fr" }));
  assert.match(text(env.state().loadError), /gợi ý ngôn ngữ/);
});

test("reloading the meeting on screen paints what is known at once and never steps back a version", async () => {
  const env = await booted("/meetings/m1", "m1");
  const paint = env.state().paintRev;
  env.dispatch("meeting-requested", { meetingId: "m1" });
  assert.equal(env.state().view, "meeting");
  assert.equal(env.state().paintRev, paint + 1);
  assert.equal(env.state().left.kind, "switch", "same meeting: no reset");
  assert.equal(env.state().left.rev, 1, "only the boot reset");
  await env.answer(GET("m1"), 200, meeting("m1", { metadata_version: 1, title: "Old title" }));
  assert.equal(env.state().meeting.title, "Meeting m1");
  assert.equal(env.state().meeting.metadata_version, 3);
});

test("popstate and hashchange are actions: back loads the meeting or goes home, a hash repaints the tab", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("tab-chosen", { tab: "notes" });
  env.address.pathname = "/";
  env.dispatch("popped");
  assert.equal(env.state().view, "home");
  env.address.pathname = "/meetings/m1";
  env.address.hash = "#transcript";
  env.dispatch("popped");
  assert.equal(env.state().view, "meeting", "the same meeting paints at once");
  assert.equal(env.state().tab, "transcript", "the address names the tab");
  env.address.hash = "#notes";
  env.dispatch("hash-changed");
  assert.equal(env.state().tab, "notes");
  env.dispatch("home-shown");
  env.dispatch("hash-changed");
  assert.equal(env.state().tab, null, "no meeting on screen: the hash is ignored");
});

test("the brand link goes home with one new entry only when the address says otherwise", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("home-clicked");
  assert.deepEqual(env.calls.at(-1), ["address.push", "/", null, null]);
  assert.equal(env.state().view, "home");
  env.calls.length = 0;
  env.address.pathname = "/";
  env.dispatch("home-clicked");
  assert.deepEqual(env.calls, []);
});

// ---- tabs ----

test("with no tab in the address the default waits for notes.js, then follows its answer", async () => {
  const env = await booted("/meetings/m1", "m1");
  assert.equal(env.state().tab, null);
  assert.deepEqual(env.live(), [TAB_PENDING_MS]);
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 2, status: null, known: true });
  assert.equal(env.state().tab, "notes");
  assert.deepEqual(env.live(), [], "the fallback timer is cancelled");
});

test("a meeting with no saved notes defaults to the transcript; a running notes task defaults to notes", async () => {
  let env = await booted("/meetings/m1", "m1");
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 0, status: null, known: true });
  assert.equal(env.state().tab, "transcript");
  env = await booted("/meetings/m1", "m1");
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 0, status: "running", known: true });
  assert.equal(env.state().tab, "notes");
  assert.equal(defaultTab(initialState), null);
});

test("an unknown summary changes nothing; a summary for another meeting is ignored", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 3, status: null, known: false });
  assert.equal(env.state().tab, null);
  assert.equal(env.state().notes.count, 3);
  const rev = env.state().summaryRev;
  env.dispatch("notes-summary-reported", { meetingId: "other", count: 9, status: null, known: true });
  assert.equal(env.state().summaryRev, rev);
  assert.equal(env.state().notes.count, 3);
});

test("when notes.js never answers the page falls back to the transcript after the timeout", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.fire(TAB_PENDING_MS);
  assert.equal(env.state().tab, "transcript");
  assert.deepEqual(env.live(), []);
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 5, status: null, known: true });
  assert.equal(env.state().tab, "transcript", "a late answer does not move a chosen tab");
});

test("a late answer after the user chose a tab keeps the choice", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("tab-chosen", { tab: "transcript" });
  assert.deepEqual(env.live(), []);
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 5, status: null, known: true });
  assert.equal(env.state().tab, "transcript");
});

test("leaving the meeting forgets the tab and the fallback timer", async () => {
  const env = await booted("/meetings/m1", "m1");
  assert.deepEqual(env.live(), [TAB_PENDING_MS]);
  env.dispatch("home-shown");
  assert.deepEqual(env.live(), []);
  assert.equal(env.state().tab, null);
});

test("choosing a tab pushes one entry that remembers the tab being left", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 1, status: null, known: true });
  env.address.search = "?x=1";
  env.dispatch("tab-chosen", { tab: "transcript" });
  assert.deepEqual(env.calls.at(-1), ["address.push", "/meetings/m1?x=1#transcript", "transcript", "notes"]);
  assert.equal(env.state().tab, "transcript");
  env.calls.length = 0;
  env.dispatch("tab-chosen", { tab: "transcript" });
  env.dispatch("tab-chosen", { tab: "nope" });
  assert.deepEqual(env.calls, []);
  env.dispatch("home-shown");
  env.dispatch("tab-chosen", { tab: "notes" });
  assert.deepEqual(env.calls, [], "only on the meeting view");
});

test("opening a saved source shows the transcript with one entry, unless the address already names it", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("tab-chosen", { tab: "notes" });
  env.calls.length = 0;
  const source = { meetingId: "m1", snapshotId: "s 1", segmentId: "seg" };
  env.dispatch("source-tab-shown", source);
  assert.deepEqual(env.calls.at(-1), ["address.push", "/meetings/m1?notes_source=s%201&segment=seg#transcript", "transcript", "notes"]);
  assert.equal(env.state().tab, "transcript");
  env.calls.length = 0;
  env.address.search = "?notes_source=s%201&segment=seg";
  env.address.hash = "#transcript";
  env.dispatch("source-tab-shown", source);
  assert.deepEqual(env.calls, []);
  env.dispatch("source-tab-shown", { ...source, meetingId: "other" });
  assert.deepEqual(env.calls, [], "another meeting: nothing");
});

// ---- notes source lock ----

test("the notes source lock follows the meeting copy and notes.js, and only repaints on a change", async () => {
  const env = await booted("/meetings/m1", "m1");
  const rev = env.state().lockRev;
  env.dispatch("notes-source-lock-reported", { meetingId: "m1", locked: false, known: true });
  assert.equal(env.state().lockRev, rev, "unchanged");
  env.dispatch("notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  assert.equal(env.state().notes.locked, true);
  assert.equal(env.state().lockRev, rev + 1);
  env.dispatch("notes-source-lock-reported", { meetingId: "other", locked: false, known: true });
  assert.equal(env.state().notes.locked, true);
});

test("a meeting without a lock flag leaves the lock unknown until notes.js reports it", async () => {
  const env = setup({ path: "/meetings/m1" });
  env.dispatch("booted", { today: "2025-03-04" });
  await env.answer(env.is("GET", "/api/meetings"), 200, LIST);
  await env.answer(GET("m1"), 200, meeting("m1", { notes_source_locked: undefined }));
  assert.equal(env.state().notes.lockKnown, false);
});

// ---- metadata editor ----

async function editing() {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("editor-opened");
  return env;
}
const PATCH = (id) => (r) => r.method === "PATCH" && r.url === `/api/meetings/${id}`;

test("the editor starts from the meeting and counts a field as edited only when it differs", async () => {
  const env = await editing();
  const editor = selectEditor(env.state());
  assert.equal(editor.open, true);
  assert.equal(editor.title, "Meeting m1");
  assert.equal(editor.date, "2025-03-04");
  assert.equal(text(editor.saveLabel), "Lưu thay đổi");
  assert.equal(env.state().focus.target, "edit-title");
  env.dispatch("editor-submitted", { title: "Meeting m1", date: "2025-03-04", badInput: false });
  assert.equal(env.state().editor, null, "nothing changed: the editor just closes");
  assert.equal(env.waiting(PATCH("m1")).length, 0);
});

test("the editor refuses an empty or too long name, a half-typed date and a locked date", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "   ", date: "2025-03-04", badInput: false });
  assert.match(text(env.state().editor.error), /từ 1 đến 200 ký tự/);
  env.dispatch("editor-submitted", { title: "x".repeat(201), date: "2025-03-04", badInput: false });
  assert.match(text(env.state().editor.error), /từ 1 đến 200 ký tự/);
  env.dispatch("editor-submitted", { title: "New", date: "", badInput: true });
  assert.match(text(env.state().editor.error), /chưa đầy đủ/);
  assert.equal(env.state().editor.focusDate, 1);
  env.dispatch("editor-submitted", { title: "New", date: "0999-04-05", badInput: false });
  assert.match(text(env.state().editor.error), /chưa đầy đủ/);
  assert.equal(validMeetingDate("1000-02-29"), false);
  assert.equal(validMeetingDate("1000-02-28"), true);
  env.dispatch("notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  env.dispatch("editor-submitted", { title: "New", date: "2025-04-05", badInput: false });
  assert.match(text(env.state().editor.error), /đang bị khóa/);
  assert.equal(env.waiting(PATCH("m1")).length, 0);
  assert.equal(selectEditor(env.state()).saveDisabled, true, "the changed date blocks the save while the lock holds");
  env.dispatch("editor-edited", { date: "2025-03-04" });
  assert.equal(selectEditor(env.state()).saveDisabled, false, "the date is back to the original: the name can be saved");
});

test("the lock disables the date and the save only while the date differs from the original", async () => {
  const env = await editing();
  env.dispatch("notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  let editor = selectEditor(env.state());
  assert.equal(editor.dateDisabled, true);
  assert.match(text(editor.status), /Nguồn đang được dùng để tạo notes/);
  assert.equal(editor.clearDisabled, true);
  assert.equal(editor.saveDisabled, false);
  env.dispatch("editor-edited", { date: "" });
  editor = selectEditor(env.state());
  assert.equal(editor.dateDisabled, false, "a changed date stays editable so it can be put back");
  assert.equal(editor.saveDisabled, true);
});

test("saving sends the version and only the changed fields, then installs the answer and closes", async () => {
  const env = await editing();
  env.dispatch("editor-edited", { title: "Renamed" });
  env.dispatch("editor-submitted", { title: "Renamed", date: "2025-03-04", badInput: false });
  assert.equal(env.state().editor.busy, true);
  const sent = env.waiting(PATCH("m1"))[0];
  assert.deepEqual(sent.body, { expected_metadata_version: 3, title: "Renamed" });
  await env.answer(PATCH("m1"), 200, meeting("m1", { title: "Renamed", metadata_version: 4 }));
  const s = env.state();
  assert.equal(s.editor, null);
  assert.equal(s.meeting.title, "Renamed");
  assert.equal(s.installKind, "save");
  assert.equal(text(s.editStatus), "Đã lưu tên và ngày họp.");
  assert.equal(s.focus.target, "meeting-actions");
  assert.equal(env.waiting(env.is("GET", "/api/meetings")).length, 1, "the lists learn of it");
});

test("a second save while one is in flight is ignored", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  env.dispatch("editor-submitted", { title: "B", date: "2025-03-04", badInput: false });
  assert.equal(env.requests.filter(PATCH("m1")).length, 1);
  assert.equal(selectEditor(env.state()).titleDisabled, true);
  assert.equal(selectEditor(env.state()).cancelDisabled, true);
});

test("a save that fails uses a safe localized fallback and frees the editor, keeping the draft", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 422, { detail: "Tên không hợp lệ." });
  let s = env.state();
  assert.equal(text(s.editor.error), "File không đạt yêu cầu. Hãy kiểm tra định dạng, thời lượng và nội dung file.");
  assert.doesNotMatch(text(s.editor.error), /Tên không hợp lệ/);
  assert.equal(s.editor.busy, false);
  assert.equal(s.editor.title, "A");
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  await env.fail(PATCH("m1"));
  s = env.state();
  assert.match(text(s.editor.error), /Không kết nối được/);
  assert.equal(s.editor.busy, false);
});

test("a save answer that arrives after the editor was cancelled is dropped, but the lists refresh", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  env.dispatch("editor-cancelled");
  assert.equal(env.state().editor, null);
  assert.equal(selectEditor(env.state()).cancelDisabled, undefined);
  await env.answer(PATCH("m1"), 200, meeting("m1", { title: "A", metadata_version: 4 }));
  assert.equal(env.state().meeting.title, "Meeting m1", "the closed editor does not install");
  assert.equal(env.state().editor, null);
  assert.equal(env.waiting(env.is("GET", "/api/meetings")).length, 1);
});

test("a save answer for an editor that was reopened is dropped", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  env.dispatch("editor-cancelled");
  env.dispatch("editor-opened");
  await env.answer(PATCH("m1"), 200, meeting("m1", { title: "A", metadata_version: 4 }));
  assert.equal(env.state().meeting.title, "Meeting m1");
  assert.ok(env.state().editor, "the new editor stays");
  assert.equal(env.state().editor.busy, false);
});

test("opening another meeting while a save is in flight orphans it", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  env.dispatch("meeting-link-clicked", { meetingId: "m2" });
  await env.answer(PATCH("m1"), 200, meeting("m1", { title: "A", metadata_version: 4 }));
  assert.equal(env.state().meeting.title, "Meeting m1");
  assert.equal(env.state().view, "loading");
  assert.ok(env.calls.some(([name, meetingId, reason]) => name === "meeting.change" && meetingId === "m1" && reason === "metadata"));
});

test("a calendar detour keeps the editor, and the save result still lands", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  env.dispatch("calendar-opened");
  const paint = env.state().paintRev;
  await env.answer(PATCH("m1"), 200, meeting("m1", { title: "A", metadata_version: 4 }));
  const s = env.state();
  assert.equal(s.meeting.title, "A");
  assert.equal(s.editor, null);
  assert.equal(s.paintRev, paint, "the meeting header is not repainted while the calendar shows");
  assert.equal(s.view, "calendar");
});

test("a version conflict reloads the meeting, keeps edited fields and moves only the baseline", async () => {
  const env = await editing();
  env.dispatch("editor-edited", { title: "Mine" });
  env.dispatch("editor-submitted", { title: "Mine", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version", message: "stale" } });
  assert.equal(env.state().editor.busy, true, "still busy while the latest copy is read");
  await env.answer(GET("m1"), 200, meeting("m1", { title: "Theirs", meeting_date: "2025-05-06", metadata_version: 7 }));
  const s = env.state();
  assert.equal(s.editor.busy, false);
  assert.equal(s.editor.title, "Mine", "the edited field keeps the draft");
  assert.equal(s.editor.date, "2025-05-06", "the untouched field follows the stored value");
  assert.equal(s.editor.version, 7);
  assert.equal(s.editor.original.title, "Meeting m1");
  assert.equal(s.editor.original.meeting_date, "2025-05-06");
  assert.equal(s.meeting.title, "Theirs");
  assert.match(text(s.editor.conflict), /“Theirs” · 06\/05\/2025\. Bản nháp tên của bạn vẫn được giữ/);
  assert.equal(text(s.editor.saveLabel), "Lưu bản nháp lên bản mới nhất");
  assert.equal(s.installKind, "conflict");
  // The next save is checked against the new version and sends the title only.
  env.dispatch("editor-submitted", { title: "Mine", date: "2025-05-06", badInput: false });
  assert.deepEqual(env.waiting(PATCH("m1"))[0].body, { expected_metadata_version: 7, title: "Mine" });
});

test("a conflict with nothing edited says so, and both edited fields join with 'và'", async () => {
  let env = await editing();
  env.dispatch("editor-submitted", { title: "x", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  env.dispatch("editor-edited", { title: "Meeting m1" }); // back to the original: no longer an edit
  await env.answer(GET("m1"), 200, meeting("m1", { title: "T", metadata_version: 5 }));
  assert.match(text(env.state().editor.conflict), /Bạn chưa sửa trường nào khác bản đã lưu/);
  env = await editing();
  env.dispatch("editor-edited", { title: "A", date: "2025-04-04" });
  env.dispatch("editor-submitted", { title: "A", date: "2025-04-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  await env.answer(GET("m1"), 200, meeting("m1", { metadata_version: 5 }));
  assert.match(text(env.state().editor.conflict), /tên và ngày họp/);
});

test("a conflict whose latest copy cannot be read keeps the draft and says so", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "Mine", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  await env.fail(GET("m1"));
  const s = env.state();
  assert.match(text(s.editor.conflict), /chưa tải được bản mới/);
  assert.equal(s.editor.title, "Mine");
  assert.equal(s.editor.busy, false);
  assert.equal(text(s.editor.saveLabel), "Lưu thay đổi");
});

test("a conflict answer for an editor that was closed only refreshes the lists", async () => {
  const env = await editing();
  env.dispatch("editor-submitted", { title: "Mine", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  env.dispatch("editor-cancelled");
  await env.answer(GET("m1"), 200, meeting("m1", { title: "Theirs", metadata_version: 7 }));
  assert.equal(env.state().meeting.title, "Meeting m1");
  assert.equal(env.state().editor, null);
  assert.equal(env.waiting(env.is("GET", "/api/meetings")).length, 1);
});

// ---- deletion ----

async function deleting() {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("deletion-opened");
  return env;
}
const DELETE = (id) => (r) => r.method === "DELETE" && r.url === `/api/meetings/${id}`;

test("the confirmation button enables only for the exact name and shows the cleanup state", async () => {
  const env = await deleting();
  let view = selectDeletion(env.state());
  assert.equal(view.dialog.open, true);
  assert.equal(view.dialog.confirmDisabled, true);
  assert.equal(text(view.dialog.confirmLabel), "Xóa dữ liệu đã lưu");
  assert.equal(view.dialog.name, "Meeting m1");
  env.dispatch("deletion-confirmation-edited", { value: "Meeting m1" });
  view = selectDeletion(env.state());
  assert.equal(view.dialog.confirmDisabled, false);
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  view = selectDeletion(env.state());
  assert.equal(view.dialog.confirmDisabled, true);
  assert.equal(text(view.dialog.confirmLabel), "Đang dọn dữ liệu…");
  assert.equal(view.header.state, "pending");
  assert.equal(view.header.deleteDisabled, true);
});

test("a wrong name sends nothing; a second submit while deleting is ignored", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "nope" });
  assert.equal(env.requests.filter(DELETE("m1")).length, 0);
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  assert.equal(env.requests.filter(DELETE("m1")).length, 1);
  env.dispatch("deletion-opened");
  assert.equal(env.state().deletion.pending, true);
});

test("a deleted meeting leaves the lists and the page goes home", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.answer(DELETE("m1"), 200, { meeting_id: "m1", status: "deleted" });
  const s = env.state();
  assert.equal(s.view, "home");
  assert.equal(s.meeting, null);
  assert.equal(s.total, 1);
  assert.deepEqual(s.meetings.map((item) => item.id), ["m2"]);
  assert.deepEqual(s.recent, []);
  assert.equal(s.deletion.dialog.open, false);
  assert.equal(s.deletion.pending, false);
  assert.equal(s.focus.target, "page-title");
  assert.deepEqual(env.calls.at(-1), ["address.push", "/", null, null]);
  assert.equal(env.waiting(env.is("GET", "/api/meetings")).length, 1);
});

test("a meeting that is already gone counts as deleted", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.answer(DELETE("m1"), 404, { detail: { code: "meeting_not_found" } });
  assert.equal(env.state().view, "home");
  assert.equal(env.state().total, 1);
});

test("an answer that does not confirm the deletion is a failure", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.answer(DELETE("m1"), 200, { meeting_id: "other", status: "deleted" });
  assert.match(text(env.state().deletion.failures.m1.message), /chưa xác nhận việc xóa/);
  assert.equal(env.state().deletion.pending, false);
});

test("a deletion that fails reloads the meeting, then shows the error in the open dialog", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.answer(DELETE("m1"), 409, { detail: { code: "meeting_task_active", message: "busy" } });
  let s = env.state();
  assert.equal(s.deletion.pending, false);
  assert.equal(s.deletion.dialog.error, null, "the error waits for the reload");
  assert.match(text(selectDeletion(s).header.text), /còn tác vụ đang chạy/);
  assert.equal(selectDeletion(s).header.hidden, false);
  assert.equal(selectDeletion(s).header.state, "error");
  env.dispatch("deletion-confirmation-edited", { value: "Meeting m1" });
  s = env.state();
  assert.equal(selectDeletion(s).dialog.confirmDisabled, true, "the button stays off until the reload ends");
  assert.equal(text(selectDeletion(s).dialog.confirmLabel), "Đang dọn dữ liệu…");
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  assert.equal(env.state().deletion.pending, false, "no second DELETE before the reload ends");
  await env.answer(GET("m1"), 200, meeting("m1"));
  s = env.state();
  assert.match(text(s.deletion.dialog.error), /còn tác vụ đang chạy/);
  assert.equal(s.deletion.dialog.confirmation, "");
  assert.equal(s.deletion.dialog.open, true);
  assert.equal(selectDeletion(s).dialog.confirmDisabled, true);
  assert.equal(text(selectDeletion(s).dialog.confirmLabel), "Xóa dữ liệu đã lưu");
});

test("a failed cleanup marks the meeting and offers to continue", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.fail(DELETE("m1"));
  await env.answer(GET("m1"), 200, meeting("m1"));
  let s = env.state();
  assert.match(text(s.deletion.failures.m1.message), /Không kết nối được/);
  assert.equal(s.deletion.failures.m1.cleanupMarked, false);
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  env.dispatch("deletion-confirmation-edited", { value: "Meeting m1" });
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.answer(DELETE("m1"), 500, { detail: { code: "meeting_deletion_cleanup_failed" } });
  await env.answer(GET("m1"), 200, meeting("m1", { deletion_status: "failed" }));
  s = env.state();
  assert.equal(s.deletion.failures.m1.cleanupMarked, true);
  const view = selectDeletion(s);
  assert.equal(text(view.header.deleteLabel), "Thử dọn tiếp");
  assert.equal(text(view.dialog.confirmLabel), "Thử dọn tiếp");
  assert.equal(view.header.state, "failed");
  assert.match(text(view.header.text), /Dọn dẹp chưa hoàn tất\. Hệ thống sẽ tự thử lại khi khởi động; sau khi xử lý sự cố/);
});

test("closing the dialog while the request runs keeps the request; the failure shows in the header only", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  env.dispatch("deletion-dialog-closed");
  assert.equal(env.state().deletion.dialog.open, false);
  assert.equal(env.state().deletion.pending, true);
  await env.answer(DELETE("m1"), 409, { detail: { code: "meeting_task_active" } });
  await env.answer(GET("m1"), 200, meeting("m1"));
  const s = env.state();
  assert.equal(s.deletion.dialog.error, null);
  assert.equal(selectDeletion(s).header.hidden, false);
});

test("a deletion failure for a meeting that is no longer on screen only records the failure", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  env.address.pathname = "/";
  env.dispatch("popped");
  await env.fail(DELETE("m1"));
  assert.equal(env.waiting(GET("m1")).length, 0, "no reload");
  assert.ok(env.state().deletion.failures.m1);
  assert.equal(env.state().view, "home");
});

test("a dialog error waits for the reload, and leaving first drops it", async () => {
  const env = await deleting();
  env.dispatch("deletion-submitted", { confirmation: "Meeting m1" });
  await env.fail(DELETE("m1"));
  assert.ok(env.state().deletion.after, "waiting for the reload");
  env.address.pathname = "/";
  env.dispatch("home-shown");
  assert.equal(env.state().deletion.after, null, "settled when the load was replaced");
  assert.equal(env.state().deletion.dialog.error, null, "not on screen any more");
});

test("closing the dialog twice or with nothing to reset changes nothing", async () => {
  const env = await booted("/meetings/m1", "m1");
  const before = env.state();
  env.dispatch("deletion-dialog-closed");
  assert.equal(env.state(), before);
  env.dispatch("deletion-confirmation-edited", { value: "x" });
  assert.equal(env.state(), before, "typing without the dialog open is ignored");
});

// ---- meeting updates from the task and transcript code ----

test("a newer copy from the server merges into the current meeting; another meeting's copy is ignored", async () => {
  const env = await booted("/meetings/m1", "m1");
  const rev = env.state().installRev;
  env.dispatch("meeting-fetched", { meeting: meeting("m2") });
  assert.equal(env.state().installRev, rev);
  env.dispatch("meeting-fetched", { meeting: meeting("m1", { transcription: { generation: { ...GENERATION, source_version: 1 }, task: null } }) });
  assert.equal(env.state().installRev, rev + 1);
  assert.equal(env.state().installKind, "refresh");
  assert.equal(env.state().meeting.transcription.generation.source_version, 2, "an older generation does not replace the page's");
});

const SEGMENT = { id: "s1", text: "old", speaker_id: "A", assignment_version: 1 };
const SPEAKER = { speaker_id: "A", display_name: "Speaker 1" };
const WITH_ITEMS = { ...GENERATION, source_version: 2, segments: [SEGMENT], speakers: [SPEAKER] };
const withItems = (extra = {}) => meeting("m1", { transcription: { generation: WITH_ITEMS, generation_history: [], task: null }, ...extra });
const answer = (field, extra = {}) => ({
  type: "transcript/mutation-succeeded",
  meta: { kind: field, meetingId: "m1", generationId: "g1", field, itemId: field === "speakerName" ? "A" : "s1", ...extra.meta },
  response: { status: 200, body: extra.body },
});

test("a saved transcript field is applied to the meeting as new objects, and notes.js is told", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("meeting-fetched", { meeting: withItems() });
  const before = env.state();
  const next = reduce(before, answer("segmentText", { body: { text: "new", source_version: 3 } })).state;
  const generation = next.meeting.transcription.generation;
  assert.equal(generation.segments[0].text, "new");
  assert.equal(generation.source_version, 3);
  assert.equal(before.meeting.transcription.generation.segments[0].text, "old", "the old meeting is not edited in place");
  assert.notEqual(next.meeting, before.meeting);
  assert.equal(next.contextRev, before.contextRev + 1);
  const named = reduce(next, answer("speakerName", { body: { display_name: "Ann", source_version: 4 } })).state;
  assert.equal(named.meeting.transcription.generation.speakers[0].display_name, "Ann");
  const assigned = reduce(named, answer("speakerAssignment", { body: { speaker_id: "B", assignment_version: 2, source_version: 5 } })).state;
  assert.deepEqual([assigned.meeting.transcription.generation.segments[0].speaker_id, assigned.meeting.transcription.generation.segments[0].assignment_version], ["B", 2]);
});

test("a saved field is not applied to another meeting, a missing item or an older source version", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("meeting-fetched", { meeting: withItems() });
  const state = env.state();
  const other = reduce(state, answer("segmentText", { meta: { meetingId: "m2" }, body: { text: "new", source_version: 3 } })).state;
  assert.equal(other.meeting, state.meeting);
  const missing = reduce(state, answer("segmentText", { meta: { itemId: "nope" }, body: { text: "new", source_version: 3 } })).state;
  assert.equal(missing.meeting, state.meeting);
  const older = reduce(state, answer("segmentText", { body: { text: "new", source_version: 1 } })).state;
  assert.equal(older.meeting, state.meeting);
  assert.equal(older.contextRev, state.contextRev + 1, "notes.js is told even when nothing changed, as before");
});

test("a speaker the server added goes into its generation only when the source version moves on", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("meeting-fetched", { meeting: withItems() });
  const state = env.state();
  const body = { speaker_id: "B", display_name: "Bob", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 3, generation_id: "g1" };
  const added = reduce(state, { type: "transcript/mutation-succeeded", meta: { kind: "speakerAdd", meetingId: "m1", generationId: "g1" }, response: { body } }).state;
  assert.deepEqual(added.meeting.transcription.generation.speakers.map((item) => item.speaker_id), ["A", "B"]);
  assert.equal(added.meeting.transcription.generation.source_version, 3);
  assert.equal(added.contextRev, state.contextRev, "an added speaker does not reach notes.js, as before");
  const stale = reduce(state, { type: "transcript/mutation-succeeded", meta: { kind: "speakerAdd", meetingId: "m1", generationId: "g1" }, response: { body: { ...body, source_version: 2 } } }).state;
  assert.equal(stale.meeting, state.meeting);
  // A past generation exists in the meeting: a reapply for it still does not count, an add that names it does.
  const past = { ...WITH_ITEMS, id: "gh", source_version: 1, segments: [], speakers: [SPEAKER] };
  env.dispatch("meeting-fetched", { meeting: withItems({ transcription: { generation: WITH_ITEMS, generation_history: [past], task: null } }) });
  const withPast = env.state();
  const reapplied = reduce(withPast, { type: "transcript/mutation-succeeded", meta: { kind: "speakerReapply", meetingId: "m1", generationId: "gh" }, response: { body: { ...body, generation_id: "g1" } } }).state;
  assert.equal(reapplied.meeting, withPast.meeting, "a reapply counts only for the current generation");
  const addedPast = reduce(withPast, { type: "transcript/mutation-succeeded", meta: { kind: "speakerAdd", meetingId: "m1", generationId: "g1" }, response: { body: { ...body, generation_id: "gh", source_version: 2 } } }).state;
  assert.deepEqual(addedPast.meeting.transcription.generation_history[0].speakers.map((item) => item.speaker_id), ["A", "B"]);
});

test("a 2xx body that is no object changes nothing and does not throw", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("meeting-fetched", { meeting: withItems() });
  const state = env.state();
  for (const body of [null, "<html>login</html>", [], 7, undefined]) {
    for (const field of ["segmentText", "speakerName", "speakerAssignment"]) assert.equal(reduce(state, answer(field, { body })).state, state);
    const added = { type: "transcript/mutation-succeeded", meta: { kind: "speakerAdd", meetingId: "m1", generationId: "g1" }, response: { body } };
    assert.equal(reduce(state, added).state, state);
  }
});

test("a saved field keeps the notes source lock the page was told of, not the one the load carried", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("meeting-fetched", { meeting: withItems({ notes_source_locked: false }) });
  env.dispatch("notes-source-lock-reported", { meetingId: "m1", locked: true, known: true });
  const state = env.state();
  assert.equal(state.meeting.notes_source_locked, false);
  const saved = reduce(state, answer("segmentText", { body: { text: "new", source_version: 3 } })).state;
  assert.equal(saved.meeting.notes_source_locked, true);
  const body = { speaker_id: "B", display_name: "Bob", first_seen_seconds: 0, last_seen_seconds: 1, source_version: 3, generation_id: "g1" };
  const added = reduce(state, { type: "transcript/mutation-succeeded", meta: { kind: "speakerAdd", meetingId: "m1", generationId: "g1" }, response: { body } }).state;
  assert.equal(added.meeting.notes_source_locked, true);
});

test("the latest copy a reload read replaces the meeting unless another copy was installed meanwhile", async () => {
  const env = await booted("/meetings/m1", "m1");
  const state = env.state();
  const loaded = (meta, body) => reduce(state, { type: "transcript/reload-loaded", meta, response: { body } }).state;
  const newer = withItems();
  const ok = loaded({ meetingId: "m1", installRev: state.installRev }, { meeting: newer });
  assert.equal(ok.installRev, state.installRev + 1);
  assert.equal(ok.installKind, "refresh");
  assert.equal(ok.shownQuery, state.query);
  assert.equal(loaded({ meetingId: "m1", installRev: state.installRev - 1 }, newer).installRev, state.installRev, "installed copy changed meanwhile");
  assert.equal(loaded({ meetingId: "m1", installRev: state.installRev }, { ...newer, transcription: null }).installRev, state.installRev);
  assert.equal(loaded({ meetingId: "m1", installRev: state.installRev }, { ...newer, id: "m2" }).installRev, state.installRev);
});

// ---- selectors ----

test("the sidebar marks the open meeting and explains an empty library", async () => {
  const env = await booted("/meetings/m1", "m1");
  assert.equal(selectSidebar(env.state()).openId, "m1");
  env.dispatch("home-shown");
  assert.equal(selectSidebar(env.state()).openId, null);
  const none = { ...initialState, total: 0 };
  assert.equal(selectSidebar(none).emptyHidden, false);
  assert.equal(text(selectSidebar(none).emptyText), "Cuộc họp đã lưu sẽ xuất hiện tại đây.");
  const undated = { ...initialState, total: 3, recent: [] };
  assert.equal(text(selectSidebar(undated).emptyText), "Chưa có cuộc họp nào có ngày. Mở nhóm Chưa đặt ngày trong lịch.");
  assert.equal(selectSidebar(undated).workspaceEmptyHidden, true);
});

// ---- the reducer's own guards, driven without the runner (which would also drop most stale answers) ----

const step = (state, action) => reduce(state, { at: HOME, ...action });
const effectOf = (result, key) => result.effects.find((effect) => effect.key === key);
const bootedState = (path = "/") => step(initialState, { type: "history/booted", today: "2025-03-04", at: at(path) }).state;
const withMeeting = (id = "a") => {
  const s = bootedState(`/meetings/${id}`);
  return step(s, { type: "history/meeting-loaded", meta: { loadId: s.loadId, meetingId: id }, response: { status: 200, body: meeting(id) }, at: at(`/meetings/${id}`) }).state;
};
const asSaved = (state, title) => {
  const result = step(state, { type: "history/editor-submitted", title, date: "2025-03-04", badInput: false });
  return { state: result.state, meta: effectOf(result, "history/save").meta };
};

test("a load failure is dropped by its id after leaving for home or after switching meeting", () => {
  let result = step(bootedState(), { type: "history/meeting-requested", meetingId: "a" });
  const first = effectOf(result, "history/meeting").meta;
  let s = step(result.state, { type: "history/home-shown" }).state;
  s = step(s, { type: "history/meeting-load-failed", meta: first, error: { message: "x" } }).state;
  assert.equal(s.view, "home");
  result = step(bootedState(), { type: "history/meeting-requested", meetingId: "a" });
  const old = effectOf(result, "history/meeting").meta;
  result = step(result.state, { type: "history/meeting-requested", meetingId: "b" });
  s = step(result.state, { type: "history/meeting-load-failed", meta: old, error: { message: "x" } }).state;
  assert.equal(s.view, "loading");
});

test("a search answer or failure is dropped by its id when a newer search exists", () => {
  let s = step(bootedState(), { type: "history/search-input", value: "a" }).state;
  const r1 = step(s, { type: "history/search-due" });
  const old = effectOf(r1, "history/search").meta;
  const r2 = step(r1.state, { type: "history/search-due" });
  const fresh = effectOf(r2, "history/search").meta;
  s = step(r2.state, { type: "history/search-loaded", meta: fresh, response: { status: 200, body: { meetings: [meeting("new")] } } }).state;
  assert.equal(s.results[0].id, "new");
  s = step(s, { type: "history/search-loaded", meta: old, response: { status: 200, body: { meetings: [meeting("old")] } } }).state;
  assert.equal(s.results[0].id, "new");
  s = step(s, { type: "history/search-failed", meta: old, error: { message: "x" } }).state;
  assert.equal(s.searchFailed, false);
});

test("a search that failed is cleared by the next answer", () => {
  let s = step(bootedState(), { type: "history/search-input", value: "a" }).state;
  let r = step(s, { type: "history/search-due" });
  s = step(r.state, { type: "history/search-failed", meta: effectOf(r, "history/search").meta, error: {} }).state;
  assert.equal(s.searchFailed, true);
  r = step(s, { type: "history/search-due" });
  s = step(r.state, { type: "history/search-loaded", meta: effectOf(r, "history/search").meta, response: { status: 200, body: { meetings: [] } } }).state;
  assert.equal(s.searchFailed, false);
});

test("a list answer is dropped by its id, and a 200 without a list keeps the sidebar", () => {
  const s0 = bootedState();
  const first = s0.listId;
  const r1 = step(s0, { type: "history/calendar-opened" });
  const list = (id) => ({ meetings: [meeting(id)], recent: [], total: 1 });
  let s = step(r1.state, { type: "history/list-loaded", meta: { id: r1.state.listId }, response: { status: 200, body: list("new") } }).state;
  s = step(s, { type: "history/list-loaded", meta: { id: first }, response: { status: 200, body: list("old") } }).state;
  assert.equal(s.meetings[0].id, "new");
  s = step(s, { type: "history/list-loaded", meta: { id: s.listId }, response: { status: 200, body: { nope: 1 } } }).state;
  assert.equal(s.meetings.length, 1);
  assert.equal(s.total, 1);
});

test("an answer for an old save cannot free the busy flag of a reopened editor", () => {
  let s = step(withMeeting(), { type: "history/editor-opened" }).state;
  const old = asSaved(s, "New");
  s = step(old.state, { type: "history/editor-cancelled" }).state;
  s = step(s, { type: "history/editor-opened" }).state;
  s = asSaved(s, "Newer").state;
  assert.equal(s.editor.busy, true);
  s = step(s, { type: "history/save-failed", meta: old.meta, error: { message: "x" } }).state;
  assert.equal(s.editor.busy, true);
});

test("a conflict reload answered after a reopen and a second save keeps the editor busy", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("editor-opened");
  env.dispatch("editor-submitted", { title: "A", date: "2025-03-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  env.dispatch("editor-cancelled");
  env.dispatch("editor-opened");
  env.dispatch("editor-submitted", { title: "B", date: "2025-03-04", badInput: false });
  const conflictReload = env.requests.find((r) => r.method === "GET" && r.url === "/api/meetings/m1" && !r.done);
  assert.ok(conflictReload);
  conflictReload.done = true;
  conflictReload.resolve(reply(200, meeting("m1", { metadata_version: 5 })));
  await tick();
  assert.equal(env.state().editor.busy, true, "the second save is still running");
});

test("a 409 that is not a stale version is a plain failure, not a conflict", () => {
  let s = step(withMeeting(), { type: "history/editor-opened" }).state;
  const saved = asSaved(s, "N");
  const result = step(saved.state, { type: "history/save-failed", meta: saved.meta, error: { status: 409, body: { detail: { code: "other", message: "no" } } } });
  assert.equal(effectOf(result, "history/conflict"), undefined);
  assert.equal(result.state.editor.busy, false);
});

test("entering a year keeps the selected day; 1000 is the first year, padded text is not a year", () => {
  let s = bootedState();
  const selected = s.calendar.selected;
  s = step(s, { type: "history/calendar-year-entered", text: "2020" }).state;
  assert.equal(s.calendar.year, 2020);
  assert.equal(s.calendar.selected, selected);
  s = step(s, { type: "history/calendar-year-entered", text: "1000" }).state;
  assert.equal(s.calendar.year, 1000);
  s = step(s, { type: "history/calendar-year-entered", text: " 2024" }).state;
  assert.equal(s.calendar.year, 1000);
});

test("the fallback timeout after the user chose a tab keeps that tab", () => {
  let s = step(withMeeting(), { type: "history/tab-chosen", tab: "notes", at: at("/meetings/a") }).state;
  assert.equal(s.tab, "notes");
  s = step(s, { type: "history/tab-timed-out" }).state;
  assert.equal(s.tab, "notes");
});

test("a deletion that succeeds for a meeting that is not on screen leaves the view alone", () => {
  const s = step(withMeeting("a"), {
    type: "history/deletion-succeeded",
    meta: { meetingId: "z" },
    response: { status: 200, body: { meeting_id: "z", status: "deleted" } },
    at: at("/meetings/a"),
  }).state;
  assert.equal(s.view, "meeting");
  assert.equal(s.meeting.id, "a");
});

test("the notes summary of one meeting does not choose the tab of the next", async () => {
  const env = await booted("/meetings/m1", "m1");
  env.dispatch("notes-summary-reported", { meetingId: "m1", count: 4, status: null, known: true });
  assert.equal(env.state().tab, "notes");
  env.dispatch("meeting-link-clicked", { meetingId: "m2" });
  await env.answer(GET("m2"), 200, meeting("m2"));
  assert.equal(env.state().notes.known, false);
  assert.equal(env.state().notes.count, 0);
  assert.equal(env.state().tab, null, "the default waits for notes.js to speak about m2");
});

test("a conflict where only the date was edited takes the title from the server", async () => {
  const env = await editing();
  env.dispatch("editor-edited", { date: "2025-04-04" });
  env.dispatch("editor-submitted", { title: "Meeting m1", date: "2025-04-04", badInput: false });
  await env.answer(PATCH("m1"), 409, { detail: { code: "stale_version" } });
  await env.answer(GET("m1"), 200, meeting("m1", { title: "Theirs", meeting_date: "2025-05-06", metadata_version: 7 }));
  const s = env.state();
  assert.equal(s.editor.date, "2025-04-04");
  assert.equal(s.editor.title, "Theirs");
  assert.equal(s.editor.original.title, "Theirs");
});

test("Settings changing rereads the open meeting, and a failed read leaves it on screen", () => {
  let result = step(bootedState(), { type: "history/meeting-requested", meetingId: "m1" });
  const loaded = effectOf(result, "history/meeting").meta;
  let s = step(result.state, { type: "history/meeting-loaded", meta: loaded, response: { status: 200, body: meeting("m1") } }).state;
  assert.equal(s.view, "meeting");

  result = step(s, { type: "history/asr-settings-changed" });
  const read = effectOf(result, "history/asr-settings-read");
  assert.equal(read.url, "/api/meetings/m1");
  assert.equal(result.state.loadId, s.loadId); // not a page load: it cannot replace the meeting with an error

  const failed = step(result.state, { type: "history/asr-settings-read-failed", meta: read.meta, error: { message: "x" } }).state;
  assert.equal(failed.view, "meeting");
  assert.equal(failed.meeting.id, "m1");

  const fresh = meeting("m1", { transcription: { ...meeting("m1").transcription, asr_models: [{ id: "auto", chosen: true }] } });
  s = step(result.state, { type: "history/asr-settings-read", meta: read.meta, response: { status: 200, body: fresh } }).state;
  assert.deepEqual(s.meeting.transcription.asr_models, [{ id: "auto", chosen: true }]);

  assert.deepEqual(step(bootedState(), { type: "history/asr-settings-changed" }).effects, []); // no meeting open
});
