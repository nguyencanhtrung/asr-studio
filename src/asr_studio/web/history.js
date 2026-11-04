// Meeting history: sidebar recent/search, synced month calendars, meeting navigation and metadata editing
// (phase 4a of #118), the transcription task (phase 4b), the transcript editing (phase 4c) and the playback (4d).
//
// The navigation and library state lives in history-core.js, the task state in task-core.js, the transcript state in
// transcript-core.js and the playback state in playback-core.js, all in one store; this file renders them and
// dispatches actions.
// Contract: docs/adr/0010-frontend-reducer-core.md; state tables: docs/frontend-design-v1.md §11.8 to §11.11.
// Announces the open meeting on the channel (channel.js) and offers notes.js its commands (meetingHistory).
import { combine, createRunner, createStore } from "./store.js";
import { meetingChanges, meetingContext } from "./channel.js";
import { formatMeetingDate, meetingIdFromPath, sameOriginAudioSource, setProp, tr } from "./shell.js";
import {
  ACTIVE_TASK_STATES,
  LANGUAGES,
  MEETING_TABS,
  MAX_YEAR,
  MIN_YEAR,
  addDays,
  dateKey,
  displayDate,
  formatRecordingDuration,
  generationsFor,
  initialState,
  parseKey,
  reduce,
  selectCalendar,
  selectDeletion,
  selectEditor,
  selectHeading,
  selectLockStatus,
  selectSidebar,
  sourceMutationUnavailable as coreSourceMutationUnavailable,
} from "./history-core.js";
import {
  autoStartStorageKey,
  initialState as taskInitialState,
  reduce as reduceTask,
  requestStorageKey,
  selectRequestBusy,
  selectRuntime,
  selectTask,
} from "./task-core.js";
import {
  canPlay,
  initialState as playbackInitialState,
  isScrollKey,
  reduce as reducePlayback,
  sourceRow,
  validSource,
} from "./playback-core.js";
import {
  awaitsLatestReload,
  displayed,
  draftOf,
  fieldValue,
  hasSegmentDraft as coreHasSegmentDraft,
  hasUnsavedDrafts,
  initialState as transcriptInitialState,
  isEditing,
  isRenaming,
  navigation,
  reduce as reduceTranscript,
  speakerAddReady,
} from "./transcript-core.js";

const $ = (selector) => document.querySelector(selector);
const MEETING_CHANGE_CHANNEL = "asr-studio:meeting-changes";
const meetingChangeTransport = typeof window.BroadcastChannel === "function"
  ? new window.BroadcastChannel(MEETING_CHANGE_CHANNEL)
  : null;
const WEEKDAYS = [t("T2"), t("T3"), t("T4"), t("T5"), t("T6"), t("T7"), t("CN")];
const MONTHS = [
  t("Tháng 1"), t("Tháng 2"), t("Tháng 3"), t("Tháng 4"), t("Tháng 5"), t("Tháng 6"),
  t("Tháng 7"), t("Tháng 8"), t("Tháng 9"), t("Tháng 10"), t("Tháng 11"), t("Tháng 12"),
];
const REVIEW_REASON_LABELS = {
  asr_uncertain: t("Nhận dạng chưa chắc"),
  overlap: t("Có lời nói chồng lấn"),
  multiple_speakers: t("Nhiều người nói trong đoạn"),
  low_speaker_dominance: t("Người nói chưa rõ"),
  unidentified_speaker: t("Chưa xác định người nói"),
};
const VIEWS = {
  home: "#home-view",
  loading: "#meeting-loading",
  error: "#meeting-error",
  meeting: "#meeting-view",
  calendar: "#calendar-view",
};

// ---- store ----

const nav = () => store.getState().history;
const tstate = () => store.getState().transcript;
const transcriptionNow = () => {
  const meeting = nav().meeting;
  return meeting ? meeting.transcription : null;
};
// The generation on screen and whether it takes edits, as the core reads them from the meeting.
const shownGeneration = () => displayed(tstate(), transcriptionNow());
const transcriptDraft = (generationId, field, itemId) => draftOf(tstate().drafts, generationId, field, itemId);
const isEditingSegment = (generationId, segmentId) => isEditing(tstate(), generationId, segmentId);
const isRenamingSpeaker = (generationId, speakerId) => isRenaming(tstate(), generationId, speakerId);
const hasSegmentDraft = (generationId, segmentId) => coreHasSegmentDraft(tstate(), generationId, segmentId);
const speakerAddRecoveryReady = (generation, draft) => speakerAddReady(tstate(), generation, draft, sourceMutationUnavailable());
const sourceMutationUnavailable = () => coreSourceMutationUnavailable(nav());

function publishMeetingChange(meetingId, reason, local = false) {
  if (typeof meetingId !== "string" || !meetingId) return;
  const detail = { meetingId, reason: typeof reason === "string" ? reason : "meeting", local };
  if (local) meetingChanges.dispatch({ type: "channel/meeting-changed", detail });
  if (!meetingChangeTransport) return;
  try {
    // `local` only guards this tab's subscriber; never send that guard over the wire or receivers would ignore it.
    meetingChangeTransport.postMessage({ meetingId: detail.meetingId, reason: detail.reason });
  } catch {
    // A closing tab can close its channel between the reducer effect and postMessage.
  }
}

// The page address enters the core as data: every action carries where the address pointed when it was made.
function addressNow() {
  const remembered = window.history.state && window.history.state.meetingTab;
  return { pathname: window.location.pathname, search: window.location.search, hash: window.location.hash, tab: remembered || null };
}

// The meeting on screen as the task slice reasons about it: history-core owns the meeting, and `combine` hands a
// slice only its own state, so the facts enter task-core as data on each action, like the address.
function shownNow() {
  const state = nav();
  const meeting = state.meeting;
  if (!meeting) return null;
  const transcription = meeting.transcription || {};
  const generation = transcription.generation || null;
  const onScreen = shownGeneration().generation;
  return {
    id: meeting.id,
    language: meeting.language_hint,
    task: transcription.task || null,
    asrModels: Array.isArray(transcription.asr_models) ? transcription.asr_models : [],
    meeting, // the transcript slice reasons about the meeting's generations (the object is never changed in place)
    view: state.view,
    installRev: state.installRev,
    generation: Boolean(generation),
    generationLanguage: generation ? generation.language_hint : null,
    blocked: coreSourceMutationUnavailable(state),
    tab: state.tab,
    generationId: onScreen ? onScreen.id : null,
    // The playback slice reasons about the rows on the page: those of the generation whose list is painted.
    rows: onScreen && onScreen.id === paintedGenerationId() && Array.isArray(onScreen.segments) ? onScreen.segments : null,
  };
}

// The generation whose rows the transcript list holds (its render key starts with the generation id).
function paintedGenerationId() {
  const key = $("#transcript-generation").dataset.renderKey;
  return key ? key.split(":").slice(0, -2).join(":") : null;
}

// The clock enters the core as data, on every action.
const stamp = (action) => ({ ...action, at: addressNow(), shown: shownNow(), now: performance.now() });
const dispatch = (action) => store.dispatch(stamp(action));
// Scroll events come in bursts and need neither the address nor the meeting.
const dispatchBare = (action) => store.dispatch({ ...action, now: performance.now() });

// "address.push": a new history entry. The entry being left remembers its tab, so Back restores it even if the
// default for an address without a hash has changed since.
function pushAddress(url, tab, remember) {
  if (remember) window.history.replaceState({ ...window.history.state, meetingTab: remember }, "");
  window.history.pushState(tab ? { meetingTab: tab } : {}, "", url);
}

const run = createRunner({
  fetch: (url, init) => fetch(url, init),
  setTimeout: (callback, delay) => window.setTimeout(callback, delay),
  clearTimeout: (id) => window.clearTimeout(id),
  navigate: (url) => window.location.assign(url),
  storage: { session: () => window.sessionStorage },
  calls: {
    "address.push": pushAddress,
    "meeting.change": (meetingId, reason) => publishMeetingChange(meetingId, reason, true),
    "clipboard.write": writeClipboard,
    "clipboard.select": selectDiagnosticCommand,
    "transcript.copy": copyTranscriptDraft,
    "playback.seek": seekAudio,
  },
});
const store = createStore({
  reduce: combine({ history: reduce, task: reduceTask, transcript: reduceTranscript, playback: reducePlayback }),
  initialState: { history: initialState, task: taskInitialState, transcript: transcriptInitialState, playback: playbackInitialState },
  run: (effect, send) => run(effect, (action) => send(stamp(action))),
});

function receiveMeetingChange(detail) {
  if (!detail || typeof detail.meetingId !== "string" || !detail.meetingId) return;
  meetingChanges.dispatch({ type: "channel/meeting-changed", detail });
}

meetingChangeTransport?.addEventListener("message", (event) => receiveMeetingChange(event.data));
meetingChanges.subscribe((detail) => {
  const state = nav();
  if (!detail || detail.local || state.view !== "meeting" || !state.meeting || state.meeting.id !== detail.meetingId) return;
  dispatch({ type: "history/meeting-change-received", meetingId: detail.meetingId });
});

// ---- painting ----

// The view on screen, read from the page itself: the DOM renderers announce the meeting while the
// core's view is already ahead of what is painted.
function paintedView() {
  return Object.keys(VIEWS).find((name) => !$(VIEWS[name]).hidden) ?? null;
}

function notifyMeetingContext() {
  const view = paintedView();
  meetingContext.dispatch({
    type: "channel/meeting-context",
    detail: {
      meeting: view === "meeting" ? nav().meeting : null,
      view,
      "transcriptionRequestInFlight": selectRequestBusy(store.getState().task),
    },
  });
}

// What the view painted last, to paint only what changed; it lives in this closure.
const render = ((memory) => (root) => {
  const s = root.history;
  const p = memory.history;
  const taskRev = root.task.rev !== memory.task.rev;
  const playbackOnly = s === p && root.task === memory.task && root.transcript === memory.transcript;
  memory.history = s;
  memory.task = root.task;
  if (playbackOnly) {
    paintPlayback(root, memory);
    return;
  }
  if (s.left.rev !== p.left.rev) meetingLeft(s.left.kind);
  if (s.installRev !== p.installRev) meetingInstalled();
  else if (s.lockRev !== p.lockRev) renderLegacyLock();
  // A refresh of the same meeting shows the tab first, so notes.js hears of the meeting with its panel visible.
  const tabFirst = s.installRev === p.installRev && s.tab !== p.tab && s.view === "meeting" && p.view === "meeting";
  if (tabFirst) renderTabs(s);
  if (s.paintRev !== p.paintRev && s.meeting) renderMeeting(s.meeting);
  // A task answer repaints the transcription unless the meeting was painted above anyway.
  const painted = (s.paintRev !== p.paintRev && s.meeting) || s.installRev !== p.installRev || s.lockRev !== p.lockRev;
  if (taskRev && !painted) repaintTranscription();
  if (s.view === "meeting" && s.meeting) loadAudio(s.meeting);
  if (s.view !== p.view || s.heading !== p.heading || s.paintRev !== p.paintRev) renderViews(s);
  if (!tabFirst && (s.tab !== p.tab || s.paintRev !== p.paintRev || (s.view === "meeting" && p.view !== "meeting"))) renderTabs(s);
  renderLock(s);
  renderEditor(s);
  renderDeletion(s);
  if (s.view !== p.view || s.shownQuery !== p.shownQuery || s.results !== p.results || s.recent !== p.recent
    || s.total !== p.total || s.searchFailed !== p.searchFailed || openMeetingId(s) !== openMeetingId(p)) renderSidebar(s);
  if (s.meetings !== p.meetings || s.calendar !== p.calendar) renderCalendars(s);
  if (s.summaryRev !== p.summaryRev && s.meeting) {
    const transcription = s.meeting.transcription || {};
    renderMeetingProcessingStatus(transcription.task, transcription.generation);
  }
  if (s.contextRev !== p.contextRev) notifyMeetingContext();
  if (s.readyRev !== p.readyRev && s.meeting) meetingReady(s.meeting);
  if (s.focus.rev !== p.focus.rev) focusTarget(s.focus.target);
  if (s.editor && p.editor && s.editor.focusDate !== p.editor.focusDate) $("#edit-date").focus();
  paintTranscript(root, memory);
  paintPlayback(root, memory);
})({ history: initialState, task: taskInitialState, transcript: transcriptInitialState, playback: playbackInitialState });

// The sidebar marks the open meeting by id; a task answer replaces the meeting object every second.
const openMeetingId = (s) => (s.meeting ? s.meeting.id : null);

function repaintTranscription() {
  const meeting = nav().meeting;
  if (meeting && meeting.transcription) renderTranscription(meeting.transcription);
  else notifyMeetingContext();
}

const FOCUS = {
  "edit-title": "#edit-title",
  "meeting-actions": "#meeting-actions",
  "page-title": "#page-title",
  "calendar-title": "#calendar-title",
  "meeting-delete-confirmation": "#meeting-delete-confirmation",
};

function focusTarget(target) {
  if (FOCUS[target]) $(FOCUS[target]).focus();
}

function renderViews(s) {
  for (const [name, selector] of Object.entries(VIEWS)) setProp($(selector), "hidden", name !== s.view);
  if (s.view === "error") setProp($("#meeting-error-copy"), "textContent", tr(s.loadError));
  const heading = selectHeading(s);
  if (heading) {
    setProp($("#page-title"), "textContent", tr(heading.title));
    document.title = tr(heading.documentTitle);
  }
}

// The meeting header and the transcript half, painted when the core says the meeting is due a repaint.
function renderMeeting(meeting) {
  $("#meeting-title").textContent = meeting.title;
  const date = formatMeetingDate(meeting.meeting_date);
  const dateElement = $("#meeting-date");
  dateElement.textContent = date.label;
  if (date.dateTime) dateElement.setAttribute("datetime", date.dateTime);
  else dateElement.removeAttribute("datetime");
  $("#meeting-language").textContent = tr(LANGUAGES[meeting.language_hint]);
  renderTranscription(meeting.transcription);
}

function loadAudio(meeting) {
  const audio = $("#meeting-audio");
  if (audio.dataset.meetingId === meeting.id) return;
  const audioUrl = meeting.audio_url || (meeting.recording && meeting.recording.audio_url);
  audio.src = sameOriginAudioSource(audioUrl, meeting.id);
  audio.dataset.meetingId = meeting.id;
  audio.load();
}

// A meeting from the server became the current one: repaint what depends on the notes source lock and tell
// notes.js. The transcript slice reconciles its drafts with it (paintTranscript asks for that) before the
// transcript is painted.
function meetingInstalled() {
  renderLegacyLock();
  notifyMeetingContext();
}

// The transcript half of a notes source lock change; the status line and the editor controls paint from state.
function renderLegacyLock() {
  syncTranscriptMutationControls();
  const meeting = nav().meeting;
  if (meeting && meeting.transcription) renderTranscription(meeting.transcription);
  syncTranscriptMutationControls();
}

