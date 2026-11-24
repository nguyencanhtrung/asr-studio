import assert from "node:assert/strict";
import test from "node:test";
import { combine, createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  activeRows,
  canPlay,
  initialState,
  isScrollKey,
  reduce,
  sourceRow,
  validSource,
} from "../../src/asr_studio/web/playback-core.js";

const row = (id, start, end) => ({ id, start_seconds: start, end_seconds: end });
// r1 and r2 overlap; there is a gap between 10 and 12.
const ROWS = [row("r1", 0, 5), row("r2", 2, 6), row("r3", 6, 10), row("r4", 12, 20)];
const SOURCE = { meetingId: "m1", snapshotId: "n1", segmentId: "r3", startSeconds: 7 };

const tick = () => new Promise((resolve) => setImmediate(resolve));

// The page as the view wires it: the real store and runner over a fake audio element, a fake clock and fake timers,
// the facts of the page stamped on every action.
function setup({ rows = ROWS, view = "meeting" } = {}) {
  const timers = [];
  const seeks = [];
  const audio = { time: 0, paused: false, ended: false, ready: true, meetingId: "m1", visible: true, typing: false, reduced: false, failSeek: false };
  const page = { clock: 0, shown: { id: "m1", view, generationId: "g1", tab: "transcript", rows } };
  const facts = () => ({
    time: audio.time, paused: audio.paused, ended: audio.ended, ready: audio.ready, audioMeetingId: audio.meetingId,
    visible: audio.visible, typing: audio.typing, reduced: audio.reduced,
  });
  const stamp = (action) => ({ shown: page.shown, ...action, now: page.clock });
  const run = createRunner({
    setTimeout: (callback, delay) => timers.push({ callback, delay, live: true }) - 1,
    clearTimeout: (id) => { timers[id].live = false; },
    calls: {
      "playback.seek": (seconds) => {
        if (audio.failSeek) throw new Error("not seekable");
        seeks.push(seconds);
        audio.time = seconds;
        return facts();
      },
    },
  });
  const store = createStore({
    reduce: combine({ playback: reduce }),
    initialState: { playback: initialState },
    run: (effect, send) => run(effect, (action) => send(stamp(action))),
  });
  const send = (type, extra = {}) => store.dispatch(stamp({ type, ...extra }));
  const env = {
    audio,
    page,
    seeks,
    timers,
    state: () => store.getState().playback,
    sample(time, at) {
      if (time !== undefined) audio.time = time;
      if (at !== undefined) page.clock = at;
      send("playback/sampled", facts());
      return env.state();
    },
    // The paint commands since the last call, acknowledged as the view does.
    ops() {
      const taken = env.state().ops;
      if (taken.length) send("playback/painted", { through: taken.at(-1).n });
      return taken.map(({ n, ...op }) => op);
    },
    seek(source = SOURCE, seekId = "k1") {
      send("playback/seek-requested", { seekId, meetingId: source.meetingId, startSeconds: source.startSeconds, ...facts() });
      return env.state();
    },
    metadata() {
      send("playback/metadata-loaded", facts());
      return env.state();
    },
    live: () => timers.filter((timer) => timer.live),
    send,
  };
  return env;
}

// ---- the rows that play ----

test("the playing row follows the recording time: start inclusive, end exclusive, the last row's end inclusive", () => {
  const rows = [row("a", 0, 5), row("b", 5, 10)];
  assert.deepEqual(activeRows(rows, 0), ["a"]);
  assert.deepEqual(activeRows(rows, 4.99), ["a"]);
  assert.deepEqual(activeRows(rows, 5), ["b"]);
  assert.deepEqual(activeRows(rows, 10), ["b"], "the last row also at its end");
  assert.deepEqual(activeRows(rows, 10.01), []);
  assert.deepEqual(activeRows([row("a", 0, 5), row("b", 5, 10), row("c", 20, 30)], 10), [], "a middle row's end is exclusive");
  assert.deepEqual(activeRows(rows, -1), []);
  assert.deepEqual(activeRows(rows, NaN), []);
  assert.deepEqual(activeRows(rows, null), [], "no time is not time 0");
  assert.deepEqual(activeRows(rows, Infinity), []);
  assert.deepEqual(activeRows([], 1), []);
});

