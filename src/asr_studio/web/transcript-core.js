// Pure core of the transcript of the open meeting (phase 4c of #118): the generation on screen, the one open segment
// editor or speaker rename, the unsaved drafts, the four writes (segment text, speaker name, speaker assignment, new
// speaker) with their conflicts, the reload of the latest copy, the status lines and where focus goes afterwards. No
// DOM, fetch, timer, storage, clock or randomness: node --test loads this file directly. Contract:
// docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design.md §11.10.
//
// The slice `transcript` shares the page store with `history` and `task`. The transcript itself lives in the meeting
// that history-core holds. A write the server accepted is one action (`transcript/mutation-succeeded`) that both
// slices reduce: history-core puts the saved value into its meeting (as a new object), this slice computes the same
// result from `shown.meeting`, the meeting the view stamps on every action, to reconcile the drafts against it.
// Ids of the request enter through state counters, not randomness: a stale answer carries an old mutation id.
//
// What only the DOM can tell enters through actions: the text typed in an editor, the value of a select, and two
// descriptors of the control that was used (`focus`: the control that had focus when the write began, `status`: the
// control whose status line reports it). The reducer cannot touch the page, so what it wants repainted is data:
// `ops` are paint commands the view runs in order and then acknowledges (`transcript/painted`), `focus` is where
// focus goes (same idea as history-core's `focus {rev, target}`), `notice` is the text of the shared status line.
//
// User-visible text is data: t(text, params) here only keeps the Vietnamese source string, and the view translates it.

import {
  answerOf,
  applyFieldAnswer,
  applySpeakerAdded,
  failureOf,
  generationsFor,
  isRecord,
} from "./history-core.js";

const t = (text, params) => (params ? { text, params } : { text });

export const SAVING = t("Đang lưu thay đổi transcript.");
export const SAVED = t("Thay đổi transcript đã hoàn tất.");

const FIELDS = ["segmentText", "speakerName", "speakerAssignment", "speakerAddName"];
const ADD_ITEM = "speaker-add-name";
const KEY = { mutation: "transcript/mutation", reload: "transcript/reload", copy: "transcript/copy" };
const CONFLICTS = ["source_version_changed", "current_generation_changed"];

export const initialState = {
  selected: null, // id of the generation on screen
  lastCurrent: null, // id of the current generation when the page last looked: a new one takes the selection
  installed: 0, // the installRev of history-core the drafts were last reconciled for
  editing: null, // { generationId, segmentId }: the one segment whose editor is open
  renaming: null, // { generationId, speakerId }: the one speaker chip being renamed
  drafts: {}, // generationId -> { segmentText, speakerName, speakerAssignment, speakerAddName }: itemId -> draft
  addName: "", // text in the add-speaker input
  mutation: null, // the write on the wire: { id, kind, meetingId, generationId, field, itemId, draftValue, meeting, focus, status, after }
  reload: null, // { id, meetingId, installRev }: the read of the latest copy on the wire
  error: null, // { meetingId, generationId, message, reloadLatest }: the panel above the transcript
  notice: null, // text of the shared status line
  noticeRev: 0, // a result was reported: the view writes the line again, so a live region announces a repeated text
  focus: { rev: 0, mode: null, target: null, from: null, sameMeeting: true }, // where focus goes after painting
  ops: [], // paint commands not acknowledged yet
  opSeq: 0,
  ids: 0, // last mutation or reload id
  rev: 0, // the transcript half of the page is due a repaint
};

const out = (state, effects = []) => ({ state, effects });

// Queue paint commands; the view runs them in order after the state they follow.
function emit(state, ...ops) {
  if (!ops.length) return state;
  const queued = ops.map((op, index) => ({ ...op, n: state.opSeq + index + 1 }));
  return { ...state, ops: [...state.ops, ...queued], opSeq: state.opSeq + ops.length };
}
const repaint = (state) => ({ ...state, rev: state.rev + 1 });
const transcriptionOf = (shown) => (shown && shown.meeting ? shown.meeting.transcription : null);
const withMeeting = (descriptor, shown) => (descriptor ? { ...descriptor, meetingId: shown ? shown.id : null } : null);

// ---- generations ----

// Which generation is on screen. A new current generation takes the selection when the page was following the
// old one; a selection that no longer exists falls back to the current one (else the latest).
export function navigation(state, transcription) {
  const generations = generationsFor(transcription);
  const current = transcription && transcription.generation;
  if (!generations.length) {
    return { generations, selected: null, lastCurrent: null, cleared: Boolean(state.selected), changed: state.selected !== null || state.lastCurrent !== null };
  }
  let { selected, lastCurrent } = state;
  if (current && current.id !== lastCurrent) {
    const previous = lastCurrent;
    lastCurrent = current.id;
    if (!selected || selected === previous) selected = current.id;
  }
  if (!generations.some((item) => item.id === selected)) selected = current ? current.id : generations.at(-1).id;
  const cleared = Boolean(state.selected && state.selected !== selected);
  return { generations, selected, lastCurrent, cleared, changed: selected !== state.selected || lastCurrent !== state.lastCurrent };
}

// The generation on screen and whether it takes edits.
export function displayed(state, transcription) {
  const { generations, selected } = navigation(state, transcription);
  const current = transcription && transcription.generation;
  const generation = generations.find((item) => item.id === selected);
  return { generation, selected, editable: Boolean(generation && current && current.id === generation.id && generation.status === "complete") };
}

