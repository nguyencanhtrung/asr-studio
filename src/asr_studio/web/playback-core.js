// Pure core of the playback of the open meeting (phase 4d of #118): which transcript rows play at the recording time,
// whether the list follows them, the user's manual scrolling that pauses the follow, the row a notes source names
// and the seek to a notes source that may have to wait for the meeting's audio. No DOM, fetch, timer, storage,
// clock, <audio> or randomness: node --test loads this file directly. Contract:
// docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design.md §11.11.
//
// The slice `playback` shares the page store with `history`, `task` and `transcript`. What only the page knows enters
// through actions: the clock (`now`, from performance.now()), the audio facts (`time`, `paused`, `ended`, `ready`
// for "metadata loaded", `meetingId` of the loaded audio), two layout facts (`visible`: some of the list shows below
// the player bar; `typing`: focus is in a transcript field) and `reduced` (prefers-reduced-motion). The rows come
// from `shown.rows`, the segments of the generation whose list is on the page (null until it is painted).
//
// The slice cannot touch the page, so what it wants done is data: `ops` are paint commands for the view, run in
// order and acknowledged with `playback/painted` (same idea as the `ops` of transcript-core); the audio seek is an
// effect `call playback.seek`; the wait for the audio metadata is a `schedule` effect.

const MANUAL_SCROLL_QUIET_MS = 2000; // a manual scroll this recent keeps the list from following
const OWN_SCROLL_GAP_MS = 200; // a scroll event this soon after our scroll, or its last event, is ours
const SEEK_TIMEOUT_MS = 5000; // how long a seek waits for the audio metadata
const SCROLL_KEYS = new Set([" ", "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"]);
// Each waiter has its own metadata deadline, as each call of the old seekNotesSource had its own timer.
const KEY = { seek: "playback/seek", timeout: (id) => `playback/seek-timeout/${id}` };

export const initialState = {
  time: null, // the recording time of the last sample
  active: [], // ids of the rows playing at that time; the first one is the current row
  current: null, // id of the current row, null in a gap between rows
  lastManual: null, // clock of the last scroll the user made (null: none yet)
  ownUntil: 0, // a scroll event up to this clock is ours, not the user's
  mark: null, // id of the row the open notes source names
  seek: null, // the seek waiting for the audio metadata: { id, ids, meetingId, startSeconds, generationId }; ids: every seek joined to it
  seekDone: null, // { id, ids, ok }: how the last seek ended; ok holds for every id in ids
  ops: [], // paint commands not acknowledged yet: { n, op: scroll | reveal, id, smooth }
  opSeq: 0,
  rev: 0, // the rows are due a repaint
};

const out = (state, effects = []) => ({ state, effects });
const bumped = (state) => ({ ...state, rev: state.rev + 1 });

export const isScrollKey = (key) => SCROLL_KEYS.has(key);

// A notes source the player can be sent to.
export function validSource(source) {
  return Boolean(source)
    && typeof source.meetingId === "string" && source.meetingId !== ""
    && typeof source.snapshotId === "string" && source.snapshotId !== ""
    && typeof source.segmentId === "string" && source.segmentId !== ""
    && Number.isFinite(source.startSeconds) && source.startSeconds >= 0;
}

// Playing the recording needs its player on screen: the Transcript tab.
export const canPlay = (shown) => Boolean(shown) && shown.tab === "transcript";

const rowsOf = (shown) => (shown && shown.rows) || [];

// The rows playing at `seconds`: start <= t < end, the last row also at t == end, overlapping rows all count.
export function activeRows(rows, seconds) {
  if (!Number.isFinite(seconds)) return [];
  const ids = [];
  rows.forEach((row, index) => {
    const start = Number(row.start_seconds);
    const end = Number(row.end_seconds);
    if (seconds >= start && (seconds < end || (index === rows.length - 1 && seconds <= end))) ids.push(row.id);
  });
  return ids;
}

// The row a notes source names: its id, else the row spanning its time (a newer transcript has other ids).
export function sourceRow(rows, segmentId, startSeconds) {
  if (!segmentId) return null;
  const row = rows.find((item) => item.id === segmentId)
    || rows.find((item) => Number(item.start_seconds) <= startSeconds && startSeconds < Number(item.end_seconds));
  return row ? row.id : null;
}

// Whether the list scrolls to a newly playing row: playing, no manual scroll lately, some of the list below the
// player bar, and focus not in a transcript field.
const follows = (state, facts) => !facts.paused && !facts.ended
  && !(state.lastManual !== null && facts.now - state.lastManual < MANUAL_SCROLL_QUIET_MS)
  && Boolean(facts.visible) && !facts.typing;

function queue(state, op) {
  const n = state.opSeq + 1;
  return { ...state, opSeq: n, ops: [...state.ops, { n, ...op }] };
}

// The rows at the time of an audio fact; scrolls to the current row when it changed and the list follows.
function sample(state, action) {
  const active = activeRows(rowsOf(action.shown), action.time);
  const current = active.length ? active[0] : null;
  let next = bumped({ ...state, time: action.time, active, current });
  if (current !== null && current !== state.current && follows(state, action)) {
    next = queue({ ...next, ownUntil: action.now + OWN_SCROLL_GAP_MS }, { op: "scroll", id: current, smooth: !action.reduced });
  }
  return next;
}