test("time moves the active row, a gap has none, and seeking back moves it back", () => {
  const env = setup();
  assert.equal(env.sample(0).current, "r1");
  assert.deepEqual(env.state().active, ["r1"]);
  assert.equal(env.sample(7).current, "r3");
  assert.deepEqual(env.state().active, ["r3"]);
  assert.equal(env.sample(11).current, null, "a gap between two rows");
  assert.deepEqual(env.state().active, []);
  assert.equal(env.sample(13).current, "r4");
  assert.equal(env.sample(1).current, "r1", "seeking back");
  assert.equal(env.state().time, 1);
});

test("rows that overlap are all active and the first is the current one", () => {
  const env = setup();
  const state = env.sample(3);
  assert.deepEqual(state.active, ["r1", "r2"]);
  assert.equal(state.current, "r1");
  assert.deepEqual(env.sample(5).active, ["r2"], "r1 ended at 5");
});

test("a sample is a new state every time, so the rows are painted again after a re-render", () => {
  const env = setup();
  const first = env.sample(1);
  const second = env.sample(1);
  assert.notEqual(first, second);
  assert.equal(second.rev, first.rev + 1);
});

test("without rows on the page (the list is not painted for this generation) nothing is active", () => {
  const env = setup({ rows: null });
  assert.deepEqual(env.sample(1).active, []);
});

// ---- following ----

test("a new current row scrolls the list while it plays, smoothly unless the user wants less motion", () => {
  const env = setup();
  env.sample(1);
  assert.deepEqual(env.ops(), [{ op: "scroll", id: "r1", smooth: true }]);
  env.sample(1.5);
  assert.deepEqual(env.ops(), [], "the same row again");
  env.audio.reduced = true;
  env.sample(7);
  assert.deepEqual(env.ops(), [{ op: "scroll", id: "r3", smooth: false }]);
});

test("the list does not follow when the audio is paused or ended, the list is out of view or focus is in a field", () => {
  for (const [name, change] of [
    ["paused", { paused: true }],
    ["ended", { ended: true }],
    ["list not below the player bar", { visible: false }],
    ["focus in a transcript field", { typing: true }],
  ]) {
    const env = setup();
    Object.assign(env.audio, change);
    env.sample(7);
    assert.deepEqual(env.ops(), [], name);
    assert.equal(env.state().current, "r3", `${name}: the active row still moves`);
  }
});

test("a gap does not scroll, and the row after it does", () => {
  const env = setup();
  env.sample(7);
  env.ops();
  env.sample(11);
  assert.deepEqual(env.ops(), []);
  env.sample(13);
  assert.deepEqual(env.ops(), [{ op: "scroll", id: "r4", smooth: true }]);
});

test("coming back to a row after the gap counts as a new row, so it scrolls again", () => {
  const env = setup();
  env.sample(13);
  env.ops();
  env.sample(11);
  env.sample(13);
  assert.equal(env.ops().length, 1);
});

test("the paint commands are numbered and stay until they are acknowledged", () => {
  const env = setup();
  env.sample(1);
  env.sample(7);
  assert.deepEqual(env.state().ops.map((op) => op.n), [1, 2]);
  env.send("playback/painted", { through: 1 });
  assert.deepEqual(env.state().ops.map((op) => op.n), [2]);
  env.send("playback/painted", { through: 2 });
  assert.deepEqual(env.state().ops, []);
});

// ---- manual scroll ----

test("a manual scroll pauses the follow for two seconds, then it resumes", () => {
  const env = setup();
  env.sample(1, 1000);
  env.ops();
  env.page.clock = 5000;
  env.send("playback/manual-scrolled");
  env.sample(7, 6999); // 1999 ms later
  assert.deepEqual(env.ops(), [], "paused");
  env.sample(13, 7000); // exactly 2000 ms later
  assert.deepEqual(env.ops(), [{ op: "scroll", id: "r4", smooth: true }], "resumed");
});