function synced(state, transcription) {
  const next = navigation(state, transcription);
  if (!next.changed) return state;
  return repaint({ ...state, selected: next.selected, lastCurrent: next.lastCurrent, addName: next.cleared ? "" : state.addName });
}

export const isEditing = (state, generationId, segmentId) => Boolean(state.editing && state.editing.generationId === generationId && state.editing.segmentId === segmentId);
export const isRenaming = (state, generationId, speakerId) => Boolean(state.renaming && state.renaming.generationId === generationId && state.renaming.speakerId === speakerId);

// ---- drafts ----

const emptyBucket = () => ({ segmentText: {}, speakerName: {}, speakerAssignment: {}, speakerAddName: {} });
const bucketEmpty = (bucket) => FIELDS.every((field) => Object.keys(bucket[field]).length === 0);

export function draftOf(drafts, generationId, field, itemId) {
  const bucket = drafts[generationId];
  return (bucket && bucket[field][itemId]) || null;
}

// Drafts are never changed in place; a draft of null drops the entry, and an emptied bucket goes with it.
function setDraft(drafts, generationId, field, itemId, draft) {
  const bucket = drafts[generationId] || emptyBucket();
  if (!draft && !Object.hasOwn(bucket[field], itemId)) return drafts;
  const items = { ...bucket[field] };
  if (draft) items[itemId] = draft;
  else delete items[itemId];
  const next = { ...bucket, [field]: items };
  if (bucketEmpty(next)) {
    const rest = { ...drafts };
    delete rest[generationId];
    return rest;
  }
  return { ...drafts, [generationId]: next };
}

export const hasUnsavedDrafts = (state) => Object.values(state.drafts).some((bucket) => !bucketEmpty(bucket));
export const hasSegmentDraft = (state, generationId, segmentId) => Boolean(draftOf(state.drafts, generationId, "segmentText", segmentId) || draftOf(state.drafts, generationId, "speakerAssignment", segmentId));

// A conflict-marked draft can be the only copy of what the user typed, so Hủy and Esc keep it.
const isConflicted = (state, generationId, field, itemId) => Boolean(draftOf(state.drafts, generationId, field, itemId)?.conflictCode);

// A conflict marks the draft while the page still shows the value the draft started from. Once a reload shows a
// different one, the review already holds the latest and no reload is due.
export const awaitsLatestReload = (draft, currentValue) => Boolean(draft && draft.conflictCode && draft.baseValue === currentValue);

// The value a draft is measured against: the saved one.
export function fieldValue(transcription, generationId, field, itemId) {
  const generation = generationsFor(transcription).find((item) => item.id === generationId);
  if (!generation) return undefined;
  if (field === "segmentText" || field === "speakerAssignment") {
    const segment = generation.segments.find((item) => item.id === itemId);
    return segment && (field === "segmentText" ? segment.text : segment.speaker_id || "");
  }
  const speaker = generation.speakers.find((item) => item.speaker_id === itemId);
  return speaker && speaker.display_name;
}

const sourceVersionOf = (transcription, generationId) => Number(generationsFor(transcription).find((item) => item.id === generationId)?.source_version) || 0;

// Keep what was typed as a draft, or drop the draft when it equals the saved value. `reviewed` is the user editing a
// draft that a conflict marked: that edit takes the newer saved value as the draft's base and lifts the mark.
function remember(state, transcription, generationId, field, itemId, value, reviewed = false) {
  const baseline = fieldValue(transcription, generationId, field, itemId);
  const sourceVersion = sourceVersionOf(transcription, generationId);
  const review = { op: "review", generationId, field, itemId };
  if (baseline === value) return { state: emit({ ...state, drafts: setDraft(state.drafts, generationId, field, itemId, null) }, review), draft: null };
  const previous = draftOf(state.drafts, generationId, field, itemId);
  const rebased = Boolean(reviewed && previous && previous.conflictCode === "source_version_changed" && (baseline !== previous.baseValue || sourceVersion > previous.baseSourceVersion));
  const draft = {
    value,
    baseValue: rebased || !previous ? baseline : previous.baseValue,
    baseSourceVersion: rebased || !previous ? sourceVersion : previous.baseSourceVersion,
    conflictCode: rebased ? null : (previous && previous.conflictCode) || null,
  };
  return { state: emit({ ...state, drafts: setDraft(state.drafts, generationId, field, itemId, draft) }, review), draft };
}

function rememberSpeakerAdd(state, transcription, generationId, value, conflictCode = null) {
  const previous = draftOf(state.drafts, generationId, "speakerAddName", ADD_ITEM);
  const draft = {
    value,
    baseSourceVersion: previous ? previous.baseSourceVersion : sourceVersionOf(transcription, generationId),
    conflictCode: conflictCode || (previous && previous.conflictCode) || null,
  };
  return { ...state, drafts: setDraft(state.drafts, generationId, "speakerAddName", ADD_ITEM, draft) };
}

const withConflict = (state, generationId, field, itemId, code) => {
  const draft = draftOf(state.drafts, generationId, field, itemId);
  return draft ? { ...state, drafts: setDraft(state.drafts, generationId, field, itemId, { ...draft, conflictCode: code }) } : state;
};

