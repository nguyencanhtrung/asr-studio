// Pure core of the meeting navigation and library: state, reducer, selectors and the date helpers the old
// history.js held. No DOM, fetch, timer, storage, clock, randomness or page address: node --test loads this
// file directly. Contract: docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design-v1.md §11.8.
//
// Scope (phase 4a of #118): the meeting list and search, the two calendars, the views (home, calendar,
// loading, error, meeting), loading one meeting, the Notes | Transcript tabs, the notes summary and source
// lock reported by notes.js, the metadata editor and meeting deletion. The transcription task (4b) and the
// transcript (4c) have their own slices; the answers they share with this one (`task/poll-loaded`,
// `transcript/mutation-succeeded`, ...) are applied to the meeting here, as new objects, never in place.
//
// User-visible text is data: t(text, params) here only keeps the Vietnamese source string, and the view
// translates it with the real t(). A param is a primitive, a t() result, or an array of those; an array is
// concatenated. A bare string in state is the server's own text.
//
// The page address and the clock enter through actions: every action the view dispatches carries `at`
// ({pathname, search, hash, tab}: the address and the tab its history entry remembers), `booted` carries `today`
// and the calendar actions carry the day they mean.

import { responseCode, responseMessage } from "./shell-core.js";

const t = (text, params) => (params ? { text, params } : { text });

export const MIN_YEAR = 1000;
export const MAX_YEAR = 9999;
export const MEETING_TABS = ["notes", "transcript"];
export const TAB_PENDING_MS = 4000; // if notes.js never answers, the page falls back to the transcript
const SEARCH_DEBOUNCE_MS = 120;
export const ACTIVE_TASK_STATES = ["accepted", "running", "stopping"];
export const LANGUAGES = { en: "English", vi: t("Tiếng Việt") };

const KEY = {
  list: "history/list",
  search: "history/search",
  searchTimer: "history/search-timer",
  tabTimer: "history/tab-timer",
  meeting: "history/meeting",
  save: "history/save",
  conflict: "history/conflict",
  remove: "history/delete",
  address: "history/address",
  meetingChange: "history/meeting-change",
};

// ---- date-only helpers: pure year/month/day arithmetic, never local time ----

const pad = (value, width = 2) => String(value).padStart(width, "0");
export const dateKey = (year, month, day) => `${pad(year, 4)}-${pad(month + 1)}-${pad(day)}`;

// Days since 1970-01-01 for a proleptic Gregorian date (month 1-12, day may overflow into the next months).
function daysFromCivil(year, month, day) {
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5);
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468 + day - 1;
}

function civilFromDays(days) {
  const z = days + 719468;
  const era = Math.floor(z / 146097);
  const doe = z - era * 146097;
  const yoe = Math.floor((doe - Math.floor(doe / 1460) + Math.floor(doe / 36524) - Math.floor(doe / 146096)) / 365);
  const doy = doe - (365 * yoe + Math.floor(yoe / 4) - Math.floor(yoe / 100));
  const mp = Math.floor((5 * doy + 2) / 153);
  const day = doy - Math.floor((153 * mp + 2) / 5) + 1;
  const month = mp < 10 ? mp + 3 : mp - 9;
  return { year: yoe + era * 400 + (month <= 2 ? 1 : 0), month: month - 1, day };
}

const keyOfDays = (days) => {
  const { year, month, day } = civilFromDays(days);
  return dateKey(year, month, day);
};

export function parseKey(key) {
  const [year, month, day] = key.split("-").map(Number);
  return { year, month: month - 1, day };
}

export function addDays(key, count) {
  const { year, month, day } = parseKey(key);
  return keyOfDays(daysFromCivil(year, month + 1, day) + count);
}

export function displayDate(key) {
  const { year, month, day } = parseKey(key);
  return `${pad(day)}/${pad(month + 1)}/${pad(year, 4)}`;
}

// Six Monday-first weeks covering the month, including out-of-month days.
export function monthGrid(year, month) {
  const first = daysFromCivil(year, month + 1, 1);
  const lead = (((first + 4) % 7) + 7 + 6) % 7; // 1970-01-01 was a Thursday
  return Array.from({ length: 42 }, (_, index) => keyOfDays(first - lead + index));
}

// Recording length only ("1 giờ 24 phút", "12 phút", "45 giây"); never a clock time.
export function formatRecordingDuration(seconds) {
  const whole = Math.round(seconds); // round first so 59.5 s reads "1 phút", never "60 giây"
  if (whole < 60) return t("{n} giây", { n: Math.max(1, whole) });
  const minutes = Math.round(whole / 60);
  if (minutes < 60) return t("{n} phút", { n: minutes });
  return t("{h} giờ {m} phút", { h: Math.floor(minutes / 60), m: pad(minutes % 60) });
}

// The year box commits as soon as it holds a complete 4-digit year in range.
export function validYear(text) {
  const year = /^\d{4}$/.test(text) ? Number(text) : NaN;
  return year >= MIN_YEAR && year <= MAX_YEAR ? year : null;
}