test("a scroll the page did not start is the user's, so it pauses the follow too", () => {
  const env = setup();
  env.page.clock = 100;
  env.send("playback/scrolled");
  assert.equal(env.state().lastManual, 100);
  env.sample(7, 1000);
  assert.deepEqual(env.ops(), []);
});

test("our own scroll and its trailing events are not manual, until the events stop", () => {
  const env = setup();
  env.sample(1, 1000); // the follow scrolls: our window is open until 1200
  env.ops();
  assert.equal(env.state().ownUntil, 1200);
  env.page.clock = 1100;
  env.send("playback/scrolled");
  assert.equal(env.state().lastManual, null, "inside the window");
  assert.equal(env.state().ownUntil, 1300, "each event holds the window open a little longer");
  env.page.clock = 1300;
  env.send("playback/scrolled");
  assert.equal(env.state().lastManual, null, "the edge of the window is still ours");
  assert.equal(env.state().ownUntil, 1500);
  env.page.clock = 1501;
  env.send("playback/scrolled");
  assert.equal(env.state().lastManual, 1501, "after the gap it is the user's");
  assert.equal(env.state().ownUntil, 1500, "the window did not move");
});

test("a scroll the app starts for a notes source also opens the own window", () => {
  const env = setup();
  env.page.clock = 400;
  env.send("playback/scroll-started");
  env.page.clock = 500;
  env.send("playback/scrolled");
  assert.equal(env.state().lastManual, null);
});

test("scroll keys are the ones that scroll a page", () => {
  for (const key of [" ", "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"]) assert.ok(isScrollKey(key), key);
  for (const key of ["a", "Enter", "ArrowLeft", "Tab", "Escape"]) assert.ok(!isScrollKey(key), key);
});

// ---- the notes source mark ----

test("the mark names the row of the source: its id first, else the row spanning its time", () => {
  assert.equal(sourceRow(ROWS, "r3", 0), "r3", "the id wins over the time");
  assert.equal(sourceRow(ROWS, "gone", 13), "r4", "a newer transcript has other ids");
  assert.equal(sourceRow(ROWS, "gone", 6), "r3", "start inclusive");
  assert.equal(sourceRow(ROWS, "gone", 20), null, "end exclusive");
  assert.equal(sourceRow(ROWS, "gone", 11), null, "a gap");
  assert.equal(sourceRow(ROWS, "", 1), null, "null clears");
  assert.equal(sourceRow(ROWS, null, 1), null);
  assert.equal(sourceRow([], "r1", 1), null);
});

test("marking a source sets the mark and asks to bring its row into view; null clears it", () => {
  const env = setup();
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7, reduced: false });
  assert.equal(env.state().mark, "r3");
  assert.deepEqual(env.ops(), [{ op: "reveal", id: "r3", smooth: true }]);
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7, reduced: true });
  assert.deepEqual(env.ops(), [{ op: "reveal", id: "r3", smooth: false }]);
  env.send("playback/mark-requested", { segmentId: null, startSeconds: null });
  assert.equal(env.state().mark, null);
  assert.deepEqual(env.ops(), []);
});

test("marking while the meeting is still loading finds no row, and the mark works once the list is painted", () => {
  const env = setup({ rows: null, view: "loading" });
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7 });
  assert.equal(env.state().mark, null, "no rows yet");
  assert.deepEqual(env.ops(), []);
  env.page.shown = { ...env.page.shown, view: "meeting", rows: ROWS };
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7 });
  assert.equal(env.state().mark, "r3");
});

test("marking a source repaints the rows", () => {
  const env = setup();
  const before = env.state().rev;
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7 });
  assert.equal(env.state().rev, before + 1);
});

test("closing an older source keeps a newer explicit seek target", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  for (const newerId of ["r2", "r4"]) {
    let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r2", startSeconds: 3, shown }).state;
    state = reduce(state, { type: "playback/target-requested", segmentId: newerId, startSeconds: newerId === "r2" ? 3 : 13, shown }).state;
    state = reduce(state, { type: "playback/mark-requested", segmentId: null, startSeconds: null, shown }).state;
    assert.equal(state.mark, null);
    assert.equal(state.target, newerId);
    assert.equal(state.targetOwner, "timestamp");
  }
});