// A newer copy of the meeting arrived (or a save landed): drop the drafts it already holds, mark the ones whose
// base moved on and lift the mark from the ones that no longer conflict. `saved` is the write that just landed.
function reconcile(state, transcription, saved = null) {
  let drafts = state.drafts;
  const ops = [];
  const matches = (generationId, field, itemId, draft) => Boolean(saved && saved.generationId === generationId && saved.field === field && saved.itemId === itemId && saved.draftValue === draft.value);
  const dropped = (generationId, field, itemId) => {
    drafts = setDraft(drafts, generationId, field, itemId, null);
    ops.push({ op: "review", generationId, field, itemId });
  };
  const marked = (generationId, field, itemId, draft, code) => {
    if ((draft.conflictCode || null) !== code) drafts = setDraft(drafts, generationId, field, itemId, { ...draft, conflictCode: code });
  };
  for (const [generationId, bucket] of Object.entries(state.drafts)) {
    const generation = generationsFor(transcription).find((item) => item.id === generationId);
    for (const [segmentId, draft] of Object.entries(bucket.segmentText)) {
      const segment = generation && generation.segments.find((item) => item.id === segmentId);
      if (matches(generationId, "segmentText", segmentId, draft) || (segment && segment.text === draft.value)) dropped(generationId, "segmentText", segmentId);
      else if (segment && segment.text !== draft.baseValue) marked(generationId, "segmentText", segmentId, draft, "source_version_changed");
      else if (segment && draft.conflictCode === "source_version_changed") marked(generationId, "segmentText", segmentId, draft, null);
    }
    for (const [speakerId, draft] of Object.entries(bucket.speakerName)) {
      const speaker = generation && generation.speakers.find((item) => item.speaker_id === speakerId);
      if (matches(generationId, "speakerName", speakerId, draft) || (speaker && speaker.display_name === draft.value)) dropped(generationId, "speakerName", speakerId);
      else if (speaker && speaker.display_name !== draft.baseValue) marked(generationId, "speakerName", speakerId, draft, "source_version_changed");
      else if (speaker && draft.conflictCode === "source_version_changed") marked(generationId, "speakerName", speakerId, draft, null);
    }
    for (const [segmentId, draft] of Object.entries(bucket.speakerAssignment)) {
      const segment = generation && generation.segments.find((item) => item.id === segmentId);
      const advanced = generation && Number(generation.source_version) > (Number(draft.baseSourceVersion) || 0);
      if (matches(generationId, "speakerAssignment", segmentId, draft) || (segment && (segment.speaker_id || "") === draft.value)) dropped(generationId, "speakerAssignment", segmentId);
      else if (draft.conflictCode === "current_generation_changed") continue; // the choice stays attached to its historical generation for review
      else if (segment && draft.conflictCode && draft.conflictCode !== "source_version_changed") {
        if (advanced) marked(generationId, "speakerAssignment", segmentId, draft, null);
      } else if (segment && advanced) marked(generationId, "speakerAssignment", segmentId, draft, null);
    }
    const current = transcription && transcription.generation;
    for (const [itemId, draft] of Object.entries(bucket.speakerAddName)) {
      const otherCurrent = current && current.id !== generationId;
      const advanced = current && current.id === generationId && Number(current.source_version) > (Number(draft.baseSourceVersion) || 0);
      if (CONFLICTS.includes(draft.conflictCode) && (otherCurrent || advanced)) marked(generationId, "speakerAddName", itemId, draft, null);
    }
  }
  return drafts === state.drafts ? state : emit({ ...state, drafts }, ...ops);
}

// ---- the panel above the transcript ----

function setError(state, meetingId, generationId, message, reloadLatest) {
  return emit({ ...state, error: { meetingId, generationId, message, reloadLatest } }, { op: "error" });
}

function clearError(state, generationId) {
  if (!state.error || (generationId && state.error.generationId !== generationId)) return state;
  return emit({ ...state, error: null }, { op: "error" });
}

const CONFLICT_NOTICE = {
  source_version_changed: t("Transcript đã thay đổi. Bản nháp vẫn được giữ để xem lại."),
  current_generation_changed: t("Phiên bản transcript đã đổi. Bản nháp vẫn được giữ để sao chép."),
};

function showConflictNotice(state, shown, generationId, code) {
  const message = code === "source_version_changed" ? CONFLICT_NOTICE.source_version_changed : CONFLICT_NOTICE.current_generation_changed;
  return shown && shown.id ? setError(state, shown.id, generationId, message, true) : state;
}

// A closed row or chip cannot hold the reload (a read row keeps two tab stops), so a conflict still waiting on it
// moves to the panel when its editor closes, and back beside the editor's buttons when it reopens.
function placeLatestReload(state, shown, generationId, field, itemId, currentValue, editorOpen) {
  const draft = draftOf(state.drafts, generationId, field, itemId);
  if (!awaitsLatestReload(draft, currentValue)) return state;
  return editorOpen ? clearError(state, generationId) : showConflictNotice(state, shown, generationId, draft.conflictCode);
}

// Swap one row (or chip) between read mode and editor.
function replaceRow(state, shown, generationId, segmentId) {
  const { generation, editable } = displayed(state, transcriptionOf(shown));
  if (!generation || generation.id !== generationId) return state;
  const segment = generation.segments.find((item) => item.id === segmentId);
  if (!segment) return state;
  const next = emit(state, { op: "row", generationId, segmentId });
  return editable ? placeLatestReload(next, shown, generationId, "segmentText", segmentId, segment.text, isEditing(next, generationId, segmentId)) : next;
}

