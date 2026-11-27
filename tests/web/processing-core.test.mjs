import assert from "node:assert/strict";
import test from "node:test";

import {
  initialState,
  reduce,
  selectProcessing,
  shouldRenderAutomaticStatus,
} from "../../src/asr_studio/web/processing-core.js";

const workflow = (overrides = {}) => ({
  id: "workflow-1",
  meeting_id: "meeting-1",
  state: "waiting",
  epoch: 2,
  preferences_version: 4,
  ready_sequence: 1,
  current_source: {
    generation_id: "generation-1",
    source_version: 3,
    metadata_version: 5,
    notes_source_version: 2,
  },
  pinned: {
    asr_choice: "auto",
    asr_language: "vi",
    notes_provider: null,
    notes_model: null,
    notes_reasoning_effort: null,
    notes_output_language: "en",
  },
  steps: [
    { id: "transcription", kind: "transcription", state: "succeeded", waiting_reason: null, source_snapshot_id: null, recipe_version: 1 },
    { id: "notes", kind: "notes", state: "waiting", waiting_reason: "provider_unconfigured", source_snapshot_id: "snapshot-1", recipe_version: 2 },
  ],
  attempts: [],
  actions: { cancel: true, retry_notes: true, apply_current_settings: true, use_current_source: false },
  ...overrides,
});

function loaded() {
  return reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1", processing: workflow() } },
  });
}

test("visible workflow installs both steps and schedules only the current meeting", () => {
  const result = loaded();
  const view = selectProcessing(result.state);
  assert.equal(view.visible, true);
  assert.equal(view.steps[0].state, "done");
  assert.equal(view.steps[1].state, "warning");
  assert.match(view.steps[1].reason.text, /provider/);
  assert.ok(result.effects.some((effect) => effect.type === "schedule" && effect.key === "processing/poll"));

  const left = reduce(result.state, { type: "history/home-clicked" });
  assert.equal(left.state.workflow, null);
  assert.ok(left.effects.some((effect) => effect.type === "cancel" && effect.key === "processing/poll"));
});

test("a skipped recipe is visible as skipped and makes no generated-output claim", () => {
  const result = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: {
      body: {
        id: "meeting-1",
        processing: workflow({
          state: "succeeded",
          stage_policy: { format_version: 1, asr: false, diarization: false, notes: false },
          current_source: {
            generation_id: null,
            source_version: null,
            metadata_version: 5,
            notes_source_version: 2,
          },
          steps: [
            { id: "transcription", kind: "transcription", state: "skipped", waiting_reason: "stage_disabled", source_snapshot_id: null, recipe_version: 1 },
            { id: "notes", kind: "notes", state: "skipped", waiting_reason: "stage_disabled", source_snapshot_id: null, recipe_version: 2 },
          ],
        }),
      },
    },
  });
  const view = selectProcessing(result.state);
  assert.equal(view.steps[0].state, "skipped");
  assert.equal(view.steps[1].state, "skipped");
  assert.equal(view.steps[0].value.text, "Đã bỏ qua");
  assert.match(view.message.text, /bỏ qua/);
  assert.doesNotMatch(view.source.text, /chép lời/);
});

test("manual task, generation, and notes status/revisions retain the history status strip", () => {
  const emptyManual = { task: null, generation: null, notes: { known: true, count: 0, status: null } };
  assert.equal(shouldRenderAutomaticStatus(workflow(), { ...emptyManual, task: { status: "running" } }), false);
  assert.equal(shouldRenderAutomaticStatus(workflow(), { ...emptyManual, task: { status: "succeeded" } }), false);
  assert.equal(shouldRenderAutomaticStatus(workflow(), { ...emptyManual, generation: { id: "generation-1" } }), false);
  assert.equal(shouldRenderAutomaticStatus(workflow(), { ...emptyManual, notes: { known: true, count: 0, status: "running" } }), false);
  assert.equal(shouldRenderAutomaticStatus(workflow(), { ...emptyManual, notes: { known: true, count: 1, status: null } }), false);
  assert.equal(shouldRenderAutomaticStatus(workflow(), emptyManual), true);
  assert.equal(shouldRenderAutomaticStatus(workflow({ state: "canceled" }), emptyManual), true);
  assert.equal(shouldRenderAutomaticStatus(workflow({ state: "succeeded" }), emptyManual), false);
});