test("closing Notes clears source-owned reveal and its pending seek", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r2", startSeconds: 3, shown }).state;
  state = reduce(state, {
    type: "playback/seek-requested", seekId: "source-seek", meetingId: "m1", segmentId: "r2",
    startSeconds: 3, audioMeetingId: "m1", ready: true, shown,
  }).state;
  state = reduce(state, {
    type: "playback/seek-applied", meta: { seekId: "source-seek" },
    value: { time: 3, paused: true }, shown,
  }).state;
  assert.equal(state.targetOwner, "source");
  assert.ok(state.pendingTarget);
  state = reduce(state, { type: "playback/mark-requested", segmentId: null, startSeconds: null, shown }).state;
  assert.equal(state.mark, null);
  assert.equal(state.target, null);
  assert.equal(state.targetOwner, null);
  assert.equal(state.pendingTarget.dismissed, true);
  assert.equal(state.seek, null);
  state = reduce(state, { type: "playback/seeking", shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, seeking: false, shown }).state;
  assert.equal(state.pendingTarget, null);
  assert.equal(state.target, null);
});

test("a newer timestamp cancels a source seek waiting for metadata", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r3", startSeconds: 7, shown }).state;
  state = reduce(state, {
    type: "playback/seek-requested", seekId: "source-seek", meetingId: "m1", segmentId: "r3",
    startSeconds: 7, audioMeetingId: "m1", ready: false, shown,
  }).state;
  assert.ok(state.seek);
  state = reduce(state, { type: "playback/target-requested", segmentId: "r4", startSeconds: 13, shown }).state;
  assert.equal(state.seek, null);
  assert.equal(state.target, "r4");
  assert.equal(state.targetOwner, "timestamp");
});

test("a newer row reuses the pending meeting scope with its own target identity", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r3", startSeconds: 7, shown }).state;
  state = reduce(state, {
    type: "playback/seek-requested", seekId: "source-seek", meetingId: "m1", segmentId: "r3",
    startSeconds: 7, audioMeetingId: "m1", ready: true, shown,
  }).state;
  state = reduce(state, {
    type: "playback/seek-applied", meta: { seekId: "source-seek" },
    value: { time: 7, paused: true }, shown,
  }).state;
  state = reduce(state, { type: "playback/target-requested", segmentId: "r4", startSeconds: 13, shown }).state;
  assert.equal(state.pendingTarget.id, "r4");
  assert.equal(state.pendingTarget.owner, "timestamp");
  assert.equal(state.pendingTarget.owedStarts, 2);
});

test("switching meetings clears the mark; going home keeps it", () => {
  const env = setup();
  env.send("playback/mark-requested", { segmentId: "r3", startSeconds: 7 });
  env.send("playback/meeting-left", { kind: "home" });
  assert.equal(env.state().mark, "r3");
  env.send("playback/meeting-left", { kind: "switch" });
  assert.equal(env.state().mark, null);
});

// ---- seeking to a notes source ----

test("a source, and whether the recording can play", () => {
  assert.ok(validSource(SOURCE));
  assert.ok(validSource({ ...SOURCE, startSeconds: 0 }));
  for (const bad of [
    null, {}, { ...SOURCE, meetingId: "" }, { ...SOURCE, meetingId: 3 }, { ...SOURCE, snapshotId: "" }, { ...SOURCE, segmentId: "" },
    { ...SOURCE, startSeconds: -1 }, { ...SOURCE, startSeconds: NaN }, { ...SOURCE, startSeconds: "7" }, { ...SOURCE, startSeconds: Infinity },
  ]) assert.ok(!validSource(bad), JSON.stringify(bad));
  assert.ok(canPlay({ tab: "transcript" }));
  assert.ok(!canPlay({ tab: "notes" }));
  assert.ok(!canPlay({ tab: null }));
  assert.ok(!canPlay(null));
});