function renderLock(s) {
  const status = selectLockStatus(s);
  setProp($("#notes-lock-status"), "textContent", tr(status.text));
  setProp($("#notes-lock-status"), "hidden", status.hidden);
}

// A meeting finished loading: task-core follows its running task, restores a pending request id and starts the
// transcription the create dialog asked for. The values it cannot read itself come with the action.
function meetingReady(meeting) {
  dispatch({
    type: "task/meeting-ready",
    requestId: newRequestId(),
    stored: readStored(requestStorageKey(meeting.id)),
    autoStart: readStored(autoStartStorageKey(meeting.id)) !== null,
  });
}

// Leaving a meeting (or home) stops its task poll and its seek; the transcript and the notes source mark start over for another meeting.
function meetingLeft(kind) {
  dispatch({ type: "task/meeting-left" });
  if (kind === "switch") dispatch({ type: "transcript/meeting-switched" });
  dispatch({ type: "playback/meeting-left", kind });
}

// ---- meeting tabs: Meeting notes | Transcript & bản ghi ----

// The tab is painted, or none while the default is still unknown. Then no tab is selected and the notes panel
// is hidden; the transcript panel, the likelier answer, is laid out but transparent, so the other panel never
// flashes and the transcript's controls are already in place when it is the one.
function renderTabs(s) {
  for (const name of MEETING_TABS) {
    const selected = name === s.tab;
    $(`#meeting-tab-${name}`).setAttribute("aria-selected", String(selected));
    // Roving tabindex: only the chosen tab is a Tab stop (the first one, before any is chosen).
    $(`#meeting-tab-${name}`).tabIndex = selected || (s.tab === null && name === MEETING_TABS[0]) ? 0 : -1;
  }
  $("#meeting-panel-notes").hidden = s.tab !== "notes";
  $("#meeting-panel-transcript").hidden = s.tab === "notes";
  $("#meeting-panel-transcript").classList.toggle("is-pending", s.tab === null);
}

// Choose a tab as the user does: a new history entry with #tab, so Back returns to the previous tab.
function selectTab(tab, { focus = false } = {}) {
  if (!MEETING_TABS.includes(tab) || nav().view !== "meeting") return;
  dispatch({ type: "history/tab-chosen", tab });
  if (focus) $(`#meeting-tab-${tab}`).focus();
}

// Opening a saved source from the notes: the Transcript tab, as one new history entry so Back returns to
// the notes. A pasted or restored link already names that address, so only a click needs this.
function showNotesSourceTab({ meetingId, snapshotId, segmentId, group }) {
  const state = nav();
  if (state.view !== "meeting" || !state.meeting || state.meeting.id !== meetingId) return false;
  dispatch({ type: "history/source-tab-shown", meetingId, snapshotId, segmentId, group });
  return true;
}

// The tab names what its panel holds: how many segments, and how many of them ask for a second look.
function renderTabCounts(generation) {
  const segments = generation && Array.isArray(generation.segments) ? generation.segments : null;
  $("#meeting-tab-count").textContent = segments
    ? t("{count} đoạn · {review} cần kiểm tra", { count: segments.length, review: segments.filter(needsReview).length })
    : "";
}

// ---- sidebar ----

function renderSidebar(s) {
  const view = selectSidebar(s);
  setProp($("#recent-heading"), "textContent", tr(view.heading));
  const empty = $("#sidebar-empty");
  setProp(empty, "hidden", view.emptyHidden);
  setProp(empty, "textContent", tr(view.emptyText));
  setProp($("#search-error"), "hidden", view.searchErrorHidden);
  const noResults = $("#search-empty");
  setProp(noResults, "hidden", view.noResultsHidden);
  setProp(noResults, "textContent", tr(view.noResultsText));
  setProp($("#workspace-empty"), "hidden", view.workspaceEmptyHidden);
  // Rebuilding the list would drop keyboard focus from the link being followed; give it back to the same meeting.
  const focused = document.activeElement;
  const focusedId = focused && focused.closest("#recent-list") ? focused.dataset.meetingLink : null;
  $("#recent-list").replaceChildren(
    ...view.items.map((meeting) => {
      const link = meetingLink(meeting, "recent-link");
      if (meeting.id === view.openId) link.setAttribute("aria-current", "page");
      const item = document.createElement("li");
      item.append(link);
      return item;
    }),
  );
  if (focusedId) {
    const again = [...$("#recent-list").querySelectorAll("[data-meeting-link]")].find((link) => link.dataset.meetingLink === focusedId);
    if (again) again.focus();
  }
}

function meetingLink(meeting, className) {
  const link = document.createElement("a");
  link.className = className;
  link.href = `/meetings/${encodeURIComponent(meeting.id)}`;
  link.dataset.meetingLink = meeting.id;
  link.title = meeting.title;
  link.setAttribute("aria-label", meeting.title);
  const title = document.createElement("span");
  title.className = "link-title";
  title.textContent = meeting.title;
  const date = document.createElement("span");
  date.className = "link-date";
  const parts = [meeting.meeting_date ? displayDate(meeting.meeting_date) : t("Chưa đặt ngày")];
  if (meeting.duration_seconds > 0) parts.push(tr(formatRecordingDuration(meeting.duration_seconds)));
  date.textContent = parts.join(" · ");
  link.append(title, date);
  if (meeting.deletion_status) {
    const deletionState = document.createElement("span");
    deletionState.className = "meeting-link-deletion-state";
    deletionState.dataset.state = meeting.deletion_status;
    deletionState.textContent = meeting.deletion_status === "failed" ? t("Dọn dẹp bị lỗi") : t("Đang dọn dẹp");
    link.append(deletionState);
  }
  return link;
}

// ---- calendars (small + large share one state) ----

const todayKey = () => {
  const now = new Date();
  return dateKey(now.getFullYear(), now.getMonth(), now.getDate());
};

const CALENDAR_TEMPLATE = `
    <div class="cal-nav">
      <button type="button" class="cal-step" data-step="-1" aria-label="${t("Tháng trước")}">‹</button>
      <strong class="cal-title" aria-live="polite"></strong>
      <button type="button" class="cal-step" data-step="1" aria-label="${t("Tháng sau")}">›</button>
    </div>
    <div class="cal-pick">
      <select class="cal-month" aria-label="${t("Tháng")}"></select>
      <input class="cal-year" type="number" inputmode="numeric" aria-label="${t("Năm")}" min="${MIN_YEAR}" max="${MAX_YEAR}" />
      <button type="button" class="cal-today">${t("Hôm nay")}</button>
    </div>
    <p class="cal-summary"></p>
    <div class="cal-weekdays" aria-hidden="true">${WEEKDAYS.map((name) => `<span>${name}</span>`).join("")}</div>
    <div class="cal-days"></div>
    <button type="button" class="cal-undated"><span>${t("Chưa đặt ngày")}</span><span class="cal-undated-count"></span></button>`;

function buildCalendar(root) {
  root.innerHTML = CALENDAR_TEMPLATE;
  const select = root.querySelector(".cal-month");
  for (let month = 0; month < 12; month += 1) select.add(new Option(MONTHS[month], String(month)));
}

function renderCalendar(root, large, view) {
  const { year, month, selected, monthKey, byDate } = view;
  const todayValue = todayKey();
  const activeElement = document.activeElement;
  const focusedDate =
    activeElement && root.contains(activeElement) && activeElement.classList.contains("cal-day")
      ? activeElement.dataset.date
      : null;
  root.querySelector(".cal-title").textContent = `${MONTHS[month]} · ${year}`;
  root.querySelector(".cal-month").value = String(month);
  // A background refresh must not overwrite what the user is typing in the year box.
  const yearInput = root.querySelector(".cal-year");
  if (document.activeElement !== yearInput) yearInput.value = String(year);
  root.querySelector(".cal-summary").textContent = tr(view.summary);

  const cells = view.cells.map((key) => {
    const meetings = byDate.get(key) || [];
    const inMonth = key.startsWith(monthKey);
    const cell = document.createElement("div");
    cell.className = "cal-cell";
    const day = document.createElement("button");
    day.type = "button";
    day.className = "cal-day";
    day.dataset.date = key;
    day.classList.toggle("is-outside", !inMonth);
    day.classList.toggle("is-today", key === todayValue);
    day.classList.toggle("is-selected", key === selected);
    day.classList.toggle("has-meetings", meetings.length > 0);
    day.setAttribute("aria-pressed", String(key === selected));
    day.setAttribute(
      "aria-label",
      `${displayDate(key)}, ${meetings.length ? t("{count} cuộc họp", { count: meetings.length }) : t("không có cuộc họp")}`,
    );
    day.tabIndex = key === selected ? 0 : -1;
    const number = document.createElement("span");
    number.className = "cal-number";
    number.textContent = String(parseKey(key).day);
    day.append(number);
    if (meetings.length) {
      const count = document.createElement("span");
      count.className = "cal-count";
      count.textContent = String(meetings.length);
      day.append(count);
    }
    cell.append(day);
    if (large) {
      for (const meeting of meetings.slice(0, 2)) {
        const chip = document.createElement("a");
        chip.className = "cal-chip";
        chip.href = `/meetings/${encodeURIComponent(meeting.id)}`;
        chip.dataset.meetingLink = meeting.id;
        chip.textContent = meeting.title;
        chip.title = meeting.title;
        chip.setAttribute("aria-label", meeting.title);
        cell.append(chip);
      }
      if (meetings.length > 2) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "cal-more";
        more.dataset.date = key;
        more.textContent = t("+{count} khác", { count: meetings.length - 2 });
        cell.append(more);
      }
    }
    return cell;
  });
  root.querySelector(".cal-days").replaceChildren(...cells);
  // Roving tabindex: keep one stop in the grid even when the selection is off-screen.
  const days = root.querySelectorAll(".cal-day");
  if (!root.querySelector('.cal-day[tabindex="0"]')) {
    const fallback = root.querySelector(`.cal-day[data-date="${monthKey}01"]`) || days[0];
    fallback.tabIndex = 0;
  }
  if (focusedDate) root.querySelector(`.cal-day[data-date="${focusedDate}"]`)?.focus();
  root.querySelector(".cal-undated-count").textContent = String(view.undatedCount);
  root.querySelector(".cal-undated").classList.toggle("is-selected", selected === "undated");
  root.querySelector(".cal-undated").setAttribute("aria-pressed", String(selected === "undated"));
}

function renderDayPanel(view) {
  const { day } = view;
  $("#day-panel-title").textContent = tr(day.title);
  $("#day-panel-count").textContent = String(day.meetings.length);
  const empty = $("#day-panel-empty");
  empty.hidden = day.meetings.length > 0;
  empty.textContent = tr(day.emptyText);
  $("#day-panel-list").replaceChildren(
    ...day.meetings.map((meeting) => {
      const item = document.createElement("li");
      item.append(meetingLink(meeting, "day-meeting"));
      return item;
    }),
  );
}

function renderCalendars(s) {
  const view = selectCalendar(s);
  renderCalendar($("#mini-calendar"), false, view);
  renderCalendar($("#large-calendar"), true, view);
  renderDayPanel(view);
}

function focusDay(root, key) {
  root.querySelector(`.cal-day[data-date="${key}"]`)?.focus();
}

function onCalendarClick(event) {
  const root = event.currentTarget;
  const target = event.target.closest("button");
  if (!target || !root.contains(target)) return;
  const mini = root.dataset.calendar === "mini";
  if (target.dataset.step) {
    dispatch({ type: "history/calendar-step-clicked", step: Number(target.dataset.step) });
  } else if (target.classList.contains("cal-today")) {
    dispatch({ type: "history/calendar-today-clicked", today: todayKey() });
  } else if (target.classList.contains("cal-undated")) {
    dispatch({ type: "history/calendar-undated-clicked", mini });
  } else if (target.dataset.date) {
    dispatch({ type: "history/calendar-date-clicked", date: target.dataset.date, mini });
    if (!mini) focusDay(root, target.dataset.date); // the small calendar opens the large one, which takes focus
  }
}

// The year box commits as soon as it holds a complete 4-digit year in range (typing, paste, spinner), so
// Enter/blur never depend on the browser firing `change`. Partial or out-of-range text is left alone while
// editing and reverted on Enter/blur.
function onCalendarInput(event) {
  if (!event.target.classList.contains("cal-year")) return;
  dispatch({ type: "history/calendar-year-entered", text: event.target.value });
}

// Enter/blur: keep a valid year, otherwise put the real year back into the box. The grid is only rebuilt when
// the year actually changes, so a blur caused by clicking a day never replaces the button under the pointer.
function commitYear(input) {
  dispatch({ type: "history/calendar-year-entered", text: input.value });
  input.value = String(nav().calendar.year);
}

function onCalendarChange(event) {
  if (event.target.classList.contains("cal-month")) {
    dispatch({ type: "history/calendar-month-chosen", month: event.target.value });
  }
}

function onCalendarFocusout(event) {
  if (event.target.classList.contains("cal-year")) commitYear(event.target);
}

const ARROW_STEPS = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
function onCalendarKeydown(event) {
  if (event.key === "Enter" && event.target.classList.contains("cal-year")) {
    event.preventDefault();
    commitYear(event.target);
    return;
  }
  const day = event.target.closest(".cal-day");
  if (!day || !(event.key in ARROW_STEPS)) return;
  event.preventDefault();
  const step = ARROW_STEPS[event.key];
  const key = addDays(day.dataset.date, step);
  const { year } = parseKey(key);
  if (year < MIN_YEAR || year > MAX_YEAR) return;
  dispatch({ type: "history/calendar-arrow-pressed", date: day.dataset.date, step });
  focusDay(event.currentTarget, key);
}

// ---- meeting navigation ----

// Resolves true when this call painted the final view, false when newer navigation won.
// Call it outside a store drain (not from a render or a channel/meeting-context subscriber): inside one the dispatch
// is queued, so loadId would still be the old one here and the promise would resolve false at once.
function loadMeeting(meetingId) {
  dispatch({ type: "history/meeting-requested", meetingId });
  const loadId = nav().loadId;
  return new Promise((resolve) => {
    const settle = (s) => {
      if (s.loadId !== loadId) resolve(false);
      else if (s.loadDone === loadId) resolve(true);
      else return false;
      return true;
    };
    const stop = store.subscribe((root) => {
      if (settle(root.history)) stop();
    });
  });
}

// The calendar never changes the URL, so the address bar is the return target: it already points at the
// meeting (or home) we came from, and leaving pushes no entry.
async function leaveCalendar() {
  const id = meetingIdFromPath();
  let target = "#page-title";
  if (id) {
    const painted = await loadMeeting(id);
    if (!painted) return; // superseded by newer navigation; it owns focus
    target = nav().view === "meeting" ? "#meeting-title" : "#meeting-error-title";
  } else {
    dispatch({ type: "history/home-shown" });
  }
  $(target).focus();
}