function replaceChip(state, shown, generationId, speakerId) {
  const { generation, editable } = displayed(state, transcriptionOf(shown));
  if (!generation || generation.id !== generationId) return state;
  const speaker = generation.speakers.find((item) => item.speaker_id === speakerId);
  if (!speaker) return state;
  const next = emit(state, { op: "chip", generationId, speakerId });
  return editable ? placeLatestReload(next, shown, generationId, "speakerName", speakerId, speaker.display_name, isRenaming(next, generationId, speakerId)) : next;
}

// ---- status lines ----

// The control whose status line reports a write, when that line is on the page: for another meeting it is not.
function statusTarget(shown, descriptor) {
  return descriptor && shown && shown.id === descriptor.meetingId ? descriptor : null;
}

// Whether that line sits in an editor that is open now (an unfinished row or chip has none).
function statusOpen(state, shown, descriptor) {
  if (!statusTarget(shown, descriptor)) return false;
  const { generation, editable } = displayed(state, transcriptionOf(shown));
  if (descriptor.kind === "speaker-add") return true;
  if (!generation || generation.id !== descriptor.generationId || !editable) return false;
  if (descriptor.kind === "segment-text") return isEditing(state, descriptor.generationId, descriptor.segmentId);
  if (descriptor.kind === "speaker-name") return isRenaming(state, descriptor.generationId, descriptor.speakerId);
  return false;
}

// Write a result: the line beside the control, and the shared line (which keeps the saved words for another meeting).
function report(state, shown, target, message, isError = false) {
  const reachable = statusTarget(shown, target);
  const sameMeeting = !target || !shown || !shown.meeting || target.meetingId === shown.id;
  const next = { ...state, notice: isError || sameMeeting ? message : SAVED, noticeRev: state.noticeRev + 1 };
  return reachable && reachable.kind !== "speaker-assignment" ? emit(next, { op: "status", target: reachable, text: message, error: isError }) : next;
}

const clearStatus = (state, target) => emit(state, { op: "status", target, error: false });

function mutationMessage(failure, fallback) {
  if (failure.code === "source_version_changed") return t("Transcript đã thay đổi. Bản nháp vẫn được giữ; hãy xem lại trước khi lưu lần nữa.");
  if (failure.code === "current_generation_changed") return t("Phiên bản này không còn là bản hiện tại. Bản nháp vẫn được giữ để xem lại và thêm vào transcript hiện tại.");
  return failure.message || fallback;
}

// ---- the write on the wire ----

const enc = encodeURIComponent;

export function speakerAddReady(state, generation, draft, blocked) {
  return Boolean(generation && generation.status === "complete" && !blocked && !(draft && draft.conflictCode) && !state.mutation && !state.reload);
}

// Start a write. The two descriptors say which control had focus and which one reports the result.
function begin(state, action, spec) {
  const { shown } = action;
  const id = state.ids + 1;
  const mutation = {
    id,
    kind: spec.kind,
    meetingId: shown.id,
    generationId: spec.generationId,
    field: spec.field,
    itemId: spec.itemId,
    draftValue: spec.draftValue,
    requested: spec.requested,
    meeting: shown.meeting,
    focus: withMeeting(action.focus, shown),
    status: withMeeting(action.status, shown),
    after: null,
  };
  const meta = { id, kind: spec.kind, meetingId: shown.id, generationId: spec.generationId, field: spec.field, itemId: spec.itemId, draftValue: spec.draftValue };
  const effect = {
    type: "fetch",
    key: KEY.mutation,
    url: spec.url,
    init: {
      method: spec.method,
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(spec.body),
    },
    ok: "transcript/mutation-succeeded",
    error: "transcript/mutation-failed",
    meta,
  };
  return out({ ...state, ids: id, mutation, notice: SAVING }, [effect]);
}

// The write ended: the shared line stops saying "saving", and focus returns to where the user was (or to the control
// the result moved to) once the view has painted.
function settle(state, shown) {
  const { mutation } = state;
  const sameMeeting = !mutation.focus || Boolean(shown && shown.id && mutation.focus.meetingId === shown.id);
  const notice = state.notice && state.notice.text === SAVING.text ? SAVED : state.notice;
  return {
    ...state,
    mutation: null,
    notice,
    focus: { rev: state.focus.rev + 1, mode: "settle", target: mutation.after, from: mutation.focus, sameMeeting },
  };
}

const focusNow = (state, target, extra = {}) => ({ ...state, focus: { rev: state.focus.rev + 1, mode: "now", target, from: null, sameMeeting: true, ...extra } });

// ---- the segment editor and the speaker rename ----

function openEditor(state, action) {
  const { shown } = action;
  const { generation, editable } = displayed(state, transcriptionOf(shown));
  if (!editable || (shown && shown.blocked) || state.mutation) return state;
  const previous = state.editing;
  let next = { ...state, editing: { generationId: generation.id, segmentId: action.segmentId } };
  // One editor at a time: the row it leaves keeps its unsaved text in the draft store.
  if (previous && previous.segmentId !== action.segmentId) next = replaceRow(next, shown, previous.generationId, previous.segmentId);
  next = replaceRow(next, shown, generation.id, action.segmentId);
  return focusNow(next, { kind: "segment-editor", generationId: generation.id, segmentId: action.segmentId }, { caret: "end" });
}