test("a seek with the metadata loaded sets the time at once and ends when the audio answers", async () => {
  const env = setup();
  env.seek();
  assert.deepEqual(env.seeks, [7]);
  assert.equal(env.state().seek.id, "k1");
  assert.equal(env.state().seekDone, null);
  assert.equal(env.live().length, 0, "no wait for metadata");
  await tick();
  assert.equal(env.state().seek, null);
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: true });
  assert.equal(env.state().time, 7, "the answer is a sample: the row at the new time is active");
  assert.equal(env.state().current, "r3");
});

test("a seek before the audio has metadata waits, then applies on the metadata", async () => {
  const env = setup();
  env.audio.ready = false;
  env.seek();
  assert.deepEqual(env.seeks, []);
  assert.equal(env.state().seek.id, "k1");
  assert.deepEqual(env.live().map((timer) => timer.delay), [5000]);
  env.audio.ready = true;
  env.metadata();
  assert.deepEqual(env.seeks, [7]);
  assert.equal(env.live().length, 0, "the wait is over");
  await tick();
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: true });
});

test("the metadata of another meeting's audio does not release the seek", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek();
  env.audio.meetingId = "m2";
  env.audio.ready = true;
  env.metadata();
  assert.deepEqual(env.seeks, []);
  assert.equal(env.state().seek.id, "k1");
});

test("a seek fails when the metadata does not come in five seconds", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek();
  const [timer] = env.live();
  timer.callback();
  assert.equal(env.state().seek, null);
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.deepEqual(env.seeks, []);
});

test("a seek fails when the audio fails, and the wait stops", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek();
  env.send("playback/audio-failed");
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.equal(env.live().length, 0);
  env.audio.ready = true;
  env.metadata();
  assert.deepEqual(env.seeks, [], "a late metadata does not seek");
  env.send("playback/audio-failed");
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: false }, "an error with no seek waiting changes nothing");
});

test("a seek fails when the audio cannot be set to the time", async () => {
  const env = setup();
  env.audio.failSeek = true;
  env.seek();
  await tick();
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.equal(env.state().seek, null);
});

test("a seek while the meeting is not shown, or with another meeting's audio, fails at once", () => {
  const loading = setup({ rows: null, view: "loading" });
  loading.seek();
  assert.deepEqual(loading.state().seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.deepEqual(loading.seeks, []);
  const other = setup();
  other.page.shown = { ...other.page.shown, id: "m2" };
  other.seek();
  assert.equal(other.state().seekDone.ok, false);
  const wrongAudio = setup();
  wrongAudio.audio.meetingId = "m2";
  wrongAudio.seek();
  assert.equal(wrongAudio.state().seekDone.ok, false);
  assert.deepEqual(wrongAudio.seeks, []);
});

test("a newer seek replaces the one that waits", async () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  env.seek({ ...SOURCE, startSeconds: 13 }, "k2");
  assert.equal(env.state().seek.id, "k2");
  assert.equal(env.live().length, 1, "one wait, not two");
  env.audio.ready = true;
  env.metadata();
  assert.deepEqual(env.seeks, [13], "only the newer seek applies");
  await tick();
  assert.deepEqual(env.state().seekDone, { id: "k2", ids: ["k2"], ok: true });
});

test("a seek to the same target joins the waiting one, and both get the result", async () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  env.seek(SOURCE, "k2");
  assert.deepEqual(env.state().seek.ids, ["k1", "k2"]);
  assert.equal(env.live().length, 2, "each waiter has its own deadline");
  env.audio.ready = true;
  env.metadata();
  assert.equal(env.live().length, 0, "every deadline is cancelled");
  assert.deepEqual(env.seeks, [7], "the audio is set once");
  await tick();
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1", "k2"], ok: true });
});

test("a seek that joins one already being applied needs no deadline", async () => {
  const env = setup();
  env.seek(SOURCE, "k1");
  env.seek(SOURCE, "k2");
  assert.equal(env.live().length, 0);
  await tick();
  assert.deepEqual(env.seeks, [7]);
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1", "k2"], ok: true });
});