// Resolves true when the player was sent to the source, false when the source could not be shown or a newer seek,
// a meeting or transcript change, an audio error or the metadata timeout won (the core decides, see playback-core.js).
async function seekNotesSource(source) {
  const { meetingId, snapshotId, segmentId, startSeconds } = source; // no source at all rejects, as before 4d
  if (!validSource(source)) return false;
  const path = `/meetings/${encodeURIComponent(meetingId)}`;
  const query = `?notes_source=${encodeURIComponent(snapshotId)}&segment=${encodeURIComponent(segmentId)}${source.group ? `&group=${encodeURIComponent(source.group)}` : ""}`;
  if (window.location.pathname !== path || window.location.search !== query) {
    // Opening a source does not change the tab: the new entry keeps the address's hash and the tab shown.
    const state = nav();
    const shown = state.meeting && state.meeting.id === meetingId && state.view === "meeting";
    const hash = window.location.pathname === path ? window.location.hash : "";
    dispatch({ type: "history/url-pushed", url: `${path}${query}${hash}`, tab: shown ? state.tab : null });
  }
  const showing = () => {
    const state = nav();
    return state.meeting && state.meeting.id === meetingId && state.view === "meeting";
  };
  if (!showing()) {
    const painted = await loadMeeting(meetingId);
    if (!painted || !showing()) return false;
  }
  const seekId = newRequestId();
  return new Promise((resolve) => {
    const stop = store.subscribe((root) => {
      const { seek, seekDone } = root.playback;
      if (seekDone && seekDone.ids.includes(seekId)) resolve(seekDone.ok);
      else if (!seek || !seek.ids.includes(seekId)) resolve(false); // replaced by a newer seek or dropped with its meeting
      else return;
      stop();
    });
    dispatch({ type: "playback/seek-requested", seekId, meetingId, startSeconds, ...audioFacts() });
  });
}

// ---- metadata editing ----

function renderEditor(s) {
  const editor = selectEditor(s);
  setProp($("#edit-status"), "textContent", tr(editor.status));
  setProp($("#meeting-edit"), "hidden", !editor.open);
  setProp($("#edit-meeting"), "hidden", editor.open);
  if (!editor.open) {
    $("#edit-date").removeAttribute("aria-describedby");
    setProp($("#edit-conflict"), "hidden", true);
    setProp($("#edit-error"), "textContent", "");
    setProp($("#edit-error"), "hidden", true);
    return;
  }
  setProp($("#edit-title"), "value", editor.title);
  setProp($("#edit-date"), "value", editor.date);
  setProp($("#edit-title"), "disabled", editor.titleDisabled);
  const dateIsLocked = editor.dateDisabled && !s.editor.busy && coreSourceMutationUnavailable(s)
    && (s.editor.date || null) === s.editor.original.meeting_date;
  if (dateIsLocked) $("#edit-date").setAttribute("aria-describedby", "edit-status");
  else $("#edit-date").removeAttribute("aria-describedby");
  setProp($("#edit-date"), "disabled", editor.dateDisabled);
  setProp($("#edit-clear-date"), "disabled", editor.clearDisabled);
  setProp($("#edit-save"), "disabled", editor.saveDisabled);
  setProp($("#edit-cancel"), "disabled", editor.cancelDisabled);
  setProp($("#edit-save"), "textContent", tr(editor.saveLabel));
  setProp($("#edit-conflict"), "textContent", tr(editor.conflict));
  setProp($("#edit-conflict"), "hidden", !editor.conflict);
  const error = tr(editor.error);
  setProp($("#edit-error"), "textContent", error);
  setProp($("#edit-error"), "hidden", !error);
}

// ---- meeting deletion ----

function renderDeletion(s) {
  const view = selectDeletion(s);
  if (view.header) {
    const status = $("#meeting-deletion-status");
    setProp(status, "hidden", view.header.hidden);
    status.dataset.state = view.header.state;
    setProp($("#delete-meeting"), "disabled", view.header.deleteDisabled);
    setProp($("#delete-meeting"), "textContent", tr(view.header.deleteLabel));
    setProp($("#meeting-deletion-message"), "textContent", tr(view.header.text));
  }
  const dialog = $("#meeting-delete-dialog");
  setProp($("#meeting-delete-name"), "textContent", view.dialog.name);
  setProp($("#meeting-delete-confirmation"), "value", view.dialog.confirmation);
  const error = tr(view.dialog.error);
  setProp($("#meeting-delete-error"), "textContent", error);
  setProp($("#meeting-delete-error"), "hidden", !error);
  setProp($("#meeting-delete-confirm"), "textContent", tr(view.dialog.confirmLabel));
  setProp($("#meeting-delete-confirm"), "disabled", view.dialog.confirmDisabled);
  if (view.dialog.open && !dialog.open) dialog.showModal();
  else if (!view.dialog.open && dialog.open) dialog.close();
}


// ---- task and transcript (phases 4b and 4c): the DOM they paint ----

const TASK_STAGES = {
  accepted: t("Đã nhận"),
  starting: t("Đang bắt đầu"),
  starting_runner: t("Khởi chạy runner"),
  loading_model: t("Nạp model local"),
  transcribing: t("Đang chép lời"),
  starting_diarization: t("Chuẩn bị nhận dạng người nói"),
  loading_diarization_model: t("Nạp Community-1 local"),
  decoding_input: t("Đọc bản ghi local"),
  diarizing: t("Đang nhận dạng người nói"),
  publishing: t("Lưu transcript hoàn chỉnh"),
  stopping: t("Đang dừng runner"),
  complete: t("Hoàn tất transcript"),
  failed: t("Thất bại"),
  canceled: t("Đã hủy"),
  interrupted: t("Bị gián đoạn"),
};
// Lines that stand in for the status text while a notice is up (see task-core.js `notice`).
const NOTICE_TEXT = {
  "poll-lost": t("Tạm mất kết nối khi đọc tiến độ. Tác vụ vẫn thuộc worker; đang thử đọc lại."),
  "refresh-lost": t("Tác vụ đã hoàn tất; không tải được transcript mới. Mở lại cuộc họp để thử lại."),
  "session-ended": t("Phiên làm việc đã hết. Đang chuyển đến trang đăng nhập."),
};

function formatElapsedSeconds(value) {
  const seconds = Math.max(0, Number(value) || 0);
  return t("{n} giây", { n: seconds.toFixed(1) });
}

function formatTimestamp(secondsValue) {
  const milliseconds = Math.max(0, Math.round((Number(secondsValue) || 0) * 1000));
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1000);
  const fraction = Math.floor((milliseconds % 1000) / 10);
  const clock = hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
  return `${clock}.${String(fraction).padStart(2, "0")}`;
}

function renderTaskError(view) {
  const container = $("#transcription-error");
  clearReloadLatestAction(container);
  setProp(container, "hidden", !view.error.visible);
  setProp($("#transcription-error-message"), "textContent", tr(view.error.message));
  const diagnostic = view.error.diagnostic;
  // Written only when different: the command stays selected after "Lệnh đã được chọn".
  setProp($("#task-diagnostic-details"), "hidden", !diagnostic);
  setProp($("#task-diagnostic-command-wrap"), "hidden", !diagnostic);
  setProp($("#task-diagnostic-code"), "textContent", diagnostic ? diagnostic.code : $("#task-diagnostic-code").textContent);
  setProp($("#task-diagnostic-id"), "textContent", diagnostic ? diagnostic.id : $("#task-diagnostic-id").textContent);
  setProp($("#task-diagnostic-command"), "value", diagnostic ? diagnostic.command : "");
  setProp($("#task-diagnostic-copy-status"), "textContent", COPY_STATUS[view.copy] ?? "");
}

const COPY_STATUS = {
  copied: t("Đã sao chép lệnh."),
  selected: t("Lệnh đã được chọn. Nhấn Ctrl+C hoặc ⌘C để sao chép."),
};

// How a run that did not finish reads alone, and after "lần chạy mới" / "lần tạo mới".
const RUN_END_LABELS = { failed: t("Thất bại"), canceled: t("Đã hủy"), interrupted: t("Bị gián đoạn") };
const NEW_RUN_END_WORDS = { failed: t("thất bại"), canceled: t("bị hủy"), interrupted: t("bị gián đoạn") };

function setStatusItem(id, name, value, itemState) {
  const item = $(`#status-${id}`);
  item.dataset.state = itemState; // idle | active | done | warning | problem: only repeats the words
  item.lastElementChild.textContent = `${name} · ${value}`;
}

// The header strip: one short line per stage. The words carry the state; the dot only repeats it.
function renderMeetingProcessingStatus(task, generation) {
  const meeting = nav().meeting;
  const seconds = Number(meeting && meeting.recording && meeting.recording.duration_seconds);
  setStatusItem(
    "recording",
    t("Bản ghi"),
    seconds > 0 ? t("Đã lưu · {duration}", { duration: tr(formatRecordingDuration(seconds)) }) : t("Đã lưu"),
    "done",
  );

  const transcriptEnd = task && Object.hasOwn(RUN_END_LABELS, task.status) ? task.status : null;
  if (task && ACTIVE_TASK_STATES.includes(task.status)) {
    setStatusItem("transcript", "Transcript", task.status === "stopping" ? t("Đang dừng") : t("Đang chép lời"), "active");
  } else if (generation) {
    setStatusItem(
      "transcript",
      "Transcript",
      transcriptEnd ? t("Hoàn tất · lần chạy mới {result}", { result: NEW_RUN_END_WORDS[transcriptEnd] }) : t("Hoàn tất"),
      transcriptEnd ? "warning" : "done",
    );
  } else if (transcriptEnd) {
    setStatusItem("transcript", "Transcript", RUN_END_LABELS[transcriptEnd], transcriptEnd === "failed" ? "problem" : "warning");
  } else {
    setStatusItem("transcript", "Transcript", t("Chưa chép lời"), "idle");
  }

  const notesEnd = Object.hasOwn(RUN_END_LABELS, nav().notes.status) ? nav().notes.status : null;
  if (!nav().notes.known) {
    setStatusItem("notes", "Notes", t("Đang kiểm tra"), "idle");
  } else if (ACTIVE_TASK_STATES.includes(nav().notes.status)) {
    setStatusItem("notes", "Notes", nav().notes.status === "stopping" ? t("Đang dừng") : t("Đang tạo"), "active");
  } else if (nav().notes.count > 0) {
    setStatusItem(
      "notes",
      "Notes",
      t("{count} bản", { count: nav().notes.count })
        + (notesEnd ? t(" · lần tạo mới {result}", { result: NEW_RUN_END_WORDS[notesEnd] }) : ""),
      notesEnd ? "warning" : "done",
    );
  } else if (notesEnd) {
    setStatusItem("notes", "Notes", RUN_END_LABELS[notesEnd], notesEnd === "failed" ? "problem" : "warning");
  } else {
    setStatusItem("notes", "Notes", t("Chưa có"), "idle");
  }
}

// The backend names a speaker nobody renamed "Speaker 3"; its chip invites a name.
const DEFAULT_SPEAKER_NAME = /^Speaker \d+$/;

// The default names a transcript still carries, for the notes reminder to name them.
function defaultSpeakerNames(generation) {
  const speakers = generation && Array.isArray(generation.speakers) ? generation.speakers : [];
  return speakers.map((speaker) => speaker.display_name || "").filter((name) => DEFAULT_SPEAKER_NAME.test(name));
}

// Open the Transcript tab at the current transcript's speaker catalog, ready to name the speakers.
function focusSpeakerCatalog() {
  if (nav().view !== "meeting") return;
  selectTab("transcript");
  const transcription = nav().meeting && nav().meeting.transcription;
  const current = transcription && transcription.generation;
  // Only the current transcript takes names, so a past one on screen gives way to it.
  if (current && shownGeneration().selected !== current.id) {
    const selector = $("#generation-selector");
    selector.value = current.id;
    selector.dispatchEvent(new Event("change", { bubbles: true }));
  }
  $("#speaker-catalog-title").focus();
}

function removeTranscriptDraftReview(generationId, field, itemId) {
  for (const review of document.querySelectorAll(".transcript-draft-review")) {
    if (
      review.dataset.generationId === generationId
      && review.dataset.draftField === field
      && review.dataset.draftItemId === itemId
    ) review.remove();
  }
}

function updateTranscriptDraftReview(generationId, field, itemId, draft, latestValue) {
  for (const review of document.querySelectorAll(".transcript-draft-review")) {
    if (
      review.dataset.generationId !== generationId
      || review.dataset.draftField !== field
      || review.dataset.draftItemId !== itemId
    ) continue;
    const latest = review.querySelector(".transcript-latest-value");
    if (latest && latestValue !== undefined) {
      const generation = generationsFor(nav().meeting && nav().meeting.transcription)
        .find((item) => item.id === generationId);
      latest.textContent = t("Bản mới nhất đã lưu: {value}", { value: draftReviewValue(field, latestValue, generation) });
    }
    const value = draftReviewValue(field, draft.value);
    review.querySelector(".transcript-draft-value").textContent = t("Bản nháp chưa lưu: {value}", { value });
  }
}

function transcriptSpeakerLabel(generation, speakerId) {
  if (!speakerId) return t("Chưa xác định");
  const speaker = generation && generation.speakers.find((item) => item.speaker_id === speakerId);
  return speaker && speaker.display_name ? speaker.display_name : speakerId;
}

function draftReviewValue(field, value, generation = null) {
  if (field === "speakerName" && value === "") return t("Khôi phục tên mặc định");
  if (field === "speakerAssignment") return transcriptSpeakerLabel(generation, value);
  return value;
}

function appendReloadLatestButton(container, generationId) {
  let button = container.querySelector("[data-reload-latest]");
  if (!button) {
    button = document.createElement("button");
    button.className = "secondary-button transcript-reload-latest";
    button.type = "button";
    button.dataset.reloadLatest = "true";
    button.dataset.transcriptMutation = "true";
    button.textContent = t("Tải lại bản mới nhất");
    button.disabled = Boolean(tstate().mutation || tstate().reload);
    container.append(button);
  }
  button.dataset.generationId = generationId;
  let status = container.querySelector(".transcript-reload-status");
  if (!status) {
    status = document.createElement("span");
    status.className = "transcript-reload-status";
    status.setAttribute("role", "status");
    container.append(status);
  }
  return button;
}