test("a meeting without embedded processing reads the durable workflow endpoint", () => {
  const result = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1" } },
  });
  const read = result.effects.find((item) => item.type === "fetch" && item.key === "processing/meeting-read");
  assert.equal(read.url, "/api/meetings/meeting-1/processing");
  assert.deepEqual(read.meta, { key: "processing/meeting-read", meetingId: "meeting-1", loadId: 1, readId: 1 });

  const loaded = reduce(result.state, {
    type: "processing/meeting-read-loaded",
    meta: { meetingId: "meeting-1", loadId: 1, readId: 1 },
    response: { body: workflow() },
  });
  assert.equal(loaded.state.workflow.id, "workflow-1");
  assert.equal(loaded.state.readPending, false);
  assert.ok(loaded.effects.some((effect) => effect.type === "schedule" && effect.key === "processing/poll"));
});

test("a durable workflow read that arrives after navigation cannot install or restart polling", () => {
  const pending = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1" } },
  });
  const left = reduce(pending.state, { type: "history/home-clicked" });
  const late = reduce(left.state, {
    type: "processing/meeting-read-loaded",
    meta: { meetingId: "meeting-1", loadId: 1, readId: 1 },
    response: { body: workflow({ state: "running" }) },
  });
  assert.equal(late.state.workflow, null);
  assert.equal(late.state.readPending, false);
  assert.equal(late.effects.some((effect) => effect.type === "schedule"), false);
});

test("visibility cancels an in-flight poll and a late answer cannot restart it", () => {
  const state = loaded().state;
  const pending = reduce(state, { type: "processing/poll-due", meetingId: "meeting-1", loadId: state.loadId });
  assert.equal(pending.state.pollPending, true);
  const hidden = reduce(pending.state, { type: "task/visibility-changed", hidden: true });
  assert.equal(hidden.state.pollPending, false);
  assert.ok(hidden.effects.some((effect) => effect.type === "cancel" && effect.key === "processing/poll"));
  const late = reduce(hidden.state, {
    type: "processing/poll-loaded",
    meta: { meetingId: "meeting-1", loadId: state.loadId, pollId: pending.state.pollId },
    response: { body: workflow({ state: "succeeded" }) },
  });
  assert.equal(late.state.workflow.state, "waiting");
  assert.equal(late.effects.some((effect) => effect.type === "call" && effect.name === "meeting.refresh"), false);
  const visible = reduce(hidden.state, { type: "task/visibility-changed", hidden: false });
  assert.ok(visible.effects.some((effect) => effect.type === "schedule" && effect.key === "processing/poll"));
});

test("completion refreshes the current meeting through the existing view boundary", () => {
  const state = loaded().state;
  const pending = reduce(state, { type: "processing/poll-due", meetingId: "meeting-1", loadId: state.loadId });
  const completed = reduce(pending.state, {
    type: "processing/poll-loaded",
    meta: { meetingId: "meeting-1", loadId: state.loadId, pollId: pending.state.pollId },
    response: {
      body: workflow({
        state: "succeeded",
        steps: workflow().steps.map((step) => ({ ...step, state: "succeeded", waiting_reason: null })),
        actions: { cancel: false, retry_notes: false, apply_current_settings: false, use_current_source: false },
      }),
    },
  });
  assert.deepEqual(completed.effects.find((effect) => effect.type === "call" && effect.name === "meeting.refresh"), {
    type: "call",
    key: "processing/refresh-meeting",
    name: "meeting.refresh",
    args: ["meeting-1"],
  });
  assert.equal(completed.effects.some((effect) => effect.type === "schedule" && effect.key === "processing/poll"), false);
});

test("cancel uses the displayed epoch and a stale poll cannot resurrect a canceled workflow", () => {
  const state = loaded().state;
  const canceled = reduce(state, { type: "processing/cancel-clicked", requestId: "cancel-1" });
  const effect = canceled.effects.find((item) => item.type === "fetch" && item.key === "processing/action");
  assert.deepEqual(JSON.parse(effect.init.body), { request_id: "cancel-1", expected_pipeline_epoch: 2 });

  const late = reduce(canceled.state, {
    type: "processing/poll-loaded",
    meta: { meetingId: "meeting-1", loadId: 1, pollId: state.pollId },
    response: { body: workflow({ state: "running" }) },
  }).state;
  assert.equal(late.workflow.state, "waiting");
});