test("each joined seek has its own deadline: the first one gives up, the later one still gets the metadata", async () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  env.page.clock = 4000;
  env.seek(SOURCE, "k2");
  const [first, second] = env.live();
  assert.deepEqual([first.delay, second.delay], [5000, 5000]);
  first.callback();
  assert.deepEqual(env.state().seek.ids, ["k2"], "k1 timed out alone");
  assert.equal(env.state().seekDone, null);
  env.audio.ready = true;
  env.metadata();
  assert.deepEqual(env.seeks, [7]);
  assert.equal(second.live, false, "the later deadline is cancelled");
  await tick();
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k2"], ok: true });
});

test("joined seeks all fail once every deadline has passed", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  env.seek(SOURCE, "k2");
  for (const timer of env.live()) timer.callback();
  assert.equal(env.state().seek, null);
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k2"], ok: false });
  assert.deepEqual(env.seeks, []);
});

test("a seek to another target replaces the waiting one, which is not in the result", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  env.seek({ ...SOURCE, startSeconds: 13 }, "k2");
  assert.deepEqual(env.state().seek.ids, ["k2"]);
  assert.equal(env.live().length, 1, "only the new seek waits");
});

test("a newer seek that can apply at once stops the wait of the older one", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek(SOURCE, "k1");
  assert.equal(env.live().length, 1);
  env.audio.ready = true;
  env.seek({ ...SOURCE, startSeconds: 13 }, "k2");
  assert.equal(env.live().length, 0);
  assert.deepEqual(env.seeks, [13]);
});

test("an answer for a seek that was replaced is dropped", async () => {
  const env = setup();
  env.seek(SOURCE, "k1");
  env.audio.ready = false;
  env.seek({ ...SOURCE, startSeconds: 13 }, "k2");
  await tick(); // the answer of k1 was cancelled by the key of k2's wait, or arrives late
  env.send("playback/seek-applied", { meta: { seekId: "k1" }, value: { time: 7 } });
  assert.equal(env.state().seek.id, "k2");
  assert.equal(env.state().seekDone, null);
  env.send("playback/seek-failed", { meta: { seekId: "k1" } });
  assert.equal(env.state().seek.id, "k2");
  env.send("playback/seek-timed-out", { seekId: "k1" });
  assert.equal(env.state().seek.id, "k2");
});

test("leaving the meeting, or switching to another, drops the seek", () => {
  for (const change of [
    (env) => env.send("playback/meeting-left", { kind: "switch" }),
    (env) => env.send("playback/meeting-left", { kind: "home" }),
    (env) => { env.page.shown = { ...env.page.shown, id: "m2" }; env.send("history/anything"); },
    (env) => { env.page.shown = null; env.send("history/anything"); },
  ]) {
    const env = setup();
    env.audio.ready = false;
    env.seek();
    change(env);
    assert.equal(env.state().seek, null);
    assert.equal(env.live().length, 0, "the wait stopped");
    env.audio.ready = true;
    env.metadata();
    assert.deepEqual(env.seeks, []);
  }
});