export function validMeetingDate(text) {
  if (text === "") return true;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return false;
  const year = validYear(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year === null || month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= days[month - 1];
}

export function meetingIdFromPathname(pathname) {
  const match = /^\/meetings\/([^/]+)\/?$/.exec(pathname || "");
  if (!match) return null;
  try {
    const id = decodeURIComponent(match[1]);
    return id && !id.includes("/") ? id : null;
  } catch {
    return null;
  }
}

// Every generation of a transcription, current one included, oldest first.
export function generationsFor(transcription) {
  const current = transcription && transcription.generation;
  const past = transcription && Array.isArray(transcription.generation_history) ? transcription.generation_history : [];
  const byId = new Map();
  for (const generation of [...past, current].filter(Boolean)) byId.set(generation.id, generation);
  return [...byId.values()].sort((left, right) => left.generation_number - right.generation_number);
}

// A copy of `meeting` that keeps the generations (and their edits) the page already holds when the server's
// copy is older. The server's object is not touched.
export function mergeTranscription(previous, meeting) {
  const known = previous && meeting && previous.id === meeting.id ? previous.transcription : null;
  const next = meeting && meeting.transcription;
  if (!known || !next) return meeting;
  const merged = new Map(generationsFor(next).map((item) => [item.id, item]));
  let current = next.generation || null;
  const knownCurrent = known.generation || null;
  for (const item of generationsFor(known)) {
    const incoming = merged.get(item.id);
    const retained = !incoming || (Number(incoming.source_version) || 0) < (Number(item.source_version) || 0) ? item : incoming;
    merged.set(item.id, retained);
    if (knownCurrent && item.id === knownCurrent.id && (!current || item.generation_number > current.generation_number)) {
      current = retained;
    } else if (current && current.id === item.id && retained !== current) {
      current = retained;
    }
  }
  const generationHistory = [...merged.values()]
    .filter((item) => !current || item.id !== current.id)
    .sort((left, right) => left.generation_number - right.generation_number);
  return { ...meeting, transcription: { ...next, generation: current, generation_history: generationHistory } };
}

// ---- transcript answers (phase 4c) ----

const ITEMS = { segmentText: "segments", speakerAssignment: "segments", speakerName: "speakers" };
const itemKey = (field) => (ITEMS[field] === "segments" ? "id" : "speaker_id");

// The meeting with one generation swapped for a changed copy.
function withGeneration(meeting, generation) {
  const transcription = meeting.transcription;
  if (transcription.generation && transcription.generation.id === generation.id) {
    return { ...meeting, transcription: { ...transcription, generation } };
  }
  const past = Array.isArray(transcription.generation_history) ? transcription.generation_history : [];
  return { ...meeting, transcription: { ...transcription, generation_history: past.map((item) => (item.id === generation.id ? generation : item)) } };
}

export const isRecord = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

// What a transcript write answered, from the request's `meta` and the response body. `value` is the server's
// copy of the field; a speaker answer carries the speaker it created.
export function answerOf(meta, body) {
  const base = { meetingId: meta.meetingId, generationId: meta.generationId, field: meta.field, itemId: meta.itemId, sourceVersion: body.source_version };
  if (meta.kind === "speakerAdd" || meta.kind === "speakerReapply") {
    const speaker = { speaker_id: body.speaker_id, display_name: body.display_name, first_seen_seconds: body.first_seen_seconds, last_seen_seconds: body.last_seen_seconds };
    // Adding names the generation the server answers for; a reapply only counts for the one it asked.
    return { ...base, speaker, generationId: meta.kind === "speakerAdd" ? body.generation_id || meta.generationId : meta.generationId, currentOnly: meta.kind === "speakerReapply" };
  }
  if (meta.field === "segmentText") return { ...base, value: body.text };
  if (meta.field === "speakerAssignment") return { ...base, value: { speaker_id: body.speaker_id, assignment_version: body.assignment_version } };
  return { ...base, value: body.display_name };
}

// A field write the server accepted, applied to the generation the page holds. Nothing changes when the meeting
// is another one, the item is gone or the page already holds a newer source version. `transcription` is the
// meeting's transcription after the write (null for another meeting); `bumped` says the version moved on.
export function applyFieldAnswer(meeting, answer) {
  const transcription = meeting && meeting.id === answer.meetingId ? meeting.transcription : null;
  const generation = generationsFor(transcription).find((item) => item.id === answer.generationId);
  const none = { meeting, transcription, generation, applied: false, changed: false, bumped: false };
  if (!generation) return none;
  const key = ITEMS[answer.field];
  const list = Array.isArray(generation[key]) ? generation[key] : [];
  const item = list.find((entry) => entry[itemKey(answer.field)] === answer.itemId);
  const currentVersion = Number(generation.source_version) || 0;
  const resultVersion = Number(answer.sourceVersion) || 0;
  if (!item || resultVersion < currentVersion) return none;
  const { value } = answer;
  let changed;
  let next;
  if (answer.field === "segmentText") {
    changed = item.text !== value;
    next = { ...item, text: value };
  } else if (answer.field === "speakerAssignment") {
    changed = item.speaker_id !== value.speaker_id || item.assignment_version !== value.assignment_version;
    next = { ...item, speaker_id: value.speaker_id, assignment_version: value.assignment_version };
  } else {
    changed = item.display_name !== value;
    next = { ...item, display_name: value };
  }
  const bumped = resultVersion > currentVersion;
  const updated = { ...generation, [key]: list.map((entry) => (entry === item ? next : entry)), ...(bumped ? { source_version: resultVersion } : {}) };
  const updatedMeeting = withGeneration(meeting, updated);
  return { meeting: updatedMeeting, transcription: updatedMeeting.transcription, generation: updated, applied: true, changed, bumped };
}

// A speaker the server added, put into the generation it names when that moves the source version on.
export function applySpeakerAdded(meeting, answer) {
  const transcription = meeting && meeting.id === answer.meetingId ? meeting.transcription : null;
  const pool = answer.currentOnly ? generationsFor(transcription).filter((item) => transcription.generation && item.id === transcription.generation.id) : generationsFor(transcription);
  const generation = pool.find((item) => item.id === answer.generationId);
  const resultVersion = Number(answer.sourceVersion) || 0;
  if (!generation || resultVersion <= (Number(generation.source_version) || 0)) return { meeting, transcription, generation, applied: false };
  const speakers = Array.isArray(generation.speakers) ? generation.speakers : [];
  const known = speakers.some((item) => item.speaker_id === answer.speaker.speaker_id);
  const merged = known ? speakers.map((item) => (item.speaker_id === answer.speaker.speaker_id ? { ...item, ...answer.speaker } : item)) : [...speakers, answer.speaker];
  const updated = { ...generation, speakers: merged, source_version: resultVersion };
  const updatedMeeting = withGeneration(meeting, updated);
  return { meeting: updatedMeeting, transcription: updatedMeeting.transcription, generation: updated, applied: true };
}

// ---- state ----

const NO_ADDRESS = { pathname: "/", search: "", hash: "", tab: null };
const NO_DELETION = { pending: false, meetingId: null, failures: {}, dialog: { open: false, confirmation: "", error: null }, after: null };

export const initialState = {
  today: null,
  // list and search; `shownQuery` is the query the sidebar was last painted for (typing alone does not repaint it)
  meetings: [],
  recent: [],
  total: 0,
  listId: 0,
  query: "",
  shownQuery: "",
  results: [],
  searchFailed: false,
  searchId: 0,
  calendar: null, // {year, month, selected: a day key or "undated"}
  // views
  view: null, // home | loading | error | meeting | calendar
  heading: null, // home | calendar | loading | meeting: the error view keeps the heading it found
  loadId: 0, // bumps on every load/leave so a stale load never repaints
  loadDone: 0, // the loadId of the last load that ended, loaded or failed
  loadError: null,
  meeting: null,
  // meeting tabs
  tab: null, // "notes" | "transcript"; null while the default waits for notes.js
  tabWaiting: false, // the fallback timer is running
  notes: { count: 0, known: false, status: null, locked: false, lockKnown: false },
  // metadata editor
  editor: null,
  editorSessions: 0,
  editSeq: 0, // identifies one editor session
  saveId: 0, // identifies the latest save; closing the editor also bumps it
  editStatus: "",
  deletion: NO_DELETION,
  // facts the view paints from: it runs the matching steps when one changes
  installRev: 0, // a meeting from the server became the current one
  installKind: null, // load | save | conflict | refresh
  paintRev: 0, // the meeting header and transcript are due a repaint
  lockRev: 0, // the notes source lock changed
  summaryRev: 0, // the notes summary changed
  contextRev: 0, // the page tells notes.js about the meeting
  readyRev: 0, // a meeting finished loading
  left: { rev: 0, kind: null }, // switch | home: the view resets the task, transcript and playback slices
  focus: { rev: 0, target: null }, // an element the view moves focus to after painting
};

const out = (state, effects = []) => ({ state, effects });

function then(result, step) {
  const next = step(result.state);
  return { state: next.state, effects: [...result.effects, ...(next.effects ?? [])] };
}

const bump = (state, ...names) => ({ ...state, ...Object.fromEntries(names.map((name) => [name, state[name] + 1])) });
const focusOn = (state, target) => ({ ...state, focus: { rev: state.focus.rev + 1, target } });
const addressOf = (action) => action.at ?? NO_ADDRESS;
const meetingPath = (id) => `/meetings/${encodeURIComponent(id)}`;

// A transport or HTTP failure as the reducer reasons about it.
const NETWORK = t("Không kết nối được với máy chủ trên máy này.");
export function failureOf(error, fallback = NETWORK) {
  if (error && error.status !== undefined) {
    return { api: true, status: error.status, message: responseMessage(error.status, error.body), code: responseCode(error.status, error.body) };
  }
  return { api: false, message: error && error.message ? NETWORK : fallback, code: "" };
}

function request(key, url, method, body, ok, error, meta) {
  const init = {
    method,
    headers: method === "GET" ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  return { type: "fetch", key, url, init, ok: `history/${ok}`, error: `history/${error}`, meta };
}

const call = (name, args, key = KEY.address) => ({ type: "call", key, name, args });
const meetingChange = (meetingId, reason) => call("meeting.change", [meetingId, reason], KEY.meetingChange);

// ---- list and search ----

function refreshList(state) {
  const listId = state.listId + 1;
  return out({ ...state, listId }, [request(KEY.list, "/api/meetings", "GET", undefined, "list-loaded", "list-failed", { id: listId })]);
}

function startSearch(state) {
  const searchId = state.searchId + 1;
  const url = `/api/meetings?q=${encodeURIComponent(state.query)}`;
  return out({ ...state, searchId }, [request(KEY.search, url, "GET", undefined, "search-loaded", "search-failed", { id: searchId })]);
}

const repaintSidebar = (state) => ({ ...state, shownQuery: state.query });

// After the list is refreshed a typed query is searched again; the sidebar repaints when that answer is in.
function afterList(state) {
  return state.query ? startSearch(state) : out(repaintSidebar(state));
}

function listLoaded(state, action) {
  if (action.meta.id !== state.listId) return out(state);
  const body = action.response.body;
  const valid = body && Array.isArray(body.meetings) && Array.isArray(body.recent) && Number.isFinite(body.total);
  // A failed refresh keeps the last known list; it must not blank the sidebar.
  const next = valid ? { ...state, meetings: body.meetings, recent: body.recent, total: body.total } : state;
  return afterList(next);
}

function searchLoaded(state, action) {
  if (action.meta.id !== state.searchId) return out(state);
  const body = action.response.body;
  if (!body || !Array.isArray(body.meetings)) return out(repaintSidebar({ ...state, results: [], searchFailed: true }));
  return out(repaintSidebar({ ...state, results: body.meetings, searchFailed: false }));
}

function searchFailed(state, action) {
  if (action.meta.id !== state.searchId) return out(state);
  return out(repaintSidebar({ ...state, results: [], searchFailed: true }));
}

function searchInput(state, action) {
  const query = action.value;
  if (!query.trim()) {
    // Nothing to search: the sidebar goes back to the recent meetings at once; an answer still on its way is dropped.
    const next = repaintSidebar({ ...state, query, searchId: state.searchId + 1 });
    return out(next, [{ type: "cancel", key: KEY.searchTimer }, { type: "cancel", key: KEY.search }]);
  }
  return out({ ...state, query }, [{ type: "schedule", key: KEY.searchTimer, delay: SEARCH_DEBOUNCE_MS, action: { type: "history/search-due" } }]);
}

// ---- calendars ----

const calendarOf = (key) => {
  const { year, month } = parseKey(key);
  return { year, month, selected: key };
};
const selectDate = (state, key) => ({ ...state, calendar: calendarOf(key) });
const hasYear = (year) => year >= MIN_YEAR && year <= MAX_YEAR;

function shiftMonth(calendar, count) {
  const index = calendar.year * 12 + calendar.month + count;
  const year = Math.floor(index / 12);
  return hasYear(year) ? { ...calendar, year, month: index - year * 12 } : calendar;
}

function enterYear(state, text) {
  const year = validYear(text);
  if (year === null || year === state.calendar.year) return out(state);
  return out({ ...state, calendar: { ...state.calendar, year } });
}

function openCalendar(state, at) {
  if (state.view === "calendar") return out(state);
  const opened = focusOn(repaintSidebar({ ...state, loadId: state.loadId + 1, view: "calendar", heading: "calendar" }), "calendar-title");
  return then(refreshList(opened), (next) => out(settleDeletion(bump(next, "contextRev"), at)));
}

// ---- tabs ----

// The tab the address names: a #notes or #transcript hash, the tab this history entry remembers, or, for a
// saved-source link with no hash, the transcript tab where the source card lives.
export function locatedTab(at) {
  const named = (at.hash || "").slice(1);
  if (MEETING_TABS.includes(named)) return named;
  if (MEETING_TABS.includes(at.tab)) return at.tab;
  const params = new URLSearchParams(at.search || "");
  return params.has("notes_source") && params.has("segment") ? "transcript" : null;
}

// With no tab in the address: notes when the meeting has saved notes or is making some, else transcript.
// Unknown until notes.js has read the notes.
export function defaultTab(state) {
  if (!state.notes.known) return null;
  return state.notes.count > 0 || ACTIVE_TASK_STATES.includes(state.notes.status) ? "notes" : "transcript";
}

// Paint a tab, or none (null) while the default is still unknown.
function setTab(state, tab) {
  if (tab !== null) return out({ ...state, tab, tabWaiting: false }, state.tabWaiting ? [{ type: "cancel", key: KEY.tabTimer }] : []);
  if (state.tabWaiting) return out({ ...state, tab });
  return out({ ...state, tab, tabWaiting: true }, [{ type: "schedule", key: KEY.tabTimer, delay: TAB_PENDING_MS, action: { type: "history/tab-timed-out" } }]);
}

function resetTab(state) {
  return out({ ...state, tab: null, tabWaiting: false }, state.tabWaiting ? [{ type: "cancel", key: KEY.tabTimer }] : []);
}

// Shown whenever the meeting view appears: the address wins, then what the page already shows, then the default.
const initTab = (state, at) => setTab(state, locatedTab(at) ?? state.tab ?? defaultTab(state));

// A new history entry for the meeting page. The entry being left remembers its tab, so Back restores it even if
// the default for an address without a hash has changed since.
function pushMeetingUrl(state, at, url, tab = null) {
  const remember = state.tab && state.meeting && state.meeting.id === meetingIdFromPathname(at.pathname) ? state.tab : null;
  return [call("address.push", [url, tab, remember])];
}

function chooseTab(state, action) {
  const at = addressOf(action);
  if (!MEETING_TABS.includes(action.tab) || state.view !== "meeting") return out(state);
  if (action.tab === state.tab) return out(state);
  const effects = pushMeetingUrl(state, at, `${at.pathname}${at.search}#${action.tab}`, action.tab);
  return then(out(state, effects), (next) => setTab(next, action.tab));
}

// Opening a saved source from the notes: the Transcript tab, as one new history entry so Back returns to the notes.
function showSourceTab(state, action) {
  const at = addressOf(action);
  const { meetingId, snapshotId, segmentId } = action;
  if (state.view !== "meeting" || !state.meeting || state.meeting.id !== meetingId) return out(state);
  const query = `?notes_source=${encodeURIComponent(snapshotId)}&segment=${encodeURIComponent(segmentId)}${action.group ? `&group=${encodeURIComponent(action.group)}` : ""}`;
  const url = `${meetingPath(meetingId)}${query}#transcript`;
  const effects = `${at.pathname}${at.search}${at.hash}` !== url ? pushMeetingUrl(state, at, url, "transcript") : [];
  return then(out(state, effects), (next) => setTab(next, "transcript"));
}

// ---- editor ----

function closeEditor(state) {
  return { ...state, editor: null, saveId: state.saveId + 1 }; // an in-flight save may no longer touch the editor
}

function openEditor(state) {
  const meeting = state.meeting;
  if (!meeting) return out(state);
  // `original` is what the user started from; a field counts as edited only when its input differs from it.
  // `version` is the baseline the next save is checked against.
  const editSeq = state.editSeq + 1;
  const editor = {
    session: editSeq,
    original: { title: meeting.title, meeting_date: meeting.meeting_date },
    version: meeting.metadata_version,
    title: meeting.title,
    date: meeting.meeting_date || "",
    busy: false,
    error: "",
    conflict: null,
    saveLabel: t("Lưu thay đổi"),
    focusDate: 0,
  };
  return out(focusOn({ ...state, editSeq, editor, editStatus: "" }, "edit-title"));
}

export const sourceMutationUnavailable = (state) => !state.notes.lockKnown || state.notes.locked;

function submitEditor(state, action) {
  if (!state.editor || state.editor.busy) return out(state);
  // The inputs as they are at submit time; typing alone also reaches the state through `editor-edited`.
  const editor = { ...state.editor, title: action.title ?? state.editor.title, date: action.date ?? state.editor.date };
  const title = editor.title.trim();
  const fail = (patch) => out({ ...state, editor: { ...editor, ...patch } });
  if (!title || Array.from(title).length > 200) return fail({ error: t("Tên cuộc họp phải có từ 1 đến 200 ký tự.") });
  // A half-typed date reads as "" but is not a deletion: only "Xóa ngày" or an empty field removes the date.
  if (action.badInput || !validMeetingDate(editor.date)) {
    return fail({
      error: t("Ngày họp chưa đầy đủ hoặc không hợp lệ. Nhập đủ ngày, tháng, năm hoặc bấm “Xóa ngày” để bỏ ngày."),
      focusDate: editor.focusDate + 1,
    });
  }
  const meetingDate = editor.date || null;
  if (meetingDate !== editor.original.meeting_date && sourceMutationUnavailable(state)) {
    return fail({ error: t("Ngày họp đang bị khóa để giữ nguyên nguồn của notes. Tên cuộc họp vẫn có thể được lưu sau khi trả ngày về giá trị ban đầu.") });
  }
  const fields = {};
  if (title !== editor.original.title) fields.title = title;
  if (meetingDate !== editor.original.meeting_date) fields.meeting_date = meetingDate;
  if (Object.keys(fields).length === 0) return out(closeEditor(state));
  // The save outlives navigation. Its editor session is this meeting plus this editor: opening another meeting
  // drops the editor, but a calendar detour keeps it.
  const saveId = state.saveId + 1;
  const meta = { saveId, meetingId: state.meeting.id, session: editor.session };
  return out(
    { ...state, saveId, editor: { ...editor, busy: true, error: "" } },
    [request(KEY.save, `/api/meetings/${encodeURIComponent(meta.meetingId)}`, "PATCH", { expected_metadata_version: editor.version, ...fields }, "save-succeeded", "save-failed", meta)],
  );
}

const sameSession = (state, meta) => Boolean(state.meeting && state.meeting.id === meta.meetingId && state.editor && state.editor.session === meta.session);
const settleBusy = (state, meta) => (meta.saveId === state.saveId && state.editor ? { ...state, editor: { ...state.editor, busy: false } } : state);

function failSave(state, meta, message) {
  const next = sameSession(state, meta) ? { ...state, editor: { ...state.editor, error: message } } : state;
  return out(settleBusy(next, meta));
}

function saveSucceeded(state, action) {
  const { meta } = action;
  const updated = action.response.body;
  if (!updated || typeof updated !== "object" || typeof updated.id !== "string") return failSave(state, meta, t("Không thể lưu thay đổi."));
  let next = state;
  if (sameSession(state, meta)) {
    next = installMeeting(next, updated, "save");
    next = { ...closeEditor(next), editStatus: t("Đã lưu tên và ngày họp.") };
    if (next.view === "meeting") next = focusOn(bump(next, "paintRev"), "meeting-actions");
  }
  const result = refreshList(settleBusy(next, meta));
  result.effects.push(meetingChange(updated.id, "metadata"));
  return result;
}

function saveFailed(state, action) {
  const { meta } = action;
  const failure = failureOf(action.error);
  if (failure.code === "stale_version") {
    return out(state, [request(KEY.conflict, `/api/meetings/${encodeURIComponent(meta.meetingId)}`, "GET", undefined, "conflict-loaded", "conflict-failed", meta)]);
  }
  return failSave(state, meta, failure.message);
}

// Another tab saved first. Fields the user edited keep their draft; untouched fields follow the stored values so
// they are neither shown stale nor re-sent. Only the version baseline moves, and the user must save again explicitly.
function conflictLoaded(state, action) {
  const { meta } = action;
  const current = action.response.body;
  if (!sameSession(state, meta)) return refreshList(settleBusy(state, meta)); // the user moved on; only the lists may learn about it
  if (!current || typeof current !== "object" || typeof current.id !== "string") return conflictFailed(state, action);
  const editor = state.editor;
  const titleEdited = editor.title.trim() !== editor.original.title;
  const dateEdited = (editor.date || null) !== editor.original.meeting_date;
  const original = {
    title: titleEdited ? editor.original.title : current.title,
    meeting_date: dateEdited ? editor.original.meeting_date : current.meeting_date,
  };
  let next = installMeeting(state, current, "conflict");
  const date = current.meeting_date ? displayDate(current.meeting_date) : t("Chưa đặt ngày");
  const kept = [titleEdited && t("tên"), dateEdited && t("ngày họp")].filter(Boolean);
  const fields = kept.length === 2 ? [kept[0], t(" và "), kept[1]] : kept;
  next = {
    ...next,
    editor: {
      ...editor,
      original,
      title: titleEdited ? editor.title : current.title,
      date: dateEdited ? editor.date : current.meeting_date || "",
      version: current.metadata_version,
      saveLabel: t("Lưu bản nháp lên bản mới nhất"),
      conflict: t("Cuộc họp đã được cập nhật ở nơi khác: “{title}” · {date}. {kept}, hoặc hủy để dùng bản đã lưu.", {
        title: current.title,
        date,
        kept: kept.length
          ? t("Bản nháp {fields} của bạn vẫn được giữ; lưu lại để ghi đè phần đó", { fields })
          : t("Bạn chưa sửa trường nào khác bản đã lưu"),
      }),
    },
  };
  if (next.view === "meeting") next = bump(next, "paintRev");
  return refreshList(settleBusy(next, meta));
}

function conflictFailed(state, action) {
  const { meta } = action;
  if (!sameSession(state, meta)) return out(settleBusy(state, meta));
  const conflict = t("Cuộc họp đã được cập nhật ở nơi khác và chưa tải được bản mới. Bản nháp của bạn được giữ nguyên.");
  return out(settleBusy({ ...state, editor: { ...state.editor, conflict } }, meta));
}

function editEditor(state, action) {
  if (!state.editor) return out(state);
  const patch = {};
  if (action.title !== undefined) patch.title = action.title;
  if (action.date !== undefined) patch.date = action.date;
  return out({ ...state, editor: { ...state.editor, ...patch } });
}

// ---- installing a meeting from the server ----

// What the page learns from a meeting the server sent: the merged copy, a fresh notes summary when it is another
// meeting, and the notes source lock the copy carries.
function installMeeting(state, incoming, kind) {
  const previous = state.meeting;
  const other = !previous || !incoming || previous.id !== incoming.id;
  let notes = other ? { ...state.notes, count: 0, known: false, status: null } : state.notes;
  if (incoming && typeof incoming.notes_source_locked === "boolean") {
    notes = { ...notes, locked: incoming.notes_source_locked, lockKnown: true };
  } else if (other) {
    notes = { ...notes, locked: false, lockKnown: false };
  }
  return { ...state, meeting: mergeTranscription(previous, incoming), notes, installKind: kind, installRev: state.installRev + 1 };
}

// The task slice (task-core.js) reduces the same answers: the meeting on screen takes the task as its own.
function takeTask(state, meetingId, task) {
  const { meeting } = state;
  if (!meeting || meeting.id !== meetingId || !task || typeof task !== "object" || Array.isArray(task)) return state;
  // notes.js takes the lock from a meeting object it has not seen before; the copy must carry the lock as reported
  // since the load, not the one the server sent then (the old in-place edit kept the object, so it was never re-read).
  const lock = state.notes.lockKnown && typeof meeting.notes_source_locked === "boolean" ? { notes_source_locked: state.notes.locked } : {};
  return { ...state, meeting: { ...meeting, ...lock, transcription: { ...(meeting.transcription || {}), task } } };
}

// The transcript slice reduces the same answer: the meeting on screen takes the saved field (or speaker) as its own.
// notes.js is told of a saved field, not of an added speaker, as before.
function takeTranscriptAnswer(state, action) {
  if (!isRecord(action.response.body)) return { state, changed: false }; // the transcript slice reports it as a failed write
  const answer = answerOf(action.meta, action.response.body);
  const result = answer.speaker ? applySpeakerAdded(state.meeting, answer) : applyFieldAnswer(state.meeting, answer);
  const lock = state.notes.lockKnown && typeof state.meeting?.notes_source_locked === "boolean" ? { notes_source_locked: state.notes.locked } : {};
  const next = result.applied ? { ...state, meeting: { ...result.meeting, ...lock } } : state;
  return { state: answer.speaker ? next : bump(next, "contextRev"), changed: result.applied };
}

// The meeting read again after a run succeeded: a newer copy from the server, with the sidebar following the query.
function refreshedMeeting(state, action) {
  const payload = action.response.body;
  const meeting = payload && payload.meeting && typeof payload.meeting === "object" ? payload.meeting : payload;
  if (!state.meeting || state.meeting.id !== action.meta.meetingId || !meeting || !meeting.transcription) return state;
  const fetched = reduce(state, { type: "history/meeting-fetched", meeting });
  return repaintSidebar(fetched.state);
}

// ---- loading one meeting ----

function requestMeeting(state, meetingId, at) {
  const same = Boolean(state.meeting && state.meeting.id === meetingId);
  let result = out({ ...state, loadId: state.loadId + 1 });
  if (!same) {
    result = then(result, (s) => out({ ...s, left: { rev: s.left.rev + 1, kind: "switch" } }));
    result = then(result, resetTab);
    result = then(result, (s) => out(closeEditor(s)));
    result = then(result, (s) => out({ ...s, view: "loading", heading: "loading" }));
  } else {
    result = then(result, (s) => initTab(s, at)); // before the render tells notes.js of the meeting
    result = then(result, (s) => out(bump({ ...s, view: "meeting", heading: "meeting" }, "paintRev"))); // what we already know, while the refresh runs
  }
  result = then(result, (s) => out(settleDeletion(s, at)));
  const meta = { loadId: result.state.loadId, meetingId };
  const fetchEffect = request(KEY.meeting, `/api/meetings/${encodeURIComponent(meetingId)}`, "GET", undefined, "meeting-loaded", "meeting-load-failed", meta);
  return { state: result.state, effects: [...result.effects, fetchEffect] };
}

function loadFailed(state, message, at) {
  const loadError = t("{message} Hãy kiểm tra phiên truy cập hoặc mở lại cuộc họp từ workspace.", { message });
  return out(settleDeletion(bump({ ...state, view: "error", loadError, loadDone: state.loadId }, "contextRev"), at));
}

function meetingLoaded(state, action) {
  if (action.meta.loadId !== state.loadId) return out(state);
  const at = addressOf(action);
  const payload = action.response.body;
  let meeting = payload && payload.meeting && typeof payload.meeting === "object" ? payload.meeting : payload;
  if (!meeting || typeof meeting.title !== "string" || typeof meeting.id !== "string") {
    return loadFailed(state, t("Máy chủ không trả về metadata cuộc họp hợp lệ."), at);
  }
  if (!LANGUAGES[meeting.language_hint]) return loadFailed(state, t("Cuộc họp chưa có gợi ý ngôn ngữ hợp lệ."), at);
  // A read issued before a save landed can answer after it; never step back a version.
  const known = state.meeting;
  if (known && known.id === meeting.id && known.metadata_version > meeting.metadata_version) meeting = known;
  let result = out({ ...installMeeting(state, meeting, "load"), loadDone: state.loadId });
  result = then(result, (s) => out(bump({ ...s, view: "meeting", heading: "meeting" }, "paintRev")));
  result = then(result, (s) => initTab(s, at)); // before notes.js hears of the meeting, so its source view can take focus
  result = then(result, (s) => out(repaintSidebar(bump(s, "contextRev", "readyRev"))));
  if (!state.meetings.length) result = then(result, refreshList);
  return then(result, (s) => out(settleDeletion(s, at)));
}

function meetingLoadFailed(state, action) {
  if (action.meta.loadId !== state.loadId) return out(state);
  const failure = failureOf(action.error);
  return loadFailed(state, failure.message, addressOf(action));
}

function showHome(state, at) {
  let next = bump({ ...state, loadId: state.loadId + 1, view: "home", heading: "home", left: { rev: state.left.rev + 1, kind: "home" } }, "contextRev");
  next = repaintSidebar(next);
  return then(resetTab(next), (s) => out(settleDeletion(s, at)));
}

// ---- deletion ----

const pathOf = (at) => at.pathname;

// The delete confirmation failed while the meeting was still on screen: the meeting was reloaded first, and the
// error shows in the dialog once that load has ended (or was replaced).
function settleDeletion(state, at) {
  const after = state.deletion.after;
  if (!after) return state;
  let next = { ...state, deletion: { ...state.deletion, after: null } };
  const selected = next.meeting && next.meeting.id === after.meetingId && pathOf(at) === meetingPath(after.meetingId);
  if (selected && next.deletion.dialog.open) {
    next = { ...next, deletion: { ...next.deletion, dialog: { ...next.deletion.dialog, confirmation: "", error: after.message } } };
  }
  return next;
}

function openDeletion(state) {
  if (!state.meeting || state.deletion.pending) return out(state);
  return out(focusOn({ ...state, deletion: { ...state.deletion, dialog: { open: true, confirmation: "", error: null } } }, "meeting-delete-confirmation"));
}

function submitDeletion(state, action) {
  const meeting = state.meeting;
  const { deletion } = state;
  const confirmation = action.confirmation ?? deletion.dialog.confirmation;
  // deletion.after: a failed request is still waiting for its reload; the old page kept the button off until then unless the name was retyped; now it stays off.
  if (!meeting || deletion.pending || deletion.after || !deletion.dialog.open || confirmation !== meeting.title) return out(state);
  const { [meeting.id]: _dropped, ...failures } = deletion.failures;
  const next = { ...state, deletion: { ...deletion, pending: true, meetingId: meeting.id, failures, dialog: { ...deletion.dialog, error: null } } };
  return out(next, [request(KEY.remove, `/api/meetings/${encodeURIComponent(meeting.id)}`, "DELETE", undefined, "deletion-succeeded", "deletion-failed", { meetingId: meeting.id })]);
}

function deletionFailure(state, action, error) {
  const { meetingId } = action.meta;
  const at = addressOf(action);
  const failure = error ?? failureOf(action.error);
  const failureMessage = failure.message || t("Không thể xác nhận trạng thái dọn dẹp. Hãy tải lại cuộc họp để kiểm tra.");
  const message = failure.code === "meeting_task_active"
    ? t("Cuộc họp còn tác vụ đang chạy. Hãy hủy chép lời hoặc tạo notes, rồi chờ trạng thái kết thúc trước khi xóa.")
    : failure.code === "meeting_deletion_cleanup_failed"
      ? t("Dọn dẹp chưa hoàn tất. Yêu cầu đã được lưu để thử lại khi khởi động; khắc phục lỗi lưu trữ rồi thử lại.")
      : failureMessage;
  const cleanupMarked = failure.code === "meeting_deletion_cleanup_failed";
  let next = {
    ...state,
    deletion: { ...state.deletion, pending: false, meetingId: null, failures: { ...state.deletion.failures, [meetingId]: { message, cleanupMarked } } },
  };
  const selected = next.meeting && next.meeting.id === meetingId && pathOf(at) === meetingPath(meetingId);
  if (!selected) return out(next);
  // The page asks the server again, so what it shows is what the server holds; the dialog error waits for that.
  return then(requestMeeting(next, meetingId, at), (s) => out({ ...s, deletion: { ...s.deletion, after: { meetingId, message } } }));
}

function deletionSucceeded(state, action) {
  const { meetingId } = action.meta;
  const at = addressOf(action);
  const body = action.response.body;
  if (!body || body.meeting_id !== meetingId || body.status !== "deleted") {
    return deletionFailure(state, action, { message: t("Máy chủ chưa xác nhận việc xóa cuộc họp."), code: "" });
  }
  return deletionDone(state, meetingId, at);
}

function deletionDone(state, meetingId, at) {
  const { [meetingId]: _dropped, ...failures } = state.deletion.failures;
  const without = (items) => items.filter((item) => item.id !== meetingId);
  let next = {
    ...state,
    meetings: without(state.meetings),
    recent: without(state.recent),
    results: without(state.results),
    total: Math.max(0, state.total - 1),
    deletion: { ...state.deletion, pending: false, meetingId: null, failures, dialog: { open: false, confirmation: "", error: null } },
  };
  next = repaintSidebar(next);
  let result = out(next);
  if (next.meeting && next.meeting.id === meetingId && pathOf(at) === meetingPath(meetingId)) {
    result = then(result, (s) => out({ ...s, meeting: null }, [call("address.push", ["/", null, null])]));
    result = then(result, (s) => showHome(s, at));
    result = then(result, (s) => out(focusOn(s, "page-title")));
  }
  return then(result, refreshList);
}

function deletionFailed(state, action) {
  const failure = failureOf(action.error);
  // Deleting a meeting that is already gone is what the user asked for.
  if (failure.api && failure.status === 404 && failure.code === "meeting_not_found") {
    return deletionDone(state, action.meta.meetingId, addressOf(action));
  }
  return deletionFailure(state, action);
}

function closeDeletionDialog(state) {
  const { dialog } = state.deletion;
  if (!dialog.open && !dialog.confirmation && !dialog.error) return out(state);
  return out({ ...state, deletion: { ...state.deletion, dialog: { open: false, confirmation: "", error: null } } });
}

// ---- the reducer ----

export function reduce(state, action) {
  const at = addressOf(action);
  switch (action.type) {
    case "history/booted": {
      const start = { ...state, today: action.today, calendar: calendarOf(action.today) };
      const id = meetingIdFromPathname(at.pathname);
      const first = id ? requestMeeting(start, id, at) : showHome(start, at);
      return then(first, refreshList);
    }
    case "history/list-loaded":
      return listLoaded(state, action);
    case "history/list-failed":
      return action.meta.id === state.listId ? afterList(state) : out(state);
    case "history/search-input":
      return searchInput(state, action);
    case "history/search-due":
      return state.query.trim() ? startSearch(state) : out(repaintSidebar(state));
    case "history/search-loaded":
      return searchLoaded(state, action);
    case "history/search-failed":
      return searchFailed(state, action);

    case "history/calendar-step-clicked":
      return out({ ...state, calendar: shiftMonth(state.calendar, action.step) });
    case "history/calendar-today-clicked":
      return out(selectDate(state, action.today));
    case "history/calendar-undated-clicked": {
      const next = { ...state, calendar: { ...state.calendar, selected: "undated" } };
      return action.mini ? openCalendar(next, at) : out(next);
    }
    case "history/calendar-date-clicked": {
      const next = selectDate(state, action.date);
      return action.mini ? openCalendar(next, at) : out(next);
    }
    case "history/calendar-arrow-pressed": {
      const key = addDays(action.date, action.step);
      return hasYear(parseKey(key).year) ? out(selectDate(state, key)) : out(state);
    }
    case "history/calendar-year-entered":
      return enterYear(state, action.text);
    case "history/calendar-month-chosen":
      return out({ ...state, calendar: { ...state.calendar, month: Number(action.month) } });
    case "history/calendar-opened":
      return openCalendar(state, at);

    case "history/meeting-requested":
      return requestMeeting(state, action.meetingId, at);
    case "history/meeting-change-received":
      if (state.view !== "meeting" || !state.meeting || state.meeting.id !== action.meetingId) return out(state);
      return requestMeeting(state, action.meetingId, at);
    case "history/meeting-link-clicked": {
      const path = meetingPath(action.meetingId);
      const moved = at.pathname !== path ? { ...at, pathname: path, search: "", hash: "", tab: null } : at; // the address once pushed
      return then(out(state, at.pathname !== path ? [call("address.push", [path, null, null])] : []), (s) => requestMeeting(s, action.meetingId, moved));
    }
    case "history/meeting-loaded":
      return meetingLoaded(state, action);
    case "history/meeting-load-failed":
      return meetingLoadFailed(state, action);
    case "history/home-shown":
      return showHome(state, at);
    case "history/home-clicked": {
      const away = at.pathname !== "/" || at.search || at.hash;
      const moved = away ? { ...at, pathname: "/", search: "", hash: "", tab: null } : at; // the address once pushed
      return then(out(state, away ? [call("address.push", ["/", null, null])] : []), (s) => showHome(s, moved));
    }
    case "history/popped": {
      const id = meetingIdFromPathname(at.pathname);
      return id ? requestMeeting(state, id, at) : showHome(state, at);
    }
    case "history/hash-changed":
      return state.view === "meeting" ? initTab(state, at) : out(state);

    case "history/tab-chosen":
      return chooseTab(state, action);
    case "history/tab-timed-out":
      return state.tab === null ? setTab({ ...state, tabWaiting: false }, "transcript") : out({ ...state, tabWaiting: false });
    case "history/source-tab-shown":
      return showSourceTab(state, action);
    case "history/url-pushed":
      return out(state, pushMeetingUrl(state, at, action.url, action.tab));

    case "history/notes-summary-reported": {
      const { meeting } = state;
      if (!meeting || meeting.id !== action.meetingId) return out(state);
      const notes = { ...state.notes, count: Number(action.count) || 0, known: Boolean(action.known), status: action.status || null };
      const next = bump({ ...state, notes }, "summaryRev");
      return next.notes.known && next.view === "meeting" && next.tab === null ? initTab(next, at) : out(next);
    }
    case "history/notes-source-lock-reported": {
      const { meeting, notes } = state;
      if (!meeting || meeting.id !== action.meetingId) return out(state);
      if (notes.locked === Boolean(action.locked) && notes.lockKnown === Boolean(action.known)) return out(state);
      return out(bump({ ...state, notes: { ...notes, locked: Boolean(action.locked), lockKnown: Boolean(action.known) } }, "lockRev"));
    }

    case "history/meeting-fetched": {
      const { meeting } = action;
      if (!meeting || !state.meeting || state.meeting.id !== meeting.id) return out(state);
      return out(installMeeting(state, meeting, "refresh"));
    }
    case "task/poll-loaded":
    case "task/start-succeeded":
    case "task/cancel-succeeded":
      return out(takeTask(state, action.meta.meetingId, action.response.body));
    case "task/refresh-loaded":
      return out(refreshedMeeting(state, action));

    case "transcript/mutation-succeeded": {
      const result = takeTranscriptAnswer(state, action);
      // A valid server answer is a confirmed mutation even when navigation made the current-view merge
      // inapplicable. The mutation's meeting id is authoritative for the cross-tab refresh hint.
      return out(result.state, result.changed || isRecord(action.response.body) ? [meetingChange(action.meta.meetingId, "transcript")] : []);
    }
    case "transcript/reload-loaded": {
      // The same meeting, still the one installed when the read began, comes back as a newer copy.
      const body = action.response.body;
      const meeting = body && body.meeting && typeof body.meeting === "object" ? body.meeting : body;
      if (!state.meeting || action.meta.installRev !== state.installRev) return out(state);
      if (!meeting || meeting.id !== action.meta.meetingId || !meeting.transcription) return out(state);
      return out(repaintSidebar(reduce(state, { type: "history/meeting-fetched", meeting }).state));
    }

    case "history/editor-opened":
      return openEditor(state);
    case "history/editor-edited":
      return editEditor(state, action);
    case "history/editor-submitted":
      return submitEditor(state, action);
    case "history/editor-cancelled":
      return out(focusOn(closeEditor(state), "meeting-actions"));
    case "history/save-succeeded":
      return saveSucceeded(state, action);
    case "history/save-failed":
      return saveFailed(state, action);
    case "history/conflict-loaded":
      return conflictLoaded(state, action);
    case "history/conflict-failed":
      return conflictFailed(state, action);

    case "history/deletion-opened":
      return openDeletion(state);
    case "history/deletion-confirmation-edited":
      return state.deletion.dialog.open ? out({ ...state, deletion: { ...state.deletion, dialog: { ...state.deletion.dialog, confirmation: action.value } } }) : out(state);
    case "history/deletion-submitted":
      return submitDeletion(state, action);
    case "history/deletion-succeeded":
      return deletionSucceeded(state, action);
    case "history/deletion-failed":
      return deletionFailed(state, action);
    case "history/deletion-dialog-closed":
      return closeDeletionDialog(state);
    default:
      return out(state);
  }
}

// ---- selectors ----

export function selectHeading(state) {
  switch (state.heading) {
    case "home":
      return { title: t("Cuộc họp"), documentTitle: "Workspace · ASR Studio" };
    case "calendar":
      return { title: t("Lịch cuộc họp"), documentTitle: t("Lịch cuộc họp · ASR Studio") };
    case "loading":
      return { title: t("Cuộc họp"), documentTitle: t("Cuộc họp · ASR Studio") };
    case "meeting":
      // The name shows once, in the meeting title; the page heading above it stays generic.
      return { title: t("Cuộc họp"), documentTitle: `${state.meeting.title} · ASR Studio` };
    default:
      return null;
  }
}

export function selectSidebar(state) {
  const searching = state.shownQuery.trim() !== "";
  const items = searching ? state.results : state.recent;
  const noMeetings = !searching && state.total === 0;
  const noDated = !searching && state.total > 0 && state.recent.length === 0;
  return {
    items,
    heading: searching ? t("Kết quả") : t("Gần đây"),
    emptyHidden: !(noMeetings || noDated),
    emptyText: noMeetings ? t("Cuộc họp đã lưu sẽ xuất hiện tại đây.") : t("Chưa có cuộc họp nào có ngày. Mở nhóm Chưa đặt ngày trong lịch."),
    searchErrorHidden: !(searching && state.searchFailed),
    noResultsHidden: !(searching && !state.searchFailed && items.length === 0),
    noResultsText: t("Không tìm thấy cuộc họp nào khớp “{query}”.", { query: state.shownQuery.trim() }),
    workspaceEmptyHidden: state.total > 0,
    openId: state.meeting && state.view === "meeting" ? state.meeting.id : null,
  };
}

export function selectCalendar(state) {
  const { year, month, selected } = state.calendar;
  const monthKey = `${pad(year, 4)}-${pad(month + 1)}-`;
  const byDate = new Map();
  for (const meeting of state.meetings) {
    if (!meeting.meeting_date) continue;
    if (!byDate.has(meeting.meeting_date)) byDate.set(meeting.meeting_date, []);
    byDate.get(meeting.meeting_date).push(meeting);
  }
  const undated = state.meetings.filter((meeting) => !meeting.meeting_date);
  const monthCount = state.meetings.filter((meeting) => meeting.meeting_date && meeting.meeting_date.startsWith(monthKey)).length;
  const dayMeetings = selected === "undated" ? undated : byDate.get(selected) || [];
  return {
    year,
    month,
    selected,
    monthKey,
    byDate,
    cells: monthGrid(year, month),
    summary: monthCount ? t("{count} cuộc họp trong tháng", { count: monthCount }) : t("Không có cuộc họp trong tháng này."),
    undatedCount: undated.length,
    day: {
      title: selected === "undated" ? t("Chưa đặt ngày") : displayDate(selected),
      meetings: dayMeetings,
      emptyText: selected === "undated" ? t("Mọi cuộc họp đều đã có ngày.") : t("Không có cuộc họp trong ngày này."),
    },
  };
}

export function selectLockStatus(state) {
  if (!state.notes.lockKnown) {
    return { hidden: false, text: t("Đang kiểm tra trạng thái khóa nguồn trước khi cho phép sửa transcript, người nói hoặc ngày họp.") };
  }
  if (state.notes.locked) {
    return { hidden: false, text: t("Nguồn đang được dùng để tạo notes. Chỉnh sửa transcript, người nói và ngày họp tạm thời bị khóa.") };
  }
  return { hidden: true, text: "" };
}

export function selectEditor(state) {
  const editor = state.editor;
  if (!editor) return { open: false, status: state.editStatus };
  const locked = sourceMutationUnavailable(state);
  const dateChanged = (editor.date || null) !== editor.original.meeting_date;
  const dateLocked = !editor.busy && locked && !dateChanged;
  return {
    open: true,
    status: dateLocked ? selectLockStatus(state).text : state.editStatus,
    title: editor.title,
    date: editor.date,
    error: editor.error,
    conflict: editor.conflict,
    saveLabel: editor.saveLabel,
    focusDate: editor.focusDate,
    titleDisabled: editor.busy,
    dateDisabled: editor.busy || (locked && !dateChanged),
    clearDisabled: editor.busy || (locked && !dateChanged),
    saveDisabled: editor.busy || (locked && dateChanged),
    cancelDisabled: editor.busy,
  };
}

// The deletion block of the meeting header and the confirmation dialog.
export function selectDeletion(state) {
  const meeting = state.meeting;
  const { deletion } = state;
  const dialog = {
    open: deletion.dialog.open,
    confirmation: deletion.dialog.confirmation,
    error: deletion.dialog.error,
  };
  if (!meeting) return { header: null, dialog: { ...dialog, confirmDisabled: true, confirmLabel: t("Xóa dữ liệu đã lưu"), name: "" } };
  const marker = meeting.deletion_status;
  const failure = deletion.failures[meeting.id];
  const message = failure ? failure.message : "";
  const cleanupMarked = Boolean(marker || (failure && failure.cleanupMarked));
  const requestPending = deletion.pending && deletion.meetingId === meeting.id;
  const settling = Boolean(deletion.after);
  let text = message;
  if (requestPending) {
    text = t("Đang dọn dữ liệu… Nếu bị gián đoạn, hệ thống sẽ tự thử lại khi khởi động; nếu vẫn chưa xong, xử lý sự cố rồi chọn “Thử dọn tiếp”.");
  } else if (marker === "failed") {
    text = t("Dọn dẹp chưa hoàn tất. Hệ thống sẽ tự thử lại khi khởi động; sau khi xử lý sự cố, hãy chọn “Thử dọn tiếp”.");
  } else if (marker === "pending") {
    text = t("Dọn dẹp chưa hoàn tất. Hệ thống sẽ thử lại khi khởi động; sau khi xử lý sự cố, hãy chọn “Thử dọn tiếp”.");
  }
  return {
    header: {
      hidden: !(requestPending || marker || message),
      state: requestPending ? "pending" : marker || (cleanupMarked ? "failed" : "error"),
      text,
      deleteDisabled: deletion.pending,
      deleteLabel: cleanupMarked ? t("Thử dọn tiếp") : t("Xóa cuộc họp"),
    },
    dialog: {
      ...dialog,
      name: meeting.title,
      confirmDisabled: deletion.pending || settling || dialog.confirmation !== meeting.title,
      confirmLabel: requestPending || settling ? t("Đang dọn dữ liệu…") : cleanupMarked ? t("Thử dọn tiếp") : t("Xóa dữ liệu đã lưu"),
    },
  };
}