test("notes retry reads the current workflow, then posts only a notes attempt", () => {
  const state = loaded().state;
  const started = reduce(state, { type: "processing/retry-notes-clicked", requestId: "retry-1" });
  assert.equal(started.state.action.phase, "reading");
  const read = reduce(started.state, {
    type: "processing/action-state-loaded",
    meta: { meetingId: "meeting-1", requestId: "retry-1", kind: "retry_notes" },
    response: { body: workflow() },
  });
  const effect = read.effects.find((item) => item.type === "fetch" && item.key === "processing/action");
  assert.deepEqual(JSON.parse(effect.init.body), {
    request_id: "retry-1",
    expected_pipeline_epoch: 2,
    source_snapshot_id: "snapshot-1",
    confirmed_recipe_version: 2,
  });
  assert.deepEqual(selectProcessing(read.state).intended.params, { recipe: 2 });
  assert.ok(!JSON.stringify(selectProcessing(read.state).intended).includes("snapshot-1"));
});

test("settings adoption reads preferences before sending the exact preference version", () => {
  const state = loaded().state;
  const started = reduce(state, { type: "processing/apply-settings-clicked", requestId: "apply-1" });
  const read = reduce(started.state, {
    type: "processing/action-state-loaded",
    meta: { meetingId: "meeting-1", requestId: "apply-1", kind: "apply_current_settings" },
    response: { body: workflow({ epoch: 7 }) },
  });
  const prefs = reduce(read.state, {
    type: "processing/apply-preferences-loaded",
    meta: { meetingId: "meeting-1", requestId: "apply-1", kind: "apply_current_settings", epoch: 7 },
    response: { body: { asr: { choice: "auto", language: "vi" }, notes: { provider: null, model: null, reasoning_effort: null, output_language: "en" }, version: 11 } },
  });
  const effect = prefs.effects.find((item) => item.type === "fetch" && item.key === "processing/action");
  assert.deepEqual(JSON.parse(effect.init.body), { expected_pipeline_epoch: 7, expected_preferences_version: 11 });
});

test("source adoption reads all four source versions before posting", () => {
  const state = reduce(loaded().state, {
    type: "history/meeting-fetched",
    meeting: { id: "meeting-1", processing: workflow({ state: "needs_attention", actions: { cancel: true, retry_notes: true, apply_current_settings: false, use_current_source: true } }) },
  }).state;
  const started = reduce(state, { type: "processing/use-source-clicked", requestId: "source-1" });
  const read = reduce(started.state, {
    type: "processing/action-state-loaded",
    meta: { meetingId: "meeting-1", requestId: "source-1", kind: "use_current_source" },
    response: { body: workflow({ state: "needs_attention", epoch: 9 }) },
  });
  const effect = read.effects.find((item) => item.type === "fetch" && item.key === "processing/action");
  assert.deepEqual(JSON.parse(effect.init.body), {
    expected_pipeline_epoch: 9,
    expected_generation_id: "generation-1",
    expected_source_version: 3,
    expected_metadata_version: 5,
    expected_notes_source_version: 2,
  });
});