const finishSeek = (state, ok) => ({ ...state, seek: null, seekDone: { id: state.seek.id, ids: state.seek.ids, ok } });
const stopWaits = (seek) => (seek ? seek.ids.map((id) => ({ type: "cancel", key: KEY.timeout(id) })) : []);
const wait = (id) => ({ type: "schedule", key: KEY.timeout(id), delay: SEEK_TIMEOUT_MS, action: { type: "playback/seek-timed-out", seekId: id } });

// A seek ends in the same step as it began unless the audio has no metadata yet.
function startSeek(state, action) {
  const { shown } = action;
  const here = Boolean(shown) && shown.view === "meeting" && shown.id === action.meetingId && action.audioMeetingId === action.meetingId;
  const seek = { id: action.seekId, ids: [action.seekId], meetingId: action.meetingId, startSeconds: action.startSeconds, generationId: shown ? shown.generationId : null };
  const open = state.seek;
  if (!here) return out({ ...state, seek: null, seekDone: { id: action.seekId, ids: [action.seekId], ok: false } }, stopWaits(open));
  // The same target as the seek in progress: join it, every waiter gets its result (or its own deadline first).
  if (open && open.meetingId === seek.meetingId && open.startSeconds === seek.startSeconds) {
    return out({ ...state, seek: { ...open, ids: [...open.ids, seek.id] } }, action.ready ? [] : [wait(seek.id)]);
  }
  const apply = {
    type: "call", key: KEY.seek, name: "playback.seek", args: [action.startSeconds],
    ok: "playback/seek-applied", error: "playback/seek-failed", meta: { seekId: seek.id },
  };
  if (action.ready) return out({ ...state, seek, seekDone: null }, [...stopWaits(open), apply]);
  return out({ ...state, seek, seekDone: null }, [...stopWaits(open), wait(seek.id)]);
}

// A seek waits for the audio of its own meeting and transcript: the user moving on drops it.
const moved = (seek, shown) => !shown || shown.id !== seek.meetingId || (seek.generationId !== null && shown.generationId !== seek.generationId);

export function reduce(state, action) {
  const dropped = state.seek && action.shown !== undefined && !action.type.startsWith("playback/seek-") && action.type !== "playback/meeting-left"
    && moved(state.seek, action.shown);
  if (!dropped) return step(state, action);
  const result = step(finishSeek(state, false), action);
  return { state: result.state, effects: [...stopWaits(state.seek), ...result.effects] };
}

function step(state, action) {
  switch (action.type) {
    case "playback/sampled":
      return out(sample(state, action));
    case "playback/metadata-loaded": {
      const next = sample(state, action);
      const seek = state.seek;
      if (!seek || action.audioMeetingId !== seek.meetingId) return out(next);
      return out(next, [
        ...stopWaits(seek),
        {
          type: "call", key: KEY.seek, name: "playback.seek", args: [seek.startSeconds],
          ok: "playback/seek-applied", error: "playback/seek-failed", meta: { seekId: seek.id },
        },
      ]);
    }
    case "playback/audio-failed":
      return state.seek ? out(finishSeek(state, false), stopWaits(state.seek)) : out(state);
    case "playback/seek-requested":
      return startSeek(state, action);
    case "playback/seek-timed-out": {
      // This waiter gives up; the seek goes on while others still wait.
      if (!state.seek || !state.seek.ids.includes(action.seekId)) return out(state);
      const ids = state.seek.ids.filter((id) => id !== action.seekId);
      return out(ids.length ? { ...state, seek: { ...state.seek, ids } } : finishSeek(state, false));
    }
    case "playback/seek-applied": {
      if (!state.seek || state.seek.id !== action.meta.seekId) return out(state); // an answer for a seek that was replaced
      return out(sample(finishSeek(state, true), { ...action.value, now: action.now, shown: action.shown }));
    }
    case "playback/seek-failed":
      return state.seek && state.seek.id === action.meta.seekId ? out(finishSeek(state, false)) : out(state);
    case "playback/meeting-left": {
      // The seek belongs to the meeting that was left; the mark only to a meeting we switched away from.
      const next = { ...state, seek: null, seekDone: null, mark: action.kind === "switch" ? null : state.mark };
      return out(bumped(next), stopWaits(state.seek));
    }
    case "playback/mark-requested": {
      const id = sourceRow(rowsOf(action.shown), action.segmentId, action.startSeconds);
      const next = bumped({ ...state, mark: id });
      return out(id === null ? next : queue(next, { op: "reveal", id, smooth: !action.reduced }));
    }
    case "playback/scroll-started":
      return out({ ...state, ownUntil: action.now + OWN_SCROLL_GAP_MS });
    case "playback/manual-scrolled":
      return out({ ...state, lastManual: action.now });
    case "playback/scrolled":
      // Our own smooth scroll keeps sending events, each holding the window open a little longer.
      return out(action.now <= state.ownUntil ? { ...state, ownUntil: action.now + OWN_SCROLL_GAP_MS } : { ...state, lastManual: action.now });
    case "playback/painted":
      return state.ops.some((op) => op.n <= action.through) ? out({ ...state, ops: state.ops.filter((op) => op.n > action.through) }) : out(state);
    default:
      return out(state);
  }
}