// Leave the editor. `discard` is the Hủy path: it drops the typed text, unless that text is a conflict-marked draft.
function closeEditor(state, shown, generationId, segmentId, discard = false) {
  let next = isEditing(state, generationId, segmentId) ? { ...state, editing: null } : state;
  if (discard && !isConflicted(next, generationId, "segmentText", segmentId)) {
    next = remember(next, transcriptionOf(shown), generationId, "segmentText", segmentId, fieldValue(transcriptionOf(shown), generationId, "segmentText", segmentId)).state;
  }
  return replaceRow(next, shown, generationId, segmentId);
}

function cancelEditor(state, action) {
  const { editing } = state;
  if (!editing || editing.segmentId !== action.segmentId || state.mutation) return state;
  const next = closeEditor(state, action.shown, editing.generationId, action.segmentId, true);
  return focusNow(next, { kind: "segment-edit-button", generationId: editing.generationId, segmentId: action.segmentId, orTimestamp: true });
}

function openRename(state, action) {
  const { shown } = action;
  const { generation, editable } = displayed(state, transcriptionOf(shown));
  if (!editable || (shown && shown.blocked) || state.mutation) return state;
  const previous = state.renaming;
  let next = { ...state, renaming: { generationId: generation.id, speakerId: action.speakerId } };
  if (previous && previous.speakerId !== action.speakerId) next = replaceChip(next, shown, previous.generationId, previous.speakerId);
  next = replaceChip(next, shown, generation.id, action.speakerId);
  return focusNow(next, { kind: "speaker-rename-input", generationId: generation.id, speakerId: action.speakerId }, { caret: "select" });
}

function closeRename(state, shown, generationId, speakerId, discard = false) {
  let next = isRenaming(state, generationId, speakerId) ? { ...state, renaming: null } : state;
  if (discard && !isConflicted(next, generationId, "speakerName", speakerId)) {
    next = remember(next, transcriptionOf(shown), generationId, "speakerName", speakerId, fieldValue(transcriptionOf(shown), generationId, "speakerName", speakerId)).state;
  }
  return replaceChip(next, shown, generationId, speakerId);
}

function cancelRename(state, action) {
  const { renaming } = state;
  if (!renaming || renaming.speakerId !== action.speakerId || state.mutation) return state;
  const next = closeRename(state, action.shown, renaming.generationId, action.speakerId, true);
  return focusNow(next, { kind: "speaker-chip", generationId: renaming.generationId, speakerId: action.speakerId, enabledOnly: true });
}

// ---- saving ----

const currentOf = (shown) => {
  const transcription = transcriptionOf(shown);
  return transcription && transcription.generation;
};

function saveSegmentText(state, action) {
  const { shown } = action;
  const generation = currentOf(shown);
  const segment = generation && generation.segments.find((item) => item.id === action.segmentId);
  if (!shown || !shown.meeting || shown.blocked || !generation || generation.id !== action.generationId || !segment || state.mutation) return out(state);
  const target = withMeeting(action.status, shown);
  let next = clearStatus(state, target);
  const remembered = remember(next, transcriptionOf(shown), generation.id, "segmentText", segment.id, action.text);
  next = remembered.state;
  if (segment.text === action.text) {
    next = closeEditor(next, shown, generation.id, segment.id);
    next = report(next, shown, null, t("Không có thay đổi."));
    return out(focusNow(next, { kind: "segment-edit-button", generationId: generation.id, segmentId: segment.id }));
  }
  if (remembered.draft && remembered.draft.conflictCode === "source_version_changed") {
    return out(report(next, shown, target, t("Transcript đã thay đổi. Xem bản mới nhất rồi chỉnh bản nháp trước khi lưu."), true));
  }
  return begin(next, action, {
    kind: "segmentText",
    generationId: generation.id,
    field: "segmentText",
    itemId: segment.id,
    draftValue: action.text,
    method: "PATCH",
    url: `/api/meetings/${enc(shown.id)}/transcript-segments/${enc(segment.id)}/text`,
    body: { expected_generation_id: generation.id, expected_source_version: generation.source_version, text: action.text },
  });
}

function saveSpeakerName(state, action) {
  const { shown } = action;
  const generation = currentOf(shown);
  const speaker = generation && generation.speakers.find((item) => item.speaker_id === action.speakerId);
  if (!shown || !shown.meeting || shown.blocked || !generation || generation.id !== action.generationId || !speaker || state.mutation) return out(state);
  const target = withMeeting(action.status, shown);
  let next = clearStatus(state, target);
  const remembered = remember(next, transcriptionOf(shown), generation.id, "speakerName", action.speakerId, action.name, Boolean(action.reviewed));
  next = remembered.state;
  if (remembered.draft && remembered.draft.conflictCode === "source_version_changed") {
    return out(emit(next, { op: "status", target, text: t("Tên người nói đã thay đổi. Xem tên mới nhất rồi chỉnh bản nháp trước khi lưu."), error: true }));
  }
  return begin(next, action, {
    kind: "speakerName",
    generationId: generation.id,
    field: "speakerName",
    itemId: action.speakerId,
    draftValue: action.name,
    method: "PATCH",
    url: `/api/meetings/${enc(shown.id)}/transcript-generations/${enc(generation.id)}/speakers/${enc(action.speakerId)}`,
    body: { expected_source_version: generation.source_version, display_name: action.name },
  });
}