function createTranscriptDraftReview(generationId, field, itemId, value, latestValue) {
  const review = document.createElement("div");
  review.className = "transcript-draft-review";
  review.dataset.generationId = generationId;
  review.dataset.draftField = field;
  review.dataset.draftItemId = itemId;
  if (latestValue !== undefined) {
    const latest = document.createElement("p");
    latest.className = "transcript-latest-value";
    const generation = generationsFor(nav().meeting && nav().meeting.transcription)
      .find((item) => item.id === generationId);
    latest.textContent = t("Bản mới nhất đã lưu: {value}", { value: draftReviewValue(field, latestValue, generation) });
    review.append(latest);
  }
  const draft = document.createElement("p");
  draft.className = "transcript-draft-value";
  const generation = generationsFor(nav().meeting && nav().meeting.transcription)
    .find((item) => item.id === generationId);
  const reviewValue = draftReviewValue(field, value, generation);
  draft.textContent = t("Bản nháp chưa lưu: {value}", { value: reviewValue });
  const copy = document.createElement("button");
  copy.className = "text-button transcript-copy-draft";
  copy.type = "button";
  copy.dataset.copyTranscriptDraft = "true";
  copy.dataset.generationId = generationId;
  copy.dataset.draftField = field;
  copy.dataset.draftItemId = itemId;
  copy.textContent = field === "speakerAssignment" ? t("Xem transcript này") : t("Sao chép bản nháp");
  if (field === "speakerAssignment") copy.dataset.openTranscriptDraft = "true";
  const status = document.createElement("span");
  status.className = "transcript-draft-copy-status";
  status.setAttribute("role", "status");
  review.append(draft, copy, status);
  return review;
}

function renderTranscriptDraftRecovery(transcription, shownId) {
  const panel = $("#transcript-draft-recovery");
  const generations = generationsFor(transcription);
  const entries = [];
  for (const [generationId, bucket] of Object.entries(tstate().drafts)) {
    const generation = generations.find((item) => item.id === generationId);
    if (!generation) continue;
    if (generationId === shownId) {
      for (const draft of Object.values(bucket.speakerAddName)) {
        entries.push({
          generationId,
          generationNumber: generation.generation_number,
          field: "speakerAddName",
          value: draft.value,
          conflictCode: draft.conflictCode || "",
        });
      }
      continue;
    }
    for (const [segmentId, draft] of Object.entries(bucket.segmentText)) {
      const segment = generation.segments.find((item) => item.id === segmentId);
      if (segment) entries.push({
        generationId,
        generationNumber: generation.generation_number,
        field: "segmentText",
        itemId: segmentId,
        value: draft.value,
        conflictCode: draft.conflictCode || "",
        latestValue: segment.text,
        startSeconds: segment.start_seconds,
      });
    }
    for (const draft of Object.values(bucket.speakerAddName)) {
      entries.push({
        generationId,
        generationNumber: generation.generation_number,
        field: "speakerAddName",
        value: draft.value,
        conflictCode: draft.conflictCode || "",
      });
    }
    for (const [speakerId, draft] of Object.entries(bucket.speakerName)) {
      const speaker = generation.speakers.find((item) => item.speaker_id === speakerId);
      if (speaker) entries.push({
        generationId,
        generationNumber: generation.generation_number,
        field: "speakerName",
        itemId: speakerId,
        value: draft.value,
        conflictCode: draft.conflictCode || "",
        latestValue: speaker.display_name,
      });
    }
    for (const [segmentId, draft] of Object.entries(bucket.speakerAssignment)) {
      const segment = generation.segments.find((item) => item.id === segmentId);
      if (segment) entries.push({
        generationId,
        generationNumber: generation.generation_number,
        field: "speakerAssignment",
        itemId: segmentId,
        value: draft.value,
        conflictCode: draft.conflictCode || "",
        latestValue: segment.speaker_id || "",
        startSeconds: segment.start_seconds,
      });
    }
  }
  const renderKey = JSON.stringify(entries);
  panel.hidden = entries.length === 0;
  if (panel.dataset.renderKey === renderKey) return;
  panel.dataset.renderKey = renderKey;
  panel.replaceChildren();
  const sections = new Map();
  for (const entry of entries) {
    let section = sections.get(entry.generationId);
    if (!section) {
      section = document.createElement("section");
      section.className = "transcript-draft-generation";
      const heading = document.createElement("h5");
      heading.textContent = t("Bản nháp chưa lưu · Transcript #{number}", { number: entry.generationNumber });
      section.append(heading);
      sections.set(entry.generationId, section);
      panel.append(section);
    }
    if (entry.field === "segmentText") {
      const context = document.createElement("p");
      context.className = "transcript-draft-context";
      context.textContent = t("Đoạn bắt đầu tại {time}", { time: formatTimestamp(entry.startSeconds) });
      section.append(
        context,
        createTranscriptDraftReview(
          entry.generationId,
          "segmentText",
          entry.itemId,
          entry.value,
          entry.latestValue,
        ),
      );
    } else if (entry.field === "speakerName") {
      const context = document.createElement("p");
      context.className = "transcript-draft-context";
      context.textContent = t("Tên người nói · {id}", { id: entry.itemId });
      section.append(
        context,
        createTranscriptDraftReview(
          entry.generationId,
          "speakerName",
          entry.itemId,
          entry.value,
          entry.latestValue,
        ),
      );
    } else if (entry.field === "speakerAssignment") {
      const context = document.createElement("p");
      context.className = "transcript-draft-context";
      context.textContent = t("Người nói · đoạn bắt đầu tại {time}", { time: formatTimestamp(entry.startSeconds) });
      section.append(
        context,
        createTranscriptDraftReview(
          entry.generationId,
          "speakerAssignment",
          entry.itemId,
          entry.value,
          entry.latestValue,
        ),
      );
    } else {
      const context = document.createElement("p");
      context.className = "transcript-draft-context";
      context.textContent = t("Tên người nói chưa được thêm");
      const review = document.createElement("div");
      review.className = "transcript-draft-review transcript-speaker-add-draft-review";
      review.dataset.generationId = entry.generationId;
      review.dataset.draftField = "speakerAddName";
      const value = document.createElement("p");
      value.className = "transcript-draft-value";
      value.textContent = t("Tên cần thêm: {name}", { name: entry.value || t("(để trống; máy chủ sẽ đặt tên mặc định)") });
      const apply = document.createElement("button");
      apply.type = "button";
      apply.className = "secondary-button transcript-reapply-speaker-add";
      apply.dataset.reapplySpeakerAddDraft = "true";
      apply.dataset.generationId = entry.generationId;
      apply.dataset.transcriptMutation = "true";
      const current = transcription && transcription.generation;
      const draft = transcriptDraft(entry.generationId, "speakerAddName", "speaker-add-name");
      apply.disabled = !speakerAddRecoveryReady(current, draft);
      apply.textContent = t("Thêm vào transcript hiện tại");
      const status = document.createElement("span");
      status.className = "transcript-speaker-add-draft-status";
      status.setAttribute("role", "status");
      review.append(value, apply, status);
      section.append(context, review);
    }
  }
}

// The selector and the generation it names. The core decides which one that is (a new current transcript takes the
// selection from the old one); this paints it.
function renderGenerationNavigation(transcription) {
  const current = transcription && transcription.generation;
  const { generations, selected } = navigation(tstate(), transcription);
  const wrapper = $("#generation-navigation");
  const selector = $("#generation-selector");
  if (!generations.length) {
    wrapper.hidden = true;
    return null;
  }
  const optionsKey = generations.map((generation) => generation.id).join(":");
  if (selector.dataset.optionsKey !== optionsKey) {
    selector.replaceChildren();
    for (const generation of generations) {
      const option = document.createElement("option");
      option.value = generation.id;
      option.textContent = `Transcript #${generation.generation_number}`
        + (generation.model_name ? ` · ${generation.model_name}` : "")
        + (current && current.id === generation.id ? t(" · hiện tại") : t(" · lịch sử"));
      selector.append(option);
    }
    selector.dataset.optionsKey = optionsKey;
  }
  wrapper.hidden = generations.length < 2;
  selector.value = selected;
  return {
    generation: generations.find((generation) => generation.id === selected),
    editable: Boolean(current && current.id === selected && current.status === "complete"),
  };
}

function renderSpeakerCatalog(generation, editable) {
  const list = $("#speaker-catalog-list");
  const speakers = Array.isArray(generation.speakers) ? generation.speakers : [];
  list.replaceChildren();
  $("#speaker-add-form").hidden = !editable;
  $("#speaker-add-status").textContent = "";
  $("#speaker-add-status").classList.remove("is-error");
  for (const speaker of speakers) list.append(buildSpeakerItem(generation, speaker, editable));
}

function speakerChipMark(text) {
  const mark = document.createElement("span");
  mark.className = "speaker-chip-mark";
  mark.textContent = text;
  return mark;
}

function buildSpeakerChip(generation, speaker, draft) {
  const chip = document.createElement("button");
  chip.className = "speaker-chip";
  chip.type = "button";
  chip.title = t("Đổi tên người nói");
  chip.dataset.speakerChip = "true";
  chip.dataset.speakerId = speaker.speaker_id;
  chip.dataset.generationId = generation.id;
  chip.dataset.transcriptMutation = "true";
  const name = document.createElement("span");
  name.className = "speaker-chip-name";
  name.textContent = speaker.display_name || speaker.speaker_id;
  chip.append(name);
  if (DEFAULT_SPEAKER_NAME.test(speaker.display_name || "")) chip.append(speakerChipMark(t("đặt tên")));
  if (draft) chip.append(speakerChipMark(t("bản nháp")));
  return chip;
}

function buildSpeakerRenameForm(generation, speaker, draft) {
  const form = document.createElement("form");
  form.className = "speaker-name-form";
  form.dataset.speakerName = "true";
  form.dataset.speakerId = speaker.speaker_id;
  form.dataset.generationId = generation.id;
  const label = document.createElement("label");
  label.textContent = t("Tên người nói · {id}", { id: speaker.speaker_id });
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 80;
  input.autocomplete = "off";
  input.value = draft ? draft.value : speaker.display_name || "";
  input.setAttribute("aria-label", t("Tên hiển thị cho {id}", { id: speaker.speaker_id }));
  input.dataset.transcriptMutation = "true";
  label.append(input);
  const save = document.createElement("button");
  save.className = "secondary-button";
  save.type = "submit";
  save.dataset.transcriptMutation = "true";
  save.textContent = t("Lưu tên");
  const cancel = document.createElement("button");
  cancel.className = "secondary-button";
  cancel.type = "button";
  cancel.dataset.editCancel = "true";
  cancel.dataset.speakerCancel = "true";
  cancel.setAttribute("aria-keyshortcuts", "Escape");
  cancel.textContent = t("Hủy");
  const clear = document.createElement("button");
  clear.className = "text-button speaker-clear-name";
  clear.type = "button";
  clear.dataset.clearSpeakerName = "true";
  clear.dataset.speakerId = speaker.speaker_id;
  clear.dataset.generationId = generation.id;
  clear.dataset.transcriptMutation = "true";
  clear.textContent = t("Khôi phục tên mặc định");
  const status = document.createElement("p");
  status.className = "speaker-form-status";
  status.dataset.speakerStatus = speaker.speaker_id;
  status.setAttribute("role", "status");
  form.append(label, save, cancel, clear, status);
  // Reopened after a conflict, the form offers the reload where the conflict first showed it.
  if (awaitsLatestReload(draft, speaker.display_name)) appendReloadLatestButton(form, generation.id);
  if (draft && draft.conflictCode) {
    form.append(
      createTranscriptDraftReview(
        generation.id,
        "speakerName",
        speaker.speaker_id,
        draft.value,
        speaker.display_name || speaker.speaker_id,
      ),
    );
  }
  return form;
}

// A chip in the current transcript opens an inline rename; one in a past transcript only reads.
function buildSpeakerItem(generation, speaker, editable) {
  const item = document.createElement("li");
  item.className = "speaker-chip-item";
  item.dataset.speakerId = speaker.speaker_id;
  const draft = transcriptDraft(generation.id, "speakerName", speaker.speaker_id);
  if (!editable) {
    const name = document.createElement("span");
    name.className = "speaker-chip speaker-name-readonly";
    name.textContent = speaker.display_name || speaker.speaker_id;
    item.append(name);
    if (draft) {
      item.append(
        createTranscriptDraftReview(generation.id, "speakerName", speaker.speaker_id, draft.value),
      );
    }
  } else if (isRenamingSpeaker(generation.id, speaker.speaker_id)) {
    item.classList.add("is-renaming");
    item.append(buildSpeakerRenameForm(generation, speaker, draft));
  } else {
    item.append(buildSpeakerChip(generation, speaker, draft));
  }
  return item;
}

function speakerItem(speakerId) {
  return [...$("#speaker-catalog-list").children].find((item) => item.dataset.speakerId === speakerId) || null;
}

// Swap one speaker between chip and rename form; the other speakers stay untouched.
function replaceSpeakerItem(generationId, speakerId) {
  const { generation, editable } = shownGeneration();
  if (!generation || generation.id !== generationId) return null;
  const speaker = generation.speakers.find((item) => item.speaker_id === speakerId);
  const current = speakerItem(speakerId);
  if (!speaker || !current) return null;
  const item = buildSpeakerItem(generation, speaker, editable);
  current.replaceWith(item);
  syncTranscriptMutationControls();
  return item;
}

function renderTranscriptGeneration(generation, editable) {
  const panel = $("#transcript-generation");
  const list = $("#transcript-segments");
  panel.hidden = !generation;
  if (!generation) {
    list.replaceChildren();
    $("#speaker-catalog-list").replaceChildren();
    delete panel.dataset.renderKey;
    return;
  }
  // Lock changes affect controls and the mode label, not transcript content.
  // Keep the content key consistent with incremental save reconciliation.
  const renderKey = `${generation.id}:${generation.source_version}:${editable}`;
  const mode = $("#generation-mode");
  mode.textContent = editable && !nav().notes.lockKnown
    ? t("Bản hiện tại · Đang kiểm tra khóa nguồn trước khi cho phép chỉnh sửa.")
    : editable && nav().notes.locked
      ? t("Bản hiện tại · Tác vụ notes đang dùng nguồn; chỉnh sửa tạm thời bị khóa.")
      : editable
        ? t("Bản hiện tại · Bạn có thể chỉnh sửa transcript và người nói.")
        : t("Bản lịch sử · Chỉ đọc.");
  mode.classList.toggle("is-readonly", !editable || sourceMutationUnavailable());
  if (panel.dataset.renderKey === renderKey) {
    updateActiveTranscriptIntervals();
    return;
  }
  list.replaceChildren();
  panel.dataset.renderKey = renderKey;

  $("#generation-title").textContent = `Transcript #${generation.generation_number}`;
  const meta = [
    t("Gợi ý: {language}", { language: tr(LANGUAGES[generation.language_hint]) || generation.language_hint }),
    generation.model_name,
    // Older transcripts carry no runtime when their task row is gone.
    generation.device ? tr(selectRuntime(generation).backend) : null,
    t("{count} đoạn", { count: generation.segment_count }),
  ].filter(Boolean);
  if (typeof generation.language_probability === "number") {
    meta.push(`language_probability ${generation.language_probability.toFixed(3)}`);
  }
  const segments = Array.isArray(generation.segments) ? generation.segments : [];
  // No confidence score: the review count cannot catch a misheard word, so it does not compare across models.
  if (segments.length && segments.every((segment) => segment.avg_logprob === null)) {
    meta.push(t("“cần kiểm tra” không xét độ tin cậy của chữ"));
  }
  if (generation.diarization_model_name) meta.push(`Diarization · ${generation.diarization_model_name}`);
  $("#generation-meta").textContent = meta.join(" · ");
  renderSpeakerCatalog(generation, editable);

  $("#transcript-empty").hidden = segments.length > 0;
  for (const segment of segments) list.append(buildSegmentRow(generation, segment, editable));
  if (tstate().mutation) syncTranscriptMutationControls();
  updateActiveTranscriptIntervals();
}