test("another transcript on screen drops the seek; the same one reloaded does not", () => {
  const env = setup();
  env.audio.ready = false;
  env.seek();
  env.page.shown = { ...env.page.shown, rows: [...ROWS] };
  env.send("history/anything");
  assert.equal(env.state().seek.id, "k1", "same generation id");
  env.page.shown = { ...env.page.shown, generationId: "g2" };
  env.send("history/anything");
  assert.equal(env.state().seek, null);
  assert.deepEqual(env.state().seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.equal(env.live().length, 0);
});

test("a seek made before the transcript exists is not dropped when it appears", () => {
  const env = setup();
  env.page.shown = { ...env.page.shown, generationId: null, rows: null };
  env.audio.ready = false;
  env.seek();
  env.page.shown = { ...env.page.shown, generationId: "g1", rows: ROWS };
  env.send("history/anything");
  assert.equal(env.state().seek.id, "k1");
});

test("the metadata is a sample too: the rows at its time play", () => {
  const env = setup();
  env.audio.time = 7;
  assert.deepEqual(env.metadata().active, ["r3"]);
});

test("leaving a meeting repaints the rows", () => {
  for (const kind of ["switch", "home"]) {
    const env = setup();
    const { rev } = env.state();
    env.send("playback/meeting-left", { kind });
    assert.equal(env.state().rev, rev + 1, kind);
  }
});

test("a seek with no meeting on screen fails at once", () => {
  const action = { type: "playback/seek-requested", seekId: "k1", meetingId: "m1", startSeconds: 7, ready: true, audioMeetingId: "m1", shown: null, now: 0 };
  const { state, effects } = reduce(initialState, action);
  assert.deepEqual(state.seekDone, { id: "k1", ids: ["k1"], ok: false });
  assert.ok(!effects.some((effect) => effect.type === "call"));
});

test("scroll events carry no meeting, so they never drop a waiting seek", () => {
  const env = setup();
  env.audio.ready = false;
  const waiting = env.seek();
  for (const type of ["playback/scrolled", "playback/manual-scrolled", "playback/scroll-started", "playback/painted"]) {
    assert.equal(reduce(waiting, { type, now: 1, through: 0 }).state.seek.id, "k1", type);
  }
});

// ---- the slice stays out of the way ----

test("actions of other slices change nothing here", () => {
  const env = setup();
  const before = env.state();
  env.send("history/anything");
  env.send("task/poll-loaded");
  assert.equal(env.state(), before);
});

test("explicit seek and source targets keep real IDs independently of filtered rendering", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r3", startSeconds: 7, shown }).state;
  assert.equal(state.target, "r3");
  assert.equal(state.mark, "r3");
  assert.equal(state.ops.at(-1).id, "r3");
  state = reduce(state, { type: "playback/mark-requested", segmentId: "old-id", startSeconds: 13, shown }).state;
  assert.equal(state.target, "r4");
  state = reduce(state, { type: "playback/mark-requested", segmentId: null, shown }).state;
  assert.equal(state.target, null);
  state = reduce(state, { type: "playback/seek-requested", seekId: "seek", meetingId: "m1", audioMeetingId: "m1", ready: true, startSeconds: 7, shown }).state;
  state = reduce(state, { type: "playback/seek-applied", meta: { seekId: "seek" }, value: { time: 7, paused: true }, shown }).state;
  assert.equal(state.target, "r3");
  assert.deepEqual(state.active, ["r3"]);
  assert.equal(state.seekDone.ok, true);
  for (const [time, expected] of [[13, "r4"], [11, null], [7, "r3"]]) {
    state = reduce(state, { type: "playback/seeked", time, paused: true, shown }).state;
    assert.equal(state.target, expected);
  }
  state = reduce(state, { type: "playback/meeting-left", kind: "switch" }).state;
  assert.equal(state.target, null);
});

test("an explicit overlapping source ID survives the seek and its native acknowledgement", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/seek-requested", seekId: "overlap", meetingId: "m1", segmentId: "r2", startSeconds: 3, audioMeetingId: "m1", ready: true, shown }).state;
  state = reduce(state, { type: "playback/seek-applied", meta: { seekId: "overlap" }, value: { time: 3, paused: true }, shown }).state;
  assert.deepEqual(state.active, ["r1", "r2"]);
  assert.equal(state.target, "r2");
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, shown }).state;
  assert.equal(state.target, "r2");
});

test("the latest explicit timestamp target wins over an older Notes mark", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/mark-requested", segmentId: "r2", startSeconds: 3, shown }).state;
  state = reduce(state, { type: "playback/target-requested", segmentId: "r1", startSeconds: 3, shown }).state;
  assert.equal(state.mark, "r2");
  assert.equal(state.target, "r1");
  state = reduce(state, { type: "playback/seeked", time: 3, paused: false, shown }).state;
  assert.equal(state.target, "r1");
  assert.equal(state.pendingTarget, null);
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, shown }).state;
  assert.equal(state.target, "r1");
  assert.equal(state.mark, "r2");
});