function saveAssignment(state, action) {
  const { shown } = action;
  const generation = currentOf(shown);
  const segment = generation && generation.segments.find((item) => item.id === action.segmentId);
  if (!shown || !shown.meeting || shown.blocked || !generation || generation.id !== action.generationId || !segment || state.mutation) return out(state);
  const remembered = remember(state, transcriptionOf(shown), generation.id, "speakerAssignment", action.segmentId, action.value, Boolean(action.reviewed));
  let next = remembered.state;
  if (remembered.draft && remembered.draft.conflictCode && !action.reviewed) {
    return out(emit(next, { op: "assignment-note", generationId: generation.id, segmentId: action.segmentId, text: t("Tải bản mới nhất để xem lựa chọn rồi áp dụng lại.") }));
  }
  if ((segment.speaker_id || "") === action.value) {
    return out(emit(next, { op: "recovery" }));
  }
  return begin(next, action, {
    kind: "speakerAssignment",
    generationId: generation.id,
    field: "speakerAssignment",
    itemId: action.segmentId,
    draftValue: action.value,
    requested: action.value,
    method: "PATCH",
    url: `/api/meetings/${enc(shown.id)}/transcript-segments/${enc(action.segmentId)}/speaker`,
    body: {
      expected_generation_id: generation.id,
      expected_source_version: generation.source_version,
      expected_assignment_version: Number(action.assignmentVersion),
      speaker_id: action.value || null,
    },
  });
}

function addSpeaker(state, action, reapply) {
  const { shown } = action;
  const generation = currentOf(shown);
  if (!shown || !shown.meeting) return out(state);
  const origin = reapply ? action.generationId : generation && generation.id;
  const draft = origin ? draftOf(state.drafts, origin, "speakerAddName", ADD_ITEM) : null;
  if (!speakerAddReady(state, generation, draft, shown.blocked)) return out(state);
  if (reapply && !draft) return out(state);
  const name = reapply ? draft.value : action.name;
  let next = state;
  if (reapply) {
    // Applied again to the current transcript: the draft starts over from this version, unmarked.
    next = { ...next, drafts: setDraft(next.drafts, origin, "speakerAddName", ADD_ITEM, { ...draft, baseSourceVersion: Number(generation.source_version) || 0, conflictCode: null }) };
  } else {
    next = rememberSpeakerAdd(next, transcriptionOf(shown), generation.id, name);
  }
  next = clearStatus(next, withMeeting({ kind: "speaker-add", control: "submit" }, shown));
  return begin(next, action, {
    kind: reapply ? "speakerReapply" : "speakerAdd",
    generationId: generation.id,
    field: "speakerAddName",
    itemId: origin,
    draftValue: name,
    requested: name,
    method: "POST",
    url: `/api/meetings/${enc(shown.id)}/transcript-generations/${enc(generation.id)}/speakers`,
    body: { expected_source_version: generation.source_version, display_name: name },
  });
}

// ---- answers ----

const saved = (mutation) => ({ generationId: mutation.generationId, field: mutation.field, itemId: mutation.itemId, draftValue: mutation.draftValue });

// What every accepted field write does first: reconcile the drafts with the saved value, and repaint what the
// page did not get from the editor that closes (a meeting refreshed meanwhile).
function landed(state, action, mutation) {
  const { shown } = action;
  const answer = answerOf(action.meta, action.response.body);
  const result = applyFieldAnswer(shown.meeting, answer);
  let next = reconcile(state, result.transcription, saved(mutation));
  if (result.transcription) next = emit(next, { op: "recovery" });
  const refreshed = result.applied && shown.meeting !== mutation.meeting && (result.bumped || result.changed);
  if (refreshed && shown.view === "meeting" && next.selected === result.generation.id) {
    const now = result.transcription.generation;
    const editable = Boolean(now && now.id === result.generation.id && result.generation.status === "complete");
    if (editable && mutation.field === "speakerName") next = emit(next, { op: "labels", generationId: result.generation.id, speakerId: mutation.itemId });
    else if (editable) next = emit(next, { op: "key", generationId: result.generation.id });
    else next = emit(next, { op: "generation", editable: false });
  }
  return { state: next, result };
}