function buildOriginalSpeaker(segment, speakerNames) {
  const originalSpeaker = document.createElement("span");
  originalSpeaker.className = "segment-original-speaker";
  originalSpeaker.dataset.originalSpeakerId = segment.original_speaker_id || "";
  originalSpeaker.textContent = t("Gán ban đầu: {speaker}", {
    speaker: segment.original_speaker_id
      ? speakerNames.get(segment.original_speaker_id) || segment.original_speaker_id
      : t("Chưa xác định"),
  });
  return originalSpeaker;
}

// A segment that shows the review cue is one the Transcript tab counts as "cần kiểm tra".
function needsReview(segment) {
  return Boolean(segment.uncertain) || (Array.isArray(segment.review_reasons) && segment.review_reasons.length > 0);
}

function buildReviewCue(segment) {
  if (!needsReview(segment)) return null;
  const reasons = Array.isArray(segment.review_reasons) ? segment.review_reasons : [];
  const reviewCue = document.createElement("span");
  reviewCue.className = "segment-review-cue";
  reviewCue.textContent = reasons.length
    ? t("Cần kiểm tra · {reasons}", { reasons: reasons.map((reason) => REVIEW_REASON_LABELS[reason] || reason).join(" · ") })
    : t("Nên nghe lại");
  return reviewCue;
}

function buildModelMeasures(segment) {
  const measures = [];
  if (typeof segment.avg_logprob === "number") measures.push(`avg_logprob ${segment.avg_logprob.toFixed(3)}`);
  if (typeof segment.no_speech_prob === "number") measures.push(`no_speech_prob ${segment.no_speech_prob.toFixed(3)}`);
  if (typeof segment.compression_ratio === "number") measures.push(`compression_ratio ${segment.compression_ratio.toFixed(3)}`);
  if (!measures.length) return null;
  const modelMeasures = document.createElement("span");
  modelMeasures.className = "segment-model-measures";
  modelMeasures.textContent = t("Chỉ số model, không phải độ chính xác đã hiệu chuẩn: {measures}", { measures: measures.join(" · ") });
  return modelMeasures;
}

// The origin and the model's own measures sit behind "Chi tiết": in the open editor, or, with no
// editor to hold them, in a past transcript's rows.
function buildSegmentDetails(segment, speakerNames) {
  const details = document.createElement("details");
  details.className = "segment-details";
  const summary = document.createElement("summary");
  summary.textContent = t("Chi tiết");
  details.append(summary, buildOriginalSpeaker(segment, speakerNames));
  const modelMeasures = buildModelMeasures(segment);
  if (modelMeasures) details.append(modelMeasures);
  return details;
}

function buildEditButton(segment, hasDraft) {
  const button = document.createElement("button");
  button.className = "secondary-button segment-edit-button";
  button.type = "button";
  button.dataset.editSegment = "true";
  button.dataset.transcriptMutation = "true";
  button.textContent = t("Sửa");
  button.setAttribute(
    "aria-label",
    t("Sửa đoạn bắt đầu tại {time}", { time: formatTimestamp(segment.start_seconds) })
      + (hasDraft ? t(" (có bản nháp chưa lưu)") : ""),
  );
  return button;
}

function buildSpeakerControl(generation, segment, assignmentDraft) {
  const speakers = Array.isArray(generation.speakers) ? generation.speakers : [];
  const speakerControl = document.createElement("div");
  speakerControl.className = "segment-speaker-control";
  const speakerLabel = document.createElement("span");
  speakerLabel.textContent = t("Người nói");
  const speakerSelect = document.createElement("select");
  speakerSelect.className = "segment-speaker-select";
  speakerSelect.dataset.speakerAssignment = "true";
  speakerSelect.dataset.segmentId = segment.id;
  speakerSelect.dataset.currentSpeaker = segment.speaker_id || "";
  speakerSelect.dataset.generationId = generation.id;
  speakerSelect.dataset.assignmentVersion = String(segment.assignment_version || 1);
  speakerSelect.dataset.transcriptMutation = "true";
  speakerSelect.setAttribute(
    "aria-label",
    t("Người nói cho đoạn bắt đầu tại {time}", { time: formatTimestamp(segment.start_seconds) }),
  );
  const unidentified = document.createElement("option");
  unidentified.value = "";
  unidentified.textContent = t("Chưa xác định");
  speakerSelect.append(unidentified);
  for (const speaker of speakers) {
    const option = document.createElement("option");
    option.value = speaker.speaker_id;
    option.textContent = speaker.display_name || speaker.speaker_id;
    speakerSelect.append(option);
  }
  speakerSelect.value = assignmentDraft ? assignmentDraft.value : segment.speaker_id || "";
  speakerControl.append(speakerLabel, speakerSelect);
  return speakerControl;
}

function buildSegmentEditForm(generation, segment, draft) {
  const form = document.createElement("form");
  form.className = "segment-edit-form";
  form.dataset.segmentText = "true";
  form.dataset.segmentId = segment.id;
  form.dataset.generationId = generation.id;
  const editor = document.createElement("textarea");
  editor.className = "segment-text-editor";
  editor.maxLength = 10000;
  editor.rows = 3;
  editor.value = draft ? draft.value : segment.text;
  editor.setAttribute(
    "aria-label",
    t("Nội dung transcript cho đoạn bắt đầu tại {time}", { time: formatTimestamp(segment.start_seconds) }),
  );
  editor.dataset.transcriptMutation = "true";
  const actions = document.createElement("div");
  actions.className = "segment-edit-actions";
  const save = document.createElement("button");
  save.className = "secondary-button";
  save.type = "submit";
  save.dataset.transcriptMutation = "true";
  save.setAttribute("aria-keyshortcuts", "Control+Enter Meta+Enter");
  save.title = "Ctrl/⌘+Enter";
  save.textContent = t("Lưu đoạn");
  const cancel = document.createElement("button");
  cancel.className = "secondary-button";
  cancel.type = "button";
  cancel.dataset.editCancel = "true";
  cancel.dataset.segmentCancel = "true";
  cancel.setAttribute("aria-keyshortcuts", "Escape");
  cancel.title = "Esc";
  cancel.textContent = t("Hủy");
  // Plain text, no role: every save is announced once, by the shared #transcript-mutation-status.
  const status = document.createElement("p");
  status.className = "segment-edit-status";
  actions.append(save, cancel, status);
  // Reopened after a conflict, the editor offers the reload where the conflict first showed it.
  if (awaitsLatestReload(draft, segment.text)) appendReloadLatestButton(actions, generation.id);
  form.append(editor, actions);
  return form;
}

// Read mode shows what a reader needs; the editor and speaker select exist only while a segment
// is being edited. A past transcript has no editor: its rows stay read-only.
function buildSegmentRow(generation, segment, editable) {
  const speakerNames = new Map(
    (Array.isArray(generation.speakers) ? generation.speakers : [])
      .map((speaker) => [speaker.speaker_id, speaker.display_name]),
  );
  const editing = editable && isEditingSegment(generation.id, segment.id);
  const textDraft = transcriptDraft(generation.id, "segmentText", segment.id);
  const assignmentDraft = transcriptDraft(generation.id, "speakerAssignment", segment.id);
  const hasDraft = hasSegmentDraft(generation.id, segment.id);

  const item = document.createElement("li");
  item.className = editing ? "transcript-segment is-editing" : "transcript-segment";
  item.dataset.segmentId = segment.id;
  item.dataset.startSeconds = String(segment.start_seconds);
  item.dataset.endSeconds = String(segment.end_seconds);

  const timestamp = document.createElement("button");
  timestamp.className = "segment-timestamp";
  timestamp.type = "button";
  timestamp.dataset.seekSeconds = String(segment.start_seconds);
  timestamp.textContent = formatTimestamp(segment.start_seconds);
  timestamp.setAttribute(
    "aria-label",
    t("Phát bản ghi từ {time}", { time: formatTimestamp(segment.start_seconds) }),
  );

  const head = document.createElement("div");
  head.className = "segment-head";
  if (editing) {
    head.append(buildSpeakerControl(generation, segment, assignmentDraft));
  } else {
    const speakerName = document.createElement("span");
    speakerName.className = "segment-speaker";
    speakerName.dataset.speakerId = segment.speaker_id || "";
    speakerName.textContent = segment.speaker_id
      ? speakerNames.get(segment.speaker_id) || segment.speaker_id
      : t("Chưa xác định");
    head.append(speakerName);
    if (editable) head.append(buildEditButton(segment, hasDraft));
  }
  item.append(timestamp, head);
  if (assignmentDraft && (editing || !editable)) {
    item.append(createAssignmentDraftReview(generation, segment, assignmentDraft, editable));
  }

  if (editing) {
    item.append(buildSegmentEditForm(generation, segment, textDraft));
    if (textDraft && textDraft.conflictCode) {
      item.append(
        createTranscriptDraftReview(
          generation.id,
          "segmentText",
          segment.id,
          textDraft.value,
          segment.text,
        ),
      );
    }
  } else {
    const text = document.createElement("p");
    text.className = "segment-text";
    text.textContent = segment.text;
    item.append(text);
    if (!editable && textDraft) {
      item.append(
        createTranscriptDraftReview(generation.id, "segmentText", segment.id, textDraft.value),
      );
    }
  }

  const reviewCue = buildReviewCue(segment);
  if (reviewCue) item.append(reviewCue);
  if (editing || !editable) {
    item.append(buildSegmentDetails(segment, speakerNames));
  } else if (hasDraft) {
    const draftMark = document.createElement("span");
    draftMark.className = "segment-draft-mark";
    draftMark.textContent = t("Bản nháp chưa lưu");
    item.append(draftMark);
  }
  return item;
}

function segmentRow(segmentId) {
  return [...$("#transcript-segments").children].find((item) => item.dataset.segmentId === segmentId) || null;
}

// Swap one row between read mode and editor; every other row stays as it is.
function replaceSegmentRow(generationId, segmentId) {
  const { generation, editable } = shownGeneration();
  if (!generation || generation.id !== generationId) return null;
  const segment = generation.segments.find((item) => item.id === segmentId);
  const current = segmentRow(segmentId);
  if (!segment || !current) return null;
  const row = buildSegmentRow(generation, segment, editable);
  current.replaceWith(row);
  syncTranscriptMutationControls();
  updateActiveTranscriptIntervals();
  return row;
}

function createAssignmentDraftReview(generation, segment, draft, editable) {
  const review = document.createElement("div");
  review.className = "transcript-draft-review transcript-assignment-draft-review";
  review.dataset.generationId = generation.id;
  review.dataset.draftField = "speakerAssignment";
  review.dataset.draftItemId = segment.id;
  const latest = document.createElement("p");
  latest.className = "transcript-latest-value";
  latest.textContent = t("Người nói đã lưu: {speaker}", { speaker: transcriptSpeakerLabel(generation, segment.speaker_id || "") });
  const value = document.createElement("p");
  value.className = "transcript-draft-value";
  value.textContent = t("Lựa chọn cần xem lại: {speaker}", { speaker: transcriptSpeakerLabel(generation, draft.value) });
  review.append(latest, value);
  if (editable) {
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "secondary-button transcript-reapply-assignment";
    apply.dataset.reapplyAssignmentDraft = "true";
    apply.dataset.generationId = generation.id;
    apply.dataset.segmentId = segment.id;
    apply.dataset.transcriptMutation = "true";
    apply.disabled = Boolean(draft.conflictCode) || Boolean(tstate().mutation);
    apply.textContent = t("Áp dụng lại lựa chọn");
    const status = document.createElement("span");
    status.className = "transcript-assignment-draft-status";
    status.setAttribute("role", "status");
    review.append(apply, status);
  }
  return review;
}

function updateAssignmentDraftReviewLabels(generation) {
  for (const review of document.querySelectorAll(".transcript-assignment-draft-review")) {
    if (review.dataset.generationId !== generation.id) continue;
    const segmentId = review.dataset.draftItemId;
    const segment = generation.segments.find((item) => item.id === segmentId);
    const draft = transcriptDraft(generation.id, "speakerAssignment", segmentId);
    if (!segment || !draft) continue;
    review.querySelector(".transcript-latest-value").textContent =
      t("Người nói đã lưu: {speaker}", { speaker: transcriptSpeakerLabel(generation, segment.speaker_id || "") });
    review.querySelector(".transcript-draft-value").textContent =
      t("Lựa chọn cần xem lại: {speaker}", { speaker: transcriptSpeakerLabel(generation, draft.value) });
  }
}

function updateTranscriptRenderKey(generation) {
  if (!generation || $("#generation-title").textContent !== `Transcript #${generation.generation_number}`) return;
  $("#transcript-generation").dataset.renderKey = `${generation.id}:${generation.source_version}:true`;
}

// ---- playback: the view of playback-core.js ----

// What only the page knows about the audio and the list: the recording time, whether it plays, whether its metadata
// is loaded, and (while it plays) whether some of the list shows below the player bar and whether focus is in a
// transcript field.
function audioFacts() {
  const audio = $("#meeting-audio");
  const facts = {
    time: audio.currentTime,
    paused: audio.paused,
    ended: audio.ended,
    ready: audio.readyState >= HTMLMediaElement.HAVE_METADATA,
    audioMeetingId: audio.dataset.meetingId ?? null,
    reduced: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    visible: false,
    typing: false,
  };
  if (audio.paused || audio.ended) return facts;
  // Reading the notes while audio plays must not pull the page to the transcript: follow only
  // while some of the list shows below the player bar.
  const bar = $(".player-bar").getBoundingClientRect();
  const list = $("#transcript-segments").getBoundingClientRect();
  facts.visible = !(list.bottom <= bar.bottom || list.top >= window.innerHeight);
  const focused = document.activeElement;
  facts.typing = Boolean(
    focused
    && focused.closest("#transcription-panel")
    && (focused.matches("textarea, input, select") || focused.isContentEditable),
  );
  return facts;
}

// The command of the effect `playback.seek`: the answer is the audio as it is now.
function seekAudio(seconds) {
  $("#meeting-audio").currentTime = seconds;
  return audioFacts();
}