test("metadata-only source adoption posts paired null generation and source versions", () => {
  const metadataOnly = workflow({
    state: "needs_attention",
    current_source: {
      generation_id: null,
      source_version: null,
      metadata_version: 2,
      notes_source_version: 1,
    },
    actions: { cancel: true, retry_notes: false, apply_current_settings: false, use_current_source: true },
  });
  const state = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1", processing: metadataOnly } },
  }).state;
  const started = reduce(state, { type: "processing/use-source-clicked", requestId: "source-metadata-1" });
  const read = reduce(started.state, {
    type: "processing/action-state-loaded",
    meta: { meetingId: "meeting-1", requestId: "source-metadata-1", kind: "use_current_source" },
    response: { body: metadataOnly },
  });
  const effect = read.effects.find((item) => item.type === "fetch" && item.key === "processing/action");
  assert.deepEqual(JSON.parse(effect.init.body), {
    expected_pipeline_epoch: 2,
    expected_generation_id: null,
    expected_source_version: null,
    expected_metadata_version: 2,
    expected_notes_source_version: 1,
  });
  assert.equal(selectProcessing(read.state).source.text, "Chỉ có thông tin cuộc họp · phiên bản {metadata}. Xác nhận để chép lời bản ghi gốc.");
  const succeeded = reduce(read.state, {
    type: "processing/action-succeeded",
    meta: { meetingId: "meeting-1", requestId: "source-metadata-1", kind: "use_current_source" },
    response: { body: workflow({ epoch: 3, current_source: metadataOnly.current_source }) },
  });
  assert.equal(selectProcessing(succeeded.state).notice.text, "Đã xác nhận thông tin cuộc họp. ASR tự động sẽ chép lời bản ghi gốc.");
  assert.equal(succeeded.effects.some((item) => item.name === "meeting.refresh"), false);
  const due = reduce(succeeded.state, { type: "processing/poll-due", meetingId: "meeting-1", loadId: succeeded.state.loadId });
  const polled = reduce(due.state, {
    type: "processing/poll-loaded",
    meta: { meetingId: "meeting-1", loadId: due.state.loadId, pollId: due.state.pollId },
    response: { body: workflow({ epoch: 3, current_source: metadataOnly.current_source }) },
  });
  assert.deepEqual(polled.state.notice, succeeded.state.notice);
  const changedDue = reduce(polled.state, { type: "processing/poll-due", meetingId: "meeting-1", loadId: polled.state.loadId });
  const changed = reduce(changedDue.state, {
    type: "processing/poll-loaded",
    meta: { meetingId: "meeting-1", loadId: changedDue.state.loadId, pollId: changedDue.state.pollId },
    response: { body: workflow({ epoch: 4, current_source: metadataOnly.current_source }) },
  });
  assert.equal(changed.state.notice, null);
});

test("source adoption rejects a mixed null generation/source pair before posting", () => {
  const mixed = workflow({
    state: "needs_attention",
    current_source: {
      generation_id: null,
      source_version: 3,
      metadata_version: 5,
      notes_source_version: 2,
    },
    actions: { cancel: true, retry_notes: false, apply_current_settings: false, use_current_source: true },
  });
  const state = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1", processing: mixed } },
  }).state;
  const started = reduce(state, { type: "processing/use-source-clicked", requestId: "source-mixed-1" });
  const read = reduce(started.state, {
    type: "processing/action-state-loaded",
    meta: { meetingId: "meeting-1", requestId: "source-mixed-1", kind: "use_current_source" },
    response: { body: mixed },
  });
  assert.equal(read.state.error.code, "source_changed");
  assert.equal(read.effects.some((item) => item.type === "fetch" && item.key === "processing/action"), false);
});

test("an interrupted recording waits for an explicit transcription start", () => {
  const held = workflow({
    state: "needs_attention",
    current_source: { generation_id: null, source_version: null, metadata_version: 1, notes_source_version: 1 },
    steps: [
      { id: "transcription", kind: "transcription", state: "needs_attention", waiting_reason: "interrupted_recording", source_snapshot_id: null, recipe_version: 1 },
      { id: "notes", kind: "notes", state: "needs_attention", waiting_reason: "interrupted_recording", source_snapshot_id: null, recipe_version: 1 },
    ],
    actions: { cancel: true, retry_notes: false, apply_current_settings: true, use_current_source: true },
  });
  const result = reduce(initialState, {
    type: "history/meeting-loaded",
    meta: { loadId: 1 },
    response: { body: { id: "meeting-1", processing: held } },
  });
  const view = selectProcessing(result.state);
  assert.equal(view.actions.useSource, true);
  assert.equal(view.sourceActionLabel.text, "Chép lời phần đã ghi");
  assert.equal(view.message.text, "Bản ghi bị gián đoạn nên chưa tự xử lý. Bấm Chép lời phần đã ghi để bắt đầu chép lời và tạo notes.");
  assert.equal(view.steps[0].state, "problem");
  assert.match(view.steps[0].reason.text, /gián đoạn/);
});