function succeeded(state, action) {
  const { mutation } = state;
  if (!mutation || action.meta.id !== mutation.id) return out(state);
  const { shown } = action;
  // A 2xx body that is no object is no saved value: the write failed, the draft stays (the old code threw on it).
  if (!isRecord(action.response.body)) return failed(state, { ...action, error: null, malformed: true });
  if (!shown || !shown.meeting) {
    // The meeting is gone: the saved draft goes as the old success path dropped it (an added name has no matching value).
    const gone = mutation.field === "speakerAddName" ? { ...state, drafts: setDraft(state.drafts, mutation.itemId, "speakerAddName", ADD_ITEM, null) } : reconcile(state, null, saved(mutation));
    return out(settle(gone, shown));
  }
  const sameGeneration = (result) => Boolean(result.generation);
  let next = state;
  const target = mutation.status;
  if (mutation.kind === "segmentText") {
    const landing = landed(next, action, mutation);
    next = landing.state;
    if (sameGeneration(landing.result)) {
      const reachable = statusTarget(shown, target);
      if (reachable) next = emit(next, { op: "clear-reload", target: reachable });
      next = emit(next, { op: "key", generationId: landing.result.generation.id });
    }
    // The saved text is back in read mode; focus follows to its Sửa button once the save settles.
    next = closeEditor(next, shown, mutation.generationId, mutation.itemId);
    next = { ...next, mutation: { ...next.mutation, after: { kind: "segment-edit-button", generationId: mutation.generationId, segmentId: mutation.itemId } } };
    next = report(next, shown, target, t("Đã lưu."));
  } else if (mutation.kind === "speakerName") {
    const landing = landed(next, action, mutation);
    next = landing.state;
    const { result } = landing;
    if (sameGeneration(result)) {
      const reachable = statusTarget(shown, target);
      if (reachable) next = emit(next, { op: "clear-reload", target: reachable });
      const speaker = result.generation.speakers.find((item) => item.speaker_id === mutation.itemId);
      if (speaker && next.selected === result.generation.id) next = emit(next, { op: "labels", generationId: result.generation.id, speakerId: mutation.itemId });
    }
    next = closeRename(next, shown, mutation.generationId, mutation.itemId);
    next = { ...next, mutation: { ...next.mutation, after: { kind: "speaker-chip", generationId: mutation.generationId, speakerId: mutation.itemId } } };
    next = report(next, shown, target, t("Đã lưu tên người nói."));
  } else if (mutation.kind === "speakerAssignment") {
    const landing = landed(next, action, mutation);
    next = landing.state;
    const segment = landing.result.generation && landing.result.generation.segments.find((item) => item.id === mutation.itemId);
    if (!segment || !landing.result.transcription) return out(settle(next, shown));
    next = emit(next, { op: "assignment-synced", generationId: mutation.generationId, segmentId: mutation.itemId });
    if (shown.view === "meeting" && next.selected === mutation.generationId) {
      next = emit(next, { op: "key", generationId: mutation.generationId });
      next = clearError(next, mutation.generationId);
    }
    next = report(next, shown, target, t("Đã lưu lựa chọn người nói."));
  } else {
    next = speakerAdded(next, action, mutation);
  }
  return out(settle(next, shown));
}

// A speaker the server added (or added again from a draft): put into the generation it names.
function speakerAdded(state, action, mutation) {
  const { shown } = action;
  const answer = answerOf(action.meta, action.response.body);
  const result = applySpeakerAdded(shown.meeting, answer);
  let next = state;
  const reapply = mutation.kind === "speakerReapply";
  if (result.applied && shown.view === "meeting" && next.selected === result.generation.id) {
    const now = result.transcription.generation;
    const editable = reapply || Boolean(now && now.id === result.generation.id && result.generation.status === "complete");
    next = emit(next, { op: "generation", editable });
    if (editable) next = { ...next, mutation: { ...next.mutation, after: { kind: "speaker-chip", generationId: result.generation.id, speakerId: answer.speaker.speaker_id } } };
  }
  const origin = reapply ? mutation.itemId : mutation.generationId;
  next = { ...next, drafts: setDraft(next.drafts, origin, "speakerAddName", ADD_ITEM, null) };
  if (next.addName === mutation.draftValue) next = { ...next, addName: "" };
  next = emit(next, { op: "recovery" });
  return report(next, shown, mutation.status, t("Đã thêm người nói."));
}

function failed(state, action) {
  const { mutation } = state;
  if (!mutation || action.meta.id !== mutation.id) return out(state);
  const { shown } = action;
  const failure = action.malformed ? { api: false, message: null, code: "" } : failureOf(action.error);
  const target = mutation.status;
  let next = state;
  if (mutation.kind === "segmentText" || mutation.kind === "speakerName") {
    const isText = mutation.kind === "segmentText";
    next = report(next, shown, target, mutationMessage(failure, isText ? t("Không thể lưu đoạn transcript.") : t("Không thể lưu tên người nói.")), true);
    const code = CONFLICTS.includes(failure.code) ? failure.code : null;
    if (code) {
      next = withConflict(next, mutation.generationId, mutation.field, mutation.itemId, code);
      if (statusOpen(next, shown, target)) next = emit(next, { op: "reload-button", target, generationId: mutation.generationId });
      else next = showConflictNotice(next, shown, mutation.generationId, code);
    }
  } else if (mutation.kind === "speakerAssignment") {
    const message = mutationMessage(failure, t("Không thể lưu người nói."));
    const isConflict = CONFLICTS.includes(failure.code) || failure.code === "assignment_version_changed";
    const now = currentOf(shown);
    const sameMeeting = Boolean(shown && shown.id === mutation.meetingId);
    const currentGeneration = sameMeeting && now;
    if (isConflict) {
      next = withConflict(next, mutation.generationId, "speakerAssignment", mutation.itemId, failure.code);
      next = emit(next, { op: "assignment-failed", generationId: mutation.generationId, segmentId: mutation.itemId, conflict: true, value: mutation.requested });
    } else {
      next = { ...next, drafts: setDraft(next.drafts, mutation.generationId, "speakerAssignment", mutation.itemId, null) };
      next = emit(next, { op: "assignment-failed", generationId: mutation.generationId, segmentId: mutation.itemId, conflict: false });
    }
    next = setError(next, mutation.meetingId, mutation.generationId, message, isConflict);
    next = report(next, shown, target, message, true);
    if (isConflict && currentGeneration && currentGeneration.id === mutation.generationId && next.selected === mutation.generationId) {
      next = emit(next, { op: "generation", editable: true, force: true });
    }
    next = emit(next, { op: "recovery" });
  } else {
    next = report(next, shown, target, mutationMessage(failure, t("Không thể thêm người nói.")), true);
    const code = CONFLICTS.includes(failure.code) ? failure.code : null;
    const origin = mutation.kind === "speakerReapply" ? mutation.itemId : mutation.generationId;
    if (code) next = withConflict(next, origin, "speakerAddName", ADD_ITEM, code);
    if (code && statusTarget(shown, target)) next = emit(next, { op: "reload-button", target, generationId: mutation.generationId });
    next = emit(next, { op: "recovery" });
  }
  return out(settle(next, shown));
}