test("a delayed native acknowledgement belongs to its outstanding explicit target", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: [row("first", 0, 5), row("short", 3, 3.08)] };
  let state = reduce(initialState, { type: "playback/target-requested", segmentId: "short", startSeconds: 3, shown }).state;
  state = reduce(state, { type: "playback/seeking", shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 3.25, paused: false, seeking: false, shown }).state;
  assert.equal(state.target, "short");
  assert.equal(state.targetOwner, "timestamp");
  assert.equal(state.pendingTarget, null);
});

test("a superseding native seek cancels the older pending target", () => {
  const rows = [row("old", 8, 10), row("new", 10, 12)];
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows };
  let state = reduce(initialState, { type: "playback/target-requested", segmentId: "old", startSeconds: 8, shown }).state;
  state = reduce(state, { type: "playback/seeking", shown }).state;
  assert.ok(state.pendingTarget);
  state = reduce(state, { type: "playback/seeking", shown }).state;
  assert.equal(state.pendingTarget, null);
  state = reduce(state, { type: "playback/seeked", time: 10.5, paused: true, seeking: false, shown }).state;
  assert.equal(state.target, "new");
  assert.equal(state.targetOwner, "time");
});

test("each programmatic start is owed before a native acknowledgement is consumed", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/target-requested", segmentId: "r2", startSeconds: 3, shown }).state;
  state = reduce(state, { type: "playback/target-requested", segmentId: "r2", startSeconds: 3, shown }).state;
  assert.equal(state.pendingTarget.owedStarts, 2);
  state = reduce(state, { type: "playback/seeking", shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, seeking: false, shown }).state;
  assert.equal(state.pendingTarget.owedStarts, 1);
  state = reduce(state, { type: "playback/seeking", shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, seeking: false, shown }).state;
  assert.equal(state.pendingTarget, null);
  assert.equal(state.target, "r2");
});

test("repeated timestamp writes before metadata owe one coalesced native start", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, {
    type: "playback/target-requested", segmentId: "r2", startSeconds: 3, ready: false, shown,
  }).state;
  state = reduce(state, {
    type: "playback/target-requested", segmentId: "r2", startSeconds: 3, ready: false, shown,
  }).state;
  assert.equal(state.pendingTarget.owedStarts, 1);
  state = reduce(state, { type: "playback/seeking", shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 3, paused: true, seeking: false, shown }).state;
  assert.equal(state.pendingTarget, null);
  state = reduce(state, { type: "playback/seeked", time: 13, paused: true, seeking: false, shown }).state;
  assert.equal(state.target, "r4", "the first later time-only seek resolves by its current time");
  assert.equal(state.targetOwner, "time");
});

test("a stale source snapshot may mark provenance without taking a newer reveal target", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  let state = reduce(initialState, { type: "playback/source-requested", sourceRequestId: 1, shown }).state;
  state = reduce(state, { type: "playback/target-requested", segmentId: "r4", startSeconds: 13, ready: true, shown }).state;
  state = reduce(state, {
    type: "playback/mark-requested", sourceRequestId: 1, segmentId: "r3", startSeconds: 7, shown,
  }).state;
  assert.equal(state.mark, "r3");
  assert.equal(state.target, "r4");
  assert.equal(state.targetOwner, "timestamp");
  assert.deepEqual(state.ops, [], "stale source provenance does not enqueue a reveal");
});

test("a native time-only seek still resolves the current row without an explicit target", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  const state = reduce(initialState, { type: "playback/seeked", time: 3, paused: true, shown }).state;
  assert.equal(state.target, "r1");
});

test("a seeked from another generation clears a pending target and uses current time", () => {
  const shown = { id: "m1", view: "meeting", generationId: "g1", rows: ROWS };
  const other = { ...shown, generationId: "g2", rows: [row("new", 13, 20)] };
  let state = reduce(initialState, { type: "playback/target-requested", segmentId: "r2", startSeconds: 3, shown }).state;
  state = reduce(state, { type: "playback/seeked", time: 13, paused: true, seeking: false, shown: other }).state;
  assert.equal(state.pendingTarget, null);
  assert.equal(state.target, "new");
  assert.equal(state.targetOwner, "time");
});