// The rows playing now and the row of the open notes source, painted from the slice: this runs after every pass over
// the rows (a re-render of the list asks for a new sample).
function paintPlaybackRows(slice) {
  const active = new Set(slice.active);
  const items = [...$("#transcript-segments").querySelectorAll(".transcript-segment")];
  for (const item of items) {
    const playing = active.has(item.dataset.segmentId);
    item.classList.toggle("is-playback-active", playing);
    if (playing) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  }
  for (const item of $("#transcript-segments").querySelectorAll(".is-notes-source")) {
    if (item.dataset.segmentId === slice.mark) continue;
    item.classList.remove("is-notes-source");
    item.querySelector(":scope > .segment-source-mark")?.remove();
  }
  const row = slice.mark ? segmentRow(slice.mark) : null;
  if (!row || row.classList.contains("is-notes-source")) return;
  row.classList.add("is-notes-source");
  const label = document.createElement("span");
  label.className = "segment-source-mark";
  label.textContent = t("Nguồn của mục notes");
  row.append(label);
}

// A scroll the app starts is told to the core first, so the scroll events it causes are not the user's.
function runPlaybackOp(op) {
  const row = segmentRow(op.id);
  if (!row) return;
  if (op.op === "reveal") {
    // Bring the source row into view when it is not, moving the page no further than that takes so the card above
    // stays as visible as it can.
    const bar = $(".player-bar").getBoundingClientRect();
    const box = row.getBoundingClientRect();
    if (box.top >= bar.bottom && box.bottom <= window.innerHeight) return;
    dispatchBare({ type: "playback/scroll-started" });
  }
  row.scrollIntoView({ block: "nearest", behavior: op.smooth ? "smooth" : "auto" });
}

function paintPlayback(root, memory) {
  const slice = root.playback;
  const painted = memory.playback;
  memory.playback = slice;
  if (slice.rev !== painted.rev) paintPlaybackRows(slice);
  for (const op of slice.ops) runPlaybackOp(op);
  if (slice.ops.length) dispatchBare({ type: "playback/painted", through: slice.ops.at(-1).n });
}

// A new sample of the audio: which rows play now, and whether the list follows.
function updateActiveTranscriptIntervals() {
  dispatch({ type: "playback/sampled", ...audioFacts() });
}

// Mark the row a saved notes source names (its ID, else the row spanning its time, since a newer
// transcript has other IDs) and bring it into view when it is not. Null clears the mark. True when a row was found.
function markNotesSource(segmentId, startSeconds) {
  const shown = shownNow();
  const found = sourceRow((shown && shown.rows) || [], segmentId, startSeconds) !== null;
  dispatch({ type: "playback/mark-requested", segmentId, startSeconds, reduced: audioFacts().reduced });
  return found;
}

// Start the recording where it is, but only while its player is on screen: the Transcript tab. A browser
// may also refuse without a recent gesture, which is not an error.
async function playRecording() {
  if (!canPlay(shownNow())) return false;
  const audio = $("#meeting-audio");
  try {
    await audio.play();
    return !audio.paused;
  } catch {
    return false;
  }
}

function clearReloadLatestActions() {
  for (const container of [
    $("#transcript-generation"),
    $("#transcription-error"),
    $("#transcript-mutation-error"),
  ]) {
    for (const action of container.querySelectorAll("[data-reload-latest], .transcript-reload-status")) {
      action.remove();
    }
  }
}

function clearReloadLatestAction(container) {
  for (const action of container.querySelectorAll("[data-reload-latest], .transcript-reload-status")) {
    action.remove();
  }
}

// A control of the transcript as data: the core adds the meeting it belongs to.
function describeTranscriptMutationFocus(element) {
  if (element.id === "speaker-add-name") {
    return { kind: "speaker-add", control: "input" };
  }
  const reapplyAdd = element.closest("[data-reapply-speaker-add-draft]");
  if (reapplyAdd) {
    return { kind: "speaker-add", control: "reapply", generationId: reapplyAdd.dataset.generationId };
  }
  const assignmentReview = element.closest("[data-reapply-assignment-draft]");
  if (assignmentReview) {
    return {
      kind: "speaker-assignment",
      generationId: assignmentReview.dataset.generationId,
      segmentId: assignmentReview.dataset.segmentId,
    };
  }
  const assignment = element.closest(".segment-speaker-select");
  if (assignment) {
    return {
      kind: "speaker-assignment",
      generationId: assignment.dataset.generationId,
      segmentId: assignment.dataset.segmentId,
    };
  }
  const textForm = element.closest("form[data-segment-text]");
  if (textForm) {
    return {
      kind: "segment-text",
      generationId: textForm.dataset.generationId,
      segmentId: textForm.dataset.segmentId,
      control: element.matches(".segment-text-editor") ? "editor" : "submit",
    };
  }
  const speakerForm = element.closest("form[data-speaker-name]");
  if (speakerForm) {
    return {
      kind: "speaker-name",
      generationId: speakerForm.dataset.generationId,
      speakerId: speakerForm.dataset.speakerId,
      control: element.matches(".speaker-clear-name")
        ? "clear"
        : element.matches("input") ? "input" : "submit",
    };
  }
  const addForm = element.closest("#speaker-add-form");
  if (addForm) return { kind: "speaker-add", control: "submit" };
  return null;
}

function resolveTranscriptMutationFocus(descriptor) {
  if (!nav().meeting || descriptor.meetingId !== nav().meeting.id) return null;
  if (descriptor.kind === "speaker-add") {
    if (descriptor.control === "input") return $("#speaker-add-name");
    // The recovery button keeps focus while it is still there and usable (a failed write), as the old page refocused it.
    const reapply = descriptor.control === "reapply" && [...document.querySelectorAll("[data-reapply-speaker-add-draft]")].find(
      (element) => element.dataset.generationId === descriptor.generationId && !element.disabled,
    );
    return reapply || $("#speaker-add-form button[type='submit']");
  }
  if (descriptor.kind === "speaker-assignment") {
    return [...document.querySelectorAll(".segment-speaker-select")].find(
      (element) => element.dataset.generationId === descriptor.generationId
        && element.dataset.segmentId === descriptor.segmentId,
    ) || null;
  }
  if (descriptor.kind === "segment-text") {
    const form = [...document.querySelectorAll("form[data-segment-text]")].find(
      (element) => element.dataset.generationId === descriptor.generationId
        && element.dataset.segmentId === descriptor.segmentId,
    );
    return form && form.querySelector(
      descriptor.control === "editor" ? ".segment-text-editor" : "button[type='submit']",
    );
  }
  if (descriptor.kind === "speaker-name") {
    const form = [...document.querySelectorAll("form[data-speaker-name]")].find(
      (element) => element.dataset.generationId === descriptor.generationId
        && element.dataset.speakerId === descriptor.speakerId,
    );
    if (!form) return null;
    if (descriptor.control === "input") return form.querySelector("input");
    return form.querySelector(
      descriptor.control === "clear" ? ".speaker-clear-name" : "button[type='submit']",
    );
  }
  return null;
}

function syncTranscriptMutationControls() {
  const { mutation, reload, notice, noticeRev } = tstate();
  const busy = Boolean(mutation);
  for (const control of document.querySelectorAll(
    "#transcript-generation [data-transcript-mutation], "
    + "#transcription-error [data-transcript-mutation], "
    + "#transcript-mutation-error [data-transcript-mutation], "
    + "#transcript-draft-recovery [data-transcript-mutation]",
  )) {
    const currentGeneration = nav().meeting && nav().meeting.transcription
      && nav().meeting.transcription.generation;
    const addFormControl = control.closest("#speaker-add-form")
      && !control.matches("[data-reload-latest]");
    const addRecoveryControl = control.matches("[data-reapply-speaker-add-draft]");
    if (addFormControl || addRecoveryControl) {
      const generationId = addRecoveryControl
        ? control.dataset.generationId
        : currentGeneration && currentGeneration.id;
      const draft = generationId
        ? transcriptDraft(generationId, "speakerAddName", "speaker-add-name")
        : null;
      control.disabled = !speakerAddRecoveryReady(currentGeneration, draft);
      continue;
    }
    if (control.matches("#generation-selector, [data-reload-latest]")) {
      control.disabled = busy || (control.matches("[data-reload-latest]") && Boolean(reload));
      continue;
    }
    const assignmentDraft = control.matches("[data-reapply-assignment-draft]")
      ? transcriptDraft(control.dataset.generationId, "speakerAssignment", control.dataset.segmentId)
      : null;
    control.disabled = sourceMutationUnavailable()
      || busy
      || Boolean(assignmentDraft && assignmentDraft.conflictCode);
    if (control.matches("[data-edit-segment], [data-speaker-chip]")) {
      // A locked source says why in #generation-mode; tie that reason to the disabled control.
      if (sourceMutationUnavailable()) control.setAttribute("aria-describedby", "generation-mode");
      else control.removeAttribute("aria-describedby");
    }
  }
  // Hủy only drops local text, so a locked source leaves it usable; a save in flight does not.
  for (const cancel of document.querySelectorAll("#transcript-generation [data-edit-cancel]")) {
    cancel.disabled = busy;
  }
  $("#transcript-generation").setAttribute("aria-busy", busy ? "true" : "false");
  // The shared status line is announced (aria-live): a reported result is written again, anything else only when its words change.
  const status = $("#transcript-mutation-status");
  if (String(noticeRev) !== (status.dataset.noticeRev ?? "0")) {
    status.dataset.noticeRev = String(noticeRev); // the noticeRev the line was last written for
    status.textContent = tr(notice);
  } else setProp(status, "textContent", tr(notice));
  setProp(status, "hidden", !notice);
}

// ---- the transcript slice: painting what the core asks for, and dispatching what the user does ----

function renderTranscriptMutationError() {
  const panel = $("#transcript-mutation-error");
  const message = $("#transcript-mutation-error-message");
  const { error } = tstate();
  const meeting = nav().meeting;
  const generationExists = Boolean(
    error
    && meeting
    && meeting.id === error.meetingId
    && shownGeneration().selected === error.generationId
    && generationsFor(meeting.transcription).some((generation) => generation.id === error.generationId),
  );
  panel.hidden = !generationExists;
  if (!generationExists) {
    clearReloadLatestAction(panel);
    return;
  }
  message.textContent = tr(error.message);
  if (error.reloadLatest) appendReloadLatestButton(panel, error.generationId);
  else clearReloadLatestAction(panel);
}

// Everything under the transcription status that follows the generation on screen.
function paintTranscriptTail(transcription) {
  const selected = renderGenerationNavigation(transcription);
  renderTabCounts(selected && selected.generation);
  renderTranscriptGeneration(selected && selected.generation, Boolean(selected && selected.editable));
  renderTranscriptDraftRecovery(transcription, selected && selected.generation.id);
  renderTranscriptMutationError();
}

// The transcript slice knows the installed meeting and has taken the selection from it.
function transcriptSynced(root) {
  const meeting = root.history.meeting;
  if (!meeting) return true;
  return root.transcript.installed === root.history.installRev && !navigation(root.transcript, meeting.transcription).changed;
}

// A control the core names, found on the page.
const segmentForm = (generationId, segmentId) => [...document.querySelectorAll("form[data-segment-text]")].find(
  (element) => element.dataset.generationId === generationId && element.dataset.segmentId === segmentId,
) || null;
const speakerForm = (generationId, speakerId) => [...document.querySelectorAll("form[data-speaker-name]")].find(
  (element) => element.dataset.generationId === generationId && element.dataset.speakerId === speakerId,
) || null;
const assignmentSelect = (generationId, segmentId) => [...document.querySelectorAll(".segment-speaker-select")].find(
  (element) => element.dataset.generationId === generationId && element.dataset.segmentId === segmentId,
) || null;
const draftReview = (generationId, field, itemId) => [...document.querySelectorAll(".transcript-draft-review")].find(
  (review) => review.dataset.generationId === generationId
    && review.dataset.draftField === field
    && review.dataset.draftItemId === itemId,
) || null;

// The line beside a control that reports its write.
function statusElement(descriptor) {
  if (!descriptor) return null;
  if (descriptor.kind === "segment-text") return segmentForm(descriptor.generationId, descriptor.segmentId)?.querySelector(".segment-edit-status") ?? null;
  if (descriptor.kind === "speaker-name") return speakerForm(descriptor.generationId, descriptor.speakerId)?.querySelector(".speaker-form-status") ?? null;
  if (descriptor.kind === "speaker-add") return $("#speaker-add-status");
  return null;
}

// The element a reload button sits in, from the descriptor of the button (none: the conflict panel).
function reloadHost(anchor) {
  if (!anchor) return $("#transcript-mutation-error");
  if (anchor.kind === "segment-text") return segmentForm(anchor.generationId, anchor.segmentId)?.querySelector(".segment-edit-actions") ?? null;
  if (anchor.kind === "speaker-name") return speakerForm(anchor.generationId, anchor.speakerId);
  return $("#speaker-add-form");
}