// ---- reload ----

const RELOAD_MISSING = t("Không tải được trạng thái transcript mới nhất.");
const RELOAD_FAILED = t("Không thể tải transcript mới nhất.");

function reloadClicked(state, action) {
  const { shown } = action;
  if (!shown || !shown.id || state.reload || state.mutation) return out(state);
  const id = state.ids + 1;
  const reload = { id, meetingId: shown.id, installRev: shown.installRev };
  const effect = {
    type: "fetch",
    key: KEY.reload,
    url: `/api/meetings/${enc(shown.id)}`,
    init: { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" },
    ok: "transcript/reload-loaded",
    error: "transcript/reload-failed",
    meta: { id, meetingId: shown.id, installRev: shown.installRev, anchor: action.anchor || null },
  };
  return out({ ...state, ids: id, reload }, [effect]);
}

function reloadLoaded(state, action) {
  if (!state.reload || action.meta.id !== state.reload.id) return out(state);
  const next = { ...state, reload: null };
  const { shown } = action;
  // Another copy was installed while the read was on the wire: this answer is not the latest any more.
  if (!shown || !shown.meeting || shown.installRev !== action.meta.installRev) return out(next);
  const body = action.response.body;
  const latest = body && body.meeting && typeof body.meeting === "object" ? body.meeting : body;
  if (!latest || latest.id !== action.meta.meetingId || !latest.transcription) {
    return out(emit(next, { op: "reload-note", anchor: action.meta.anchor, text: RELOAD_MISSING }));
  }
  return out(repaint(emit({ ...next, error: null }, { op: "clear-reload-all" }, { op: "generation", force: true })));
}

function reloadFailed(state, action) {
  if (!state.reload || action.meta.id !== state.reload.id) return out(state);
  const failure = failureOf(action.error, RELOAD_FAILED);
  return out(emit({ ...state, reload: null }, { op: "reload-note", anchor: action.meta.anchor, text: failure.message || RELOAD_FAILED }));
}

// ---- the reducer ----

export function reduce(state, action) {
  const synchronized = action.shown !== undefined ? synced(state, transcriptionOf(action.shown)) : state;
  const result = step(synchronized, action);
  return result.state === state && !result.effects.length ? out(state) : result;
}

function step(state, action) {
  const { shown } = action;
  switch (action.type) {
    case "transcript/transcription-shown":
      return out(state);
    case "transcript/meeting-installed": {
      const installed = shown ? shown.installRev : state.installed;
      const next = reconcile({ ...state, installed }, transcriptionOf(shown));
      return out(repaint(next));
    }
    case "transcript/meeting-switched":
      return out(repaint({ ...state, selected: null, lastCurrent: null, error: null, editing: null, renaming: null, addName: "", ops: [] }));
    case "transcript/painted":
      return out(state.ops.some((op) => op.n <= action.through) ? { ...state, ops: state.ops.filter((op) => op.n > action.through) } : state);

    case "transcript/generation-chosen":
      return out(repaint({ ...state, selected: action.id, addName: state.selected !== action.id ? "" : state.addName }));
    case "transcript/segment-edit-opened":
      return out(openEditor(state, action));
    case "transcript/segment-edit-cancelled":
      return out(cancelEditor(state, action));
    case "transcript/speaker-rename-opened":
      return out(openRename(state, action));
    case "transcript/speaker-rename-cancelled":
      return out(cancelRename(state, action));
    case "transcript/add-name-typed":
      return out(state.addName === action.value ? state : { ...state, addName: action.value });
    case "transcript/draft-typed":
      return out(remember(state, transcriptionOf(shown), action.generationId, action.field, action.itemId, action.value, true).state);

    case "transcript/segment-saved":
      return saveSegmentText(state, action);
    case "transcript/speaker-name-saved":
      return saveSpeakerName(state, action);
    case "transcript/assignment-chosen":
      return saveAssignment(state, action);
    case "transcript/speaker-add-submitted":
      return addSpeaker(state, action, false);
    case "transcript/speaker-add-reapplied":
      return addSpeaker(state, action, true);
    case "transcript/mutation-succeeded":
      return succeeded(state, action);
    case "transcript/mutation-failed":
      return failed(state, action);

    case "transcript/reload-clicked":
      return reloadClicked(state, action);
    case "transcript/reload-loaded":
      return reloadLoaded(state, action);
    case "transcript/reload-failed":
      return reloadFailed(state, action);

    case "transcript/copy-clicked": {
      const draft = draftOf(state.drafts, action.generationId, action.field, action.itemId);
      if (!draft) return out(state);
      const target = { generationId: action.generationId, field: action.field, itemId: action.itemId };
      return out(state, [{ type: "call", key: KEY.copy, name: "transcript.copy", args: [draft.value, target], ok: "transcript/copy-finished", meta: target }]);
    }
    case "transcript/copy-finished":
      return out(emit(state, { op: "copy-status", ...action.meta, how: action.value }));
    default:
      return out(state);
  }
}