function runTranscriptOp(op) {
  const transcription = transcriptionNow();
  const find = (generationId) => generationsFor(transcription).find((item) => item.id === generationId);
  switch (op.op) {
    case "error":
      renderTranscriptMutationError();
      break;
    case "recovery":
      renderTranscriptDraftRecovery(transcription, shownGeneration().selected);
      break;
    case "row":
      replaceSegmentRow(op.generationId, op.segmentId);
      break;
    case "chip":
      replaceSpeakerItem(op.generationId, op.speakerId);
      break;
    case "labels": {
      const generation = find(op.generationId);
      const speaker = generation && generation.speakers.find((item) => item.speaker_id === op.speakerId);
      if (generation && speaker) updateSpeakerLabelsInTranscript(generation, speaker);
      break;
    }
    case "key":
      updateTranscriptRenderKey(find(op.generationId));
      break;
    case "generation": {
      const { generation, editable } = shownGeneration();
      if (op.force) delete $("#transcript-generation").dataset.renderKey;
      renderTranscriptGeneration(generation, op.editable ?? editable);
      break;
    }
    case "status": {
      const element = statusElement(op.target);
      if (!element) break;
      if (op.text !== undefined) element.textContent = tr(op.text);
      element.classList.toggle("is-error", Boolean(op.error));
      break;
    }
    case "clear-reload": {
      const element = statusElement(op.target);
      if (element) clearReloadLatestAction(element.parentElement);
      break;
    }
    case "reload-button": {
      const element = statusElement(op.target);
      if (element) appendReloadLatestButton(element.parentElement, op.generationId);
      break;
    }
    case "clear-reload-all":
      clearReloadLatestActions();
      break;
    case "reload-note": {
      const status = reloadHost(op.anchor)?.querySelector(".transcript-reload-status");
      if (status) status.textContent = tr(op.text);
      break;
    }
    case "review": {
      const draft = transcriptDraft(op.generationId, op.field, op.itemId);
      if (!draft) removeTranscriptDraftReview(op.generationId, op.field, op.itemId);
      else updateTranscriptDraftReview(op.generationId, op.field, op.itemId, draft, fieldValue(transcription, op.generationId, op.field, op.itemId));
      break;
    }
    case "assignment-note": {
      const status = segmentRow(op.segmentId)?.querySelector(".transcript-assignment-draft-review .transcript-assignment-draft-status");
      if (status) status.textContent = tr(op.text);
      break;
    }
    case "assignment-synced": {
      const segment = find(op.generationId)?.segments.find((item) => item.id === op.segmentId);
      const select = assignmentSelect(op.generationId, op.segmentId);
      if (!segment || !select) break;
      select.value = segment.speaker_id || "";
      select.dataset.currentSpeaker = segment.speaker_id || "";
      select.dataset.assignmentVersion = String(segment.assignment_version || 1);
      select.closest(".transcript-segment").querySelector(".transcript-assignment-draft-review")?.remove();
      break;
    }
    case "assignment-failed": {
      const select = assignmentSelect(op.generationId, op.segmentId);
      if (!select) break;
      if (op.conflict) {
        select.value = op.value;
        break;
      }
      const current = transcription && transcription.generation;
      const segment = current && current.id === op.generationId && current.segments.find((item) => item.id === op.segmentId);
      select.value = (segment && segment.speaker_id) || "";
      select.closest(".transcript-segment").querySelector(".transcript-assignment-draft-review")?.remove();
      break;
    }
    case "copy-status": {
      const status = draftReview(op.generationId, op.field, op.itemId)?.querySelector(".transcript-draft-copy-status");
      if (status) {
        status.textContent = op.how === "copied"
          ? t("Đã sao chép bản nháp.")
          : t("Bản nháp đã được chọn. Nhấn Ctrl+C hoặc ⌘C để sao chép.");
      }
      break;
    }
    default:
      throw new Error(`unknown transcript paint command: ${op.op}`);
  }
}

// A focus target the core names: a control of a write, or the row, chip or editor an action moved to.
function resolveTranscriptFocus(target) {
  if (!target) return null;
  const row = target.segmentId ? segmentRow(target.segmentId) : null;
  if (target.kind === "segment-editor") return row?.querySelector(".segment-text-editor") ?? null;
  if (target.kind === "segment-edit-button") {
    const button = row?.querySelector(".segment-edit-button") ?? null;
    if (!target.orTimestamp) return button;
    // A locked source disables Sửa; keep the keyboard on the row instead of dropping it to the page.
    return button && !button.disabled ? button : button?.closest(".transcript-segment").querySelector(".segment-timestamp") ?? null;
  }
  if (target.kind === "speaker-rename-input") return speakerItem(target.speakerId)?.querySelector("input") ?? null;
  if (target.kind === "speaker-chip") {
    const chip = speakerItem(target.speakerId)?.querySelector(".speaker-chip") ?? null;
    return target.enabledOnly && chip && chip.disabled ? null : chip;
  }
  return resolveTranscriptMutationFocus(target);
}

function moveTranscriptFocus(focus) {
  if (focus.mode === "now") {
    const element = resolveTranscriptFocus(focus.target);
    if (!element) return;
    element.focus();
    if (focus.caret === "end") element.setSelectionRange(element.value.length, element.value.length);
    else if (focus.caret === "select") element.select();
    return;
  }
  // After a write: to the control the result moved to, else back to where the user was, but only if focus was not
  // taken elsewhere meanwhile.
  const previous = focus.from ? resolveTranscriptMutationFocus(focus.from) : null;
  let element = focus.sameMeeting ? resolveTranscriptFocus(focus.target) : null;
  if ((!element || !element.isConnected) && previous) element = previous;
  const active = document.activeElement;
  if (element && element.isConnected && (active === document.body || active === previous || active === element)) {
    element.focus({ preventScroll: true });
  }
}

// Paint what the transcript slice changed: bring it up to date with the installed meeting first (it reconciles the
// drafts), then the paint commands in order, the tail, the focus move, and the controls.
function paintTranscript(root, memory) {
  const slice = root.transcript;
  const painted = memory.transcript;
  if (!root.history.meeting) {
    memory.transcript = slice;
    // The status line is written at once, into the hidden meeting view, as before; opening the next meeting does not announce it.
    if (slice !== painted) syncTranscriptMutationControls();
    if (slice.ops.length) dispatch({ type: "transcript/painted", through: slice.ops.at(-1).n }); // no page to run them on
    return;
  }
  if (!transcriptSynced(root)) {
    dispatch({ type: slice.installed !== root.history.installRev ? "transcript/meeting-installed" : "transcript/transcription-shown" });
    return;
  }
  memory.transcript = slice;
  for (const op of slice.ops) runTranscriptOp(op);
  if (slice.rev !== painted.rev) paintTranscriptTail(transcriptionNow());
  setProp($("#speaker-add-name"), "value", slice.addName);
  // Before focus moves: a control the write disabled takes no focus. The page rebuilds that touch the controls sync them
  // themselves, so an unchanged slice (each task poll) needs no walk.
  if (slice !== painted) syncTranscriptMutationControls();
  if (slice.focus.rev !== painted.focus.rev) moveTranscriptFocus(slice.focus);
  if (slice.ops.length) dispatch({ type: "transcript/painted", through: slice.ops.at(-1).n });
}

// What the page knows that the core cannot: which control has focus now (and which reports the write).
function mutationFacts(statusSource = null) {
  const active = document.activeElement;
  return {
    focus: active.matches("[data-transcript-mutation]") ? describeTranscriptMutationFocus(active) : null,
    status: describeTranscriptMutationFocus(statusSource || active),
  };
}

// The "copy draft" button: the clipboard, else a hidden textarea, else the draft text selected for Ctrl+C.
async function copyTranscriptDraft(text, target) {
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
    return "copied";
  } catch {
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.focus();
    input.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      // Keep the visible draft selectable when the clipboard command is unavailable.
    }
    input.remove();
    if (copied) return "copied";
    const draftText = draftReview(target.generationId, target.field, target.itemId)?.querySelector(".transcript-draft-value");
    if (draftText) {
      const range = document.createRange();
      range.selectNodeContents(draftText);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    return "selected";
  }
}

function updateSpeakerLabelsInTranscript(generation, speaker) {
  for (const option of $("#transcript-segments").querySelectorAll(".segment-speaker-select option")) {
    if (option.value === speaker.speaker_id) option.textContent = speaker.display_name;
  }
  for (const label of $("#transcript-segments").querySelectorAll(".segment-original-speaker")) {
    if (label.dataset.originalSpeakerId === speaker.speaker_id) {
      label.textContent = t("Gán ban đầu: {speaker}", { speaker: speaker.display_name });
    }
  }
  for (const name of $("#transcript-segments").querySelectorAll(".segment-speaker")) {
    if (name.dataset.speakerId === speaker.speaker_id) name.textContent = speaker.display_name;
  }
  const item = speakerItem(speaker.speaker_id);
  if (item) {
    const input = item.querySelector("input");
    if (input) input.value = speaker.display_name;
    else replaceSpeakerItem(generation.id, speaker.speaker_id);
  }
  updateAssignmentDraftReviewLabels(generation);
  updateTranscriptRenderKey(generation);
}

function renderRuntimeProfile(runtime) {
  const profile = selectRuntime(runtime);
  $("#runtime-profile").dataset.device = profile.device;
  $("#runtime-backend").textContent = tr(profile.backend);
  $("#runtime-model").textContent = tr(profile.model);
}

function taskStageLabel(task) {
  return TASK_STAGES[task.stage] || task.stage || t("Chưa bắt đầu");
}

// The bar is the share of the recording already transcribed; without a usable duration it is
// indeterminate (no value attribute, no percent) rather than a guess.
function renderTaskProgress(progress, task) {
  $("#task-progress").hidden = !progress;
  if (!progress) return;
  const bar = $("#task-progress-bar");
  if (progress.known) bar.value = progress.fraction;
  else bar.removeAttribute("value");
  $("#task-progress-percent").textContent = progress.known ? `${progress.percent}%` : "";
  $("#task-progress-stage").textContent = taskStageLabel(task);
}

function renderTranscription(transcription) {
  const meeting = nav().meeting;
  const task = transcription && transcription.task;
  const generation = transcription && transcription.generation;
  const view = selectTask(store.getState().task, shownNow());
  const workerChecking = view.workerChecking;
  const startButton = $("#start-transcription");
  const cancelButton = $("#cancel-transcription");
  const measurements = $("#task-measurements");

  renderLanguageControl(view);
  renderModelControl(view);
  renderRuntimeProfile(transcription && transcription.runtime);

  startButton.hidden = view.active;
  startButton.disabled = view.requestBusy || sourceMutationUnavailable() || Boolean(view.model && view.model.blocked);
  startButton.textContent = generation ? t("Chạy lại bản ghi") : t("Bắt đầu chép lời");
  // The first run is the decisive action; running again over a transcript is not.
  startButton.classList.toggle("primary-button", !generation);
  startButton.classList.toggle("secondary-button", Boolean(generation));
  cancelButton.hidden = !view.active;
  cancelButton.disabled = view.cancelDisabled;
  cancelButton.textContent = view.stopping ? t("Đang dừng…") : t("Hủy chép lời");
  renderTaskProgress(view.progress, task);
  measurements.hidden = !task;
  renderTaskError(view);

  // With the button off, the model guidance says what to do: do not also invite a click on it.
  const modelBlocked = Boolean(view.model && view.model.blocked);
  if (!task) {
    $("#transcription-status").textContent = generation
      ? t("Transcript hoàn chỉnh đã được lưu. Bạn có thể nghe lại các mốc hoặc chủ động chạy lại bản ghi.")
      : modelBlocked
        ? t("Bản ghi đã lưu.")
        : t("Bản ghi đã lưu. Bấm “Bắt đầu chép lời” khi bạn muốn chạy nhận dạng local.");
  } else {
    const statusText = {
      accepted: t("Tác vụ đã được nhận. Worker sẽ bắt đầu xử lý."),
      running: t("Đang chép lời bằng model local. Tác vụ tiếp tục nếu bạn đóng tab."),
      stopping: t("Đang dừng và thu hồi runner. Slot chỉ được nhường sau khi runner đã kết thúc."),
      succeeded: t("Transcript đã được lưu hoàn chỉnh."),
      failed: view.error.message || t("Tác vụ thất bại. Transcript hoàn chỉnh trước đó vẫn được giữ."),
      canceled: generation
        ? t("Đã hủy sau khi runner dừng. Transcript hoàn chỉnh trước đó vẫn được giữ.")
        : t("Đã hủy sau khi runner dừng. Chưa có transcript hoàn chỉnh được lưu."),
      interrupted: modelBlocked
        ? t("Worker đã dừng giữa tác vụ. Không tự chạy lại.")
        : t("Worker đã dừng giữa tác vụ. Không tự chạy lại; hãy bấm để thử lại."),
    };
    $("#transcription-status").textContent = workerChecking
      ? task.status === "accepted"
        ? t("Đang kiểm tra worker trước khi bắt đầu xử lý.")
        : t("Đang kiểm tra worker vì heartbeat tác vụ đã cũ.")
      : statusText[task.status] || t("Đang tải trạng thái tác vụ.");
    $("#task-stage").textContent = taskStageLabel(task);
    $("#task-elapsed").textContent = formatElapsedSeconds(task.elapsed_seconds);
    $("#task-segment-count").textContent = String(task.segment_count || 0);
    $("#task-transcript-end").textContent = formatTimestamp(task.transcript_end_seconds || 0);
  }
  if (view.notice) $("#transcription-status").textContent = NOTICE_TEXT[view.notice];
  const status = $("#transcription-status");
  status.dataset.workerState = task && task.worker_state ? task.worker_state : "";
  status.classList.toggle("is-checking-worker", workerChecking);
  renderMeetingProcessingStatus(task, generation);
  if (transcriptSynced(store.getState())) paintTranscriptTail(transcription);
  notifyMeetingContext();
}

function renderLanguageControl(view) {
  const selector = $("#transcription-language");
  if (!selector || !view.language) return;
  setProp(selector, "value", view.language.value);
  setProp(selector, "disabled", view.language.disabled);
}

function renderModelControl(view) {
  const { model } = view;
  setProp($("#transcription-model-control"), "hidden", !model);
  const guidance = $("#transcription-model-guidance");
  setProp(guidance, "hidden", !(model && model.guidance));
  if (!model) return;
  const selector = $("#transcription-model");
  const optionsKey = model.options.map((option) => `${option.id}:${option.ready}`).join("|");
  if (selector.dataset.optionsKey !== optionsKey) {
    selector.replaceChildren(...model.options.map((option) => {
      const element = document.createElement("option");
      element.value = option.id;
      element.textContent = `${tr(option.label)} · ${option.ready ? t("sẵn sàng") : t("chưa sẵn sàng")}`;
      return element;
    }));
    selector.dataset.optionsKey = optionsKey;
  }
  setProp(selector, "value", model.value);
  setProp(selector, "disabled", model.disabled);
  setProp($("#transcription-model-help"), "textContent", tr(model.note));
  if (model.guidance) setProp($("#transcription-model-guidance-text"), "textContent", tr(model.guidance));
  setProp($("#transcription-model-command-wrap"), "hidden", !model.command);
  setProp($("#transcription-model-command"), "value", model.command);
}

// ---- transcription task: the view of task-core.js ----

function newRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// The only reads of sessionStorage: task-core does not read storage, the values enter through its actions.
function readStored(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

// Commands of the clipboard effects (`call`): the copy button, and the fallback that selects the command.
function writeClipboard(text) {
  if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Clipboard unavailable");
  return navigator.clipboard.writeText(text);
}

function selectDiagnosticCommand() {
  const input = $("#task-diagnostic-command");
  input.focus();
  input.select();
  return document.execCommand("copy");
}

export const meetingHistory = Object.freeze({
  // `known` is false until notes.js has read this meeting's notes; the first answer settles the default tab.
  setNotesSummary(meetingId, revisionCount, taskStatus, known = true) {
    dispatch({ type: "history/notes-summary-reported", meetingId, count: revisionCount, status: taskStatus, known });
  },
  selectTab,
  defaultSpeakerNames,
  focusSpeakerCatalog,
  showNotesSourceTab,
  markNotesSource,
  playRecording,
  currentContext() {
    const state = nav();
    return {
      meeting: state.view === "meeting" ? state.meeting : null,
      view: state.view,
      "transcriptionRequestInFlight": selectRequestBusy(store.getState().task),
    };
  },
  hasUnsavedTranscriptDrafts() {
    return hasUnsavedDrafts(tstate());
  },
  setNotesSourceLock(meetingId, locked, known = true) {
    dispatch({ type: "history/notes-source-lock-reported", meetingId, locked, known });
  },
  seekNotesSource,
  refreshMeeting(meetingId) {
    const { meeting } = nav();
    if (!meeting || meeting.id !== meetingId) return Promise.resolve(false);
    return loadMeeting(meetingId);
  },
  publishMeetingChange(meetingId, reason) {
    publishMeetingChange(meetingId, reason);
  },
});

// ---- wiring ----

for (const root of document.querySelectorAll("[data-calendar]")) {
  buildCalendar(root);
  root.addEventListener("click", onCalendarClick);
  root.addEventListener("input", onCalendarInput);
  root.addEventListener("change", onCalendarChange);
  root.addEventListener("focusout", onCalendarFocusout);
  root.addEventListener("keydown", onCalendarKeydown);
}

$(".brand").addEventListener("click", (event) => {
  const link = event.currentTarget;
  if (
    event.defaultPrevented
    || event.button !== 0
    || event.metaKey
    || event.ctrlKey
    || event.shiftKey
    || event.altKey
    || link.hasAttribute("target")
    || link.hasAttribute("download")
  ) return;
  const destination = new URL(link.href, window.location.href);
  if (
    destination.origin !== window.location.origin
    || destination.pathname !== "/"
    || destination.search
    || destination.hash
  ) return;
  event.preventDefault();
  dispatch({ type: "history/home-clicked" });
  $("#page-title").focus();
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-meeting-link]");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  dispatch({ type: "history/meeting-link-clicked", meetingId: link.dataset.meetingLink });
});
$("#open-calendar").addEventListener("click", () => dispatch({ type: "history/calendar-opened" }));
$("#calendar-back").addEventListener("click", leaveCalendar);
const actionsMenu = $("#meeting-actions-menu");
// ponytail: fixed position from the toggle's rect; it does not follow scroll while open (light dismiss closes it on click)
actionsMenu.addEventListener("toggle", (event) => {
  if (event.newState !== "open") return;
  const rect = $("#meeting-actions").getBoundingClientRect();
  actionsMenu.style.top = `${rect.bottom + 6}px`;
  actionsMenu.style.right = `${document.documentElement.clientWidth - rect.right}px`;
});
$("#edit-meeting").addEventListener("click", () => {
  actionsMenu.hidePopover();
  dispatch({ type: "history/editor-opened" });
});
$("#delete-meeting").addEventListener("click", () => {
  actionsMenu.hidePopover();
  dispatch({ type: "history/deletion-opened" });
});
$("#meeting-delete-form").addEventListener("submit", (event) => {
  event.preventDefault();
  dispatch({ type: "history/deletion-submitted", confirmation: $("#meeting-delete-confirmation").value });
});
$("#meeting-delete-confirmation").addEventListener("input", (event) => {
  dispatch({ type: "history/deletion-confirmation-edited", value: event.currentTarget.value });
});
// State closes first: a render between the native close and its queued close event would reopen the dialog.
function closeDeletionDialog() {
  dispatch({ type: "history/deletion-dialog-closed" });
  $("#meeting-delete-dialog").close();
}
$("#meeting-delete-cancel").addEventListener("click", closeDeletionDialog);
$("#meeting-delete-close").addEventListener("click", closeDeletionDialog);
$("#meeting-delete-dialog").addEventListener("cancel", () => dispatch({ type: "history/deletion-dialog-closed" }));
$("#meeting-delete-dialog").addEventListener("close", () => {
  // The opener sits in the closed actions menu, so focus falls back to the menu toggle.
  if (nav().view === "meeting" && document.activeElement === document.body) $("#meeting-actions").focus();
});
$("#edit-cancel").addEventListener("click", () => dispatch({ type: "history/editor-cancelled" }));
$("#edit-clear-date").addEventListener("click", () => {
  $("#edit-date").value = ""; // a half-typed date reads as "" already, so only this drops it
  dispatch({ type: "history/editor-edited", date: "" });
  $("#edit-date").focus();
});
$("#edit-title").addEventListener("input", (event) => dispatch({ type: "history/editor-edited", title: event.currentTarget.value }));
$("#edit-date").addEventListener("input", (event) => dispatch({ type: "history/editor-edited", date: event.currentTarget.value }));
$("#edit-date").addEventListener("change", (event) => dispatch({ type: "history/editor-edited", date: event.currentTarget.value }));
$("#meeting-edit").addEventListener("submit", (event) => {
  event.preventDefault();
  dispatch({
    type: "history/editor-submitted",
    title: $("#edit-title").value,
    date: $("#edit-date").value,
    badInput: $("#edit-date").validity.badInput,
  });
});

$("#transcription-language").addEventListener("change", (event) => {
  dispatch({ type: "task/language-chosen", language: event.currentTarget.value });
});
$("#transcription-model").addEventListener("change", (event) => dispatch({ type: "task/model-chosen", model: event.target.value }));
$("#start-transcription").addEventListener("click", () => dispatch({ type: "task/start-clicked", requestId: newRequestId() }));
$("#cancel-transcription").addEventListener("click", () => dispatch({ type: "task/cancel-clicked" }));
$("#task-diagnostic-copy").addEventListener("click", () => dispatch({ type: "task/copy-clicked" }));
$("#generation-selector").addEventListener("change", (event) => {
  dispatch({ type: "transcript/generation-chosen", id: event.currentTarget.value });
});
$("#speaker-add-name").addEventListener("input", (event) => {
  dispatch({ type: "transcript/add-name-typed", value: event.currentTarget.value });
});
$("#transcript-generation").addEventListener("submit", (event) => {
  const form = event.target;
  if (form.matches("form[data-segment-text]")) {
    event.preventDefault();
    dispatch({
      type: "transcript/segment-saved",
      generationId: form.dataset.generationId,
      segmentId: form.dataset.segmentId,
      text: form.querySelector(".segment-text-editor").value,
      ...mutationFacts(form),
    });
  } else if (form.matches("#speaker-add-form")) {
    event.preventDefault();
    dispatch({ type: "transcript/speaker-add-submitted", name: form.querySelector("#speaker-add-name").value, ...mutationFacts(form) });
  } else if (form.matches("form[data-speaker-name]")) {
    event.preventDefault();
    dispatch({
      type: "transcript/speaker-name-saved",
      generationId: form.dataset.generationId,
      speakerId: form.dataset.speakerId,
      name: form.querySelector("input").value,
      reviewed: false,
      ...mutationFacts(form),
    });
  }
});
// A speaker choice, from the select of an open row or from "Áp dụng lại lựa chọn" under a draft.
function dispatchAssignment(select, reviewed) {
  dispatch({
    type: "transcript/assignment-chosen",
    generationId: select.dataset.generationId,
    segmentId: select.dataset.segmentId,
    value: select.value,
    assignmentVersion: select.dataset.assignmentVersion,
    reviewed,
    ...mutationFacts(select),
  });
}
$("#transcript-generation").addEventListener("click", (event) => {
  const reapplyAssignment = event.target.closest("button[data-reapply-assignment-draft]");
  if (reapplyAssignment) {
    const select = assignmentSelect(reapplyAssignment.dataset.generationId, reapplyAssignment.dataset.segmentId);
    if (select) dispatchAssignment(select, true);
    return;
  }
  const reload = event.target.closest("button[data-reload-latest]");
  if (reload) {
    dispatch({ type: "transcript/reload-clicked", anchor: describeTranscriptMutationFocus(reload) });
    return;
  }
  const copyDraft = event.target.closest("button[data-copy-transcript-draft]");
  if (copyDraft) {
    dispatch({
      type: "transcript/copy-clicked",
      generationId: copyDraft.dataset.generationId,
      field: copyDraft.dataset.draftField,
      itemId: copyDraft.dataset.draftItemId,
    });
    return;
  }
  const chip = event.target.closest("button[data-speaker-chip]");
  if (chip) {
    dispatch({ type: "transcript/speaker-rename-opened", speakerId: chip.dataset.speakerId });
    return;
  }
  const cancelRename = event.target.closest("button[data-speaker-cancel]");
  if (cancelRename) {
    dispatch({ type: "transcript/speaker-rename-cancelled", speakerId: cancelRename.closest("form").dataset.speakerId });
    return;
  }
  const clear = event.target.closest("button[data-clear-speaker-name]");
  if (!clear) return;
  const form = clear.closest("li").querySelector("form[data-speaker-name]");
  if (form) {
    dispatch({
      type: "transcript/speaker-name-saved",
      generationId: form.dataset.generationId,
      speakerId: form.dataset.speakerId,
      name: "",
      reviewed: true,
      ...mutationFacts(form),
    });
  }
});
$("#transcript-draft-recovery").addEventListener("click", (event) => {
  const reapplySpeakerAdd = event.target.closest("[data-reapply-speaker-add-draft]");
  if (reapplySpeakerAdd) {
    dispatch({ type: "transcript/speaker-add-reapplied", generationId: reapplySpeakerAdd.dataset.generationId, ...mutationFacts(reapplySpeakerAdd) });
    return;
  }
  const openDraft = event.target.closest("[data-open-transcript-draft]");
  if (!openDraft) return;
  const selector = $("#generation-selector");
  const generationId = openDraft.dataset.generationId;
  if (!selector || selector.value === generationId) return;
  selector.value = generationId;
  selector.dispatchEvent(new Event("change", { bubbles: true }));
});
function onReloadLatestClick(event) {
  const reload = event.target.closest("button[data-reload-latest]");
  if (reload) dispatch({ type: "transcript/reload-clicked", anchor: describeTranscriptMutationFocus(reload) });
}
$("#transcription-error").addEventListener("click", onReloadLatestClick);
$("#transcript-mutation-error").addEventListener("click", onReloadLatestClick);
$("#transcript-generation").addEventListener("input", (event) => {
  const editor = event.target.closest(".segment-text-editor");
  if (editor) {
    dispatch({
      type: "transcript/draft-typed",
      generationId: editor.closest("form").dataset.generationId,
      field: "segmentText",
      itemId: editor.closest("form").dataset.segmentId,
      value: editor.value,
    });
    return;
  }
  const speakerInput = event.target.closest(".speaker-name-form input");
  if (speakerInput) {
    const form = speakerInput.closest("form");
    dispatch({
      type: "transcript/draft-typed",
      generationId: form.dataset.generationId,
      field: "speakerName",
      itemId: form.dataset.speakerId,
      value: speakerInput.value,
    });
  }
});
// Esc cancels and Ctrl/⌘+Enter saves, but never mid-composition: a Vietnamese IME uses both keys.
// Both work from any control of the open row: textarea, speaker select, Hủy, "Chi tiết".
$("#transcript-generation").addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Escape") {
    const row = event.target.closest(".transcript-segment.is-editing");
    const renaming = event.target.closest(".speaker-name-form");
    if (row) dispatch({ type: "transcript/segment-edit-cancelled", segmentId: row.dataset.segmentId });
    else if (renaming) dispatch({ type: "transcript/speaker-rename-cancelled", speakerId: renaming.dataset.speakerId });
    else return;
    event.preventDefault();
  } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    const form = event.target.closest(".transcript-segment.is-editing")
      ?.querySelector(".segment-edit-form");
    if (!form) return;
    event.preventDefault();
    form.requestSubmit();
  }
});
$("#transcript-segments").addEventListener("click", (event) => {
  const edit = event.target.closest("button[data-edit-segment]");
  if (edit) {
    dispatch({ type: "transcript/segment-edit-opened", segmentId: edit.closest(".transcript-segment").dataset.segmentId });
    return;
  }
  const cancelEdit = event.target.closest("button[data-segment-cancel]");
  if (cancelEdit) {
    dispatch({ type: "transcript/segment-edit-cancelled", segmentId: cancelEdit.closest(".transcript-segment").dataset.segmentId });
    return;
  }
  const timestamp = event.target.closest("button[data-seek-seconds]");
  if (!timestamp) return;
  const audio = $("#meeting-audio");
  const seconds = Number(timestamp.dataset.seekSeconds);
  if (!Number.isFinite(seconds) || seconds < 0) return;
  audio.currentTime = seconds;
  updateActiveTranscriptIntervals();
  const playback = audio.play();
  if (playback && typeof playback.catch === "function") playback.catch(() => {});
});
$("#transcript-segments").addEventListener("change", (event) => {
  const select = event.target.closest("select[data-speaker-assignment]");
  if (select) dispatchAssignment(select, false);
});
for (const eventName of ["timeupdate", "seeked", "play"]) {
  $("#meeting-audio").addEventListener(eventName, updateActiveTranscriptIntervals);
}
$("#meeting-audio").addEventListener("loadedmetadata", () => dispatch({ type: "playback/metadata-loaded", ...audioFacts() }));
$("#meeting-audio").addEventListener("error", () => dispatch({ type: "playback/audio-failed" }));
// A wheel, a touch move, a scroll key or any scroll the app did not start counts as the user's. A scrollbar drag
// sends no wheel or key first; our own smooth scroll keeps sending scroll events, which the core tells apart.
for (const eventName of ["wheel", "touchmove"]) {
  window.addEventListener(eventName, () => dispatchBare({ type: "playback/manual-scrolled" }), { passive: true });
}
window.addEventListener("keydown", (event) => {
  if (isScrollKey(event.key)) dispatchBare({ type: "playback/manual-scrolled" });
});
window.addEventListener("scroll", () => dispatchBare({ type: "playback/scrolled" }), { passive: true });
document.addEventListener("visibilitychange", () => dispatch({ type: "task/visibility-changed", hidden: document.hidden }));

const searchInput = $("#history-search");
searchInput.addEventListener("input", () => dispatch({ type: "history/search-input", value: searchInput.value }));
searchInput.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !searchInput.value) return;
  searchInput.value = "";
  dispatch({ type: "history/search-input", value: "" });
});

window.addEventListener("popstate", () => dispatch({ type: "history/popped" })); // for the same meeting this also paints the tab the address names
window.addEventListener("hashchange", () => dispatch({ type: "history/hash-changed" }));

$("#meeting-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-meeting-tab]");
  if (tab) selectTab(tab.dataset.meetingTab);
});
// Arrow keys, Home and End move between the tabs and choose the one they land on.
$("#meeting-tabs").addEventListener("keydown", (event) => {
  const tab = event.target.closest("[data-meeting-tab]");
  if (!tab || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const index = MEETING_TABS.indexOf(tab.dataset.meetingTab);
  const last = MEETING_TABS.length - 1;
  const target = {
    ArrowLeft: index === 0 ? last : index - 1,
    ArrowRight: index === last ? 0 : index + 1,
    Home: 0,
    End: last,
  }[event.key];
  if (target === undefined) return;
  event.preventDefault();
  selectTab(MEETING_TABS[target], { focus: true });
});

store.subscribe(render);
dispatch({ type: "task/visibility-changed", hidden: document.hidden });
dispatch({ type: "history/booted", today: todayKey() });
