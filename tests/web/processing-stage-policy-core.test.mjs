import assert from "node:assert/strict";
import test from "node:test";

import {
  initialState,
  reduce,
  selectStagePolicy,
} from "../../src/asr_studio/web/processing-stage-policy-core.js";

const policyResponse = (overrides = {}) => ({
  contract_version: 1,
  format_version: 1,
  preference_version: 7,
  policy: { asr: true, diarization: false, notes: true },
  admission: { state: "enabled", format_version: 1, reason: null },
  updated_at: "2026-11-27 09:10:11",
  ...overrides,
});

function loadState(response = policyResponse()) {
  let result = reduce(initialState, { type: "provider/dialog-opened" });
  const request = result.state.request;
  result = reduce(result.state, {
    type: "provider/processing-stage-policy-loaded",
    meta: { request },
    response: { body: response },
  });
  return result.state;
}

test("opening Settings reads the stage policy through its own effect", () => {
  const result = reduce(initialState, { type: "provider/dialog-opened" });
  assert.equal(result.state.status, "loading");
  assert.equal(result.state.open, true);
  assert.equal(result.effects.length, 1);
  assert.deepEqual(result.effects[0], {
    type: "fetch",
    key: "provider/processing-stage-policy",
    url: "/api/processing-stage-policy/v1",
    init: {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
    },
    ok: "provider/processing-stage-policy-loaded",
    error: "provider/processing-stage-policy-failed",
    meta: { request: 1, reconcile: false },
  });
});

test("a disabled admission preserves all stored booleans and exposes effective skips", () => {
  const state = loadState(policyResponse({
    policy: { asr: false, diarization: true, notes: true },
    admission: { state: "disabled", format_version: 1, reason: "execution_not_ready" },
  }));
  const view = selectStagePolicy(state);
  assert.equal(state.status, "paused");
  assert.deepEqual(state.draft, { asr: false, diarization: true, notes: true });
  assert.deepEqual(view.effective, { asr: false, diarization: false, notes: false });
  assert.deepEqual(view.controls, { asr: false, diarization: true, notes: true });
  assert.equal(view.admission.reason, "execution_not_ready");
});

test("draining admission is a known fenced state, not a malformed legacy response", () => {
  const state = loadState(policyResponse({
    admission: { state: "draining", format_version: 1, reason: "draining" },
  }));
  assert.equal(state.status, "paused");
  assert.equal(selectStagePolicy(state).admission.state, "draining");
});

test("future policy versions fail closed as unsupported", () => {
  let state = reduce(initialState, { type: "provider/dialog-opened" });
  state = reduce(state.state, {
    type: "provider/processing-stage-policy-loaded",
    meta: { request: state.state.request },
    response: { body: policyResponse({ contract_version: 2 }) },
  }).state;
  assert.equal(state.status, "unsupported");
  assert.equal(state.saved, null);

  state = reduce(initialState, { type: "provider/dialog-opened" });
  state = reduce(state.state, {
    type: "provider/processing-stage-policy-loaded",
    meta: { request: state.state.request },
    response: { body: policyResponse({ format_version: 2 }) },
  }).state;
  assert.equal(state.status, "unsupported");
  assert.equal(state.saved, null);
});

test("saving sends only the strict stage-policy body and keeps a dependent draft value", () => {
  let state = loadState(policyResponse({ policy: { asr: false, diarization: true, notes: false } }));
  state = reduce(state, { type: "provider/processing-stage-policy-edited", field: "notes", value: true }).state;
  const result = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  assert.equal(result.state.status, "saving");
  assert.deepEqual(result.effects[0].init.body && JSON.parse(result.effects[0].init.body), {
    expected_preference_version: 7,
    format_version: 1,
    asr: false,
    diarization: true,
    notes: true,
  });
  assert.equal(Object.keys(JSON.parse(result.effects[0].init.body)).length, 5);
});

test("a stale response cannot replace a newer stage-policy request", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const reopened = reduce(opened.state, { type: "provider/processing-stage-policy-reload-requested" });
  const stale = reduce(reopened.state, {
    type: "provider/processing-stage-policy-loaded",
    meta: { request: opened.state.request },
    response: { body: policyResponse({ preference_version: 99 }) },
  });
  assert.equal(stale.state.status, "loading");
  assert.equal(stale.state.saved, null);
});

test("a conflict preserves the draft and requires an explicit reload", () => {
  let state = loadState();
  state = reduce(state, { type: "provider/processing-stage-policy-edited", field: "diarization", value: true }).state;
  const saving = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  const conflict = reduce(saving.state, {
    type: "provider/processing-stage-policy-save-failed",
    meta: { mutation: saving.state.mutation },
    error: { status: 409, message: "HTTP 409" },
  });
  assert.equal(conflict.state.status, "conflict");
  assert.equal(conflict.state.draft.diarization, true);
  assert.equal(selectStagePolicy(conflict.state).canSave, false);
});

test("an opaque server error never becomes a saved success", () => {
  let state = loadState();
  state = reduce(state, { type: "provider/processing-stage-policy-edited", field: "notes", value: false }).state;
  const saving = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  const failed = reduce(saving.state, {
    type: "provider/processing-stage-policy-save-failed",
    meta: { mutation: saving.state.mutation },
    error: { status: 502, message: "HTTP 502" },
  });
  assert.equal(failed.state.status, "error");
  assert.equal(failed.state.notice, null);
  assert.equal(failed.state.draft.notes, false);
});

test("another form's version refresh reads stage truth and preserves only touched draft fields", () => {
  let state = reduce(loadState(), { type: "provider/processing-stage-policy-edited", field: "asr", value: false }).state;
  let result = reduce(state, { type: "provider/processing-preferences-saved", response: { body: { version: 8 } } });
  assert.equal(result.state.status, "loading");
  assert.equal(result.state.saved.preferenceVersion, 7);
  assert.equal(result.effects[0].url, "/api/processing-stage-policy/v1");
  assert.equal(result.effects[0].meta.reconcile, true);
  result = reduce(result.state, { type: "provider/processing-stage-policy-loaded", meta: result.effects[0].meta,
    response: { body: policyResponse({ preference_version: 8, policy: { asr: true, diarization: true, notes: true } }) } });
  assert.deepEqual(result.state.draft, { asr: false, diarization: true, notes: true });
  result = reduce(result.state, { type: "provider/processing-stage-policy-save-requested" });
  assert.equal(JSON.parse(result.effects[0].init.body).expected_preference_version, 8);
  assert.equal(JSON.parse(result.effects[0].init.body).diarization, true);
});

test("a workflow response during a stage read queues one newer read without retrying a write", () => {
  let result = reduce(initialState, { type: "provider/dialog-opened" });
  const first = result.effects[0].meta;
  result = reduce(result.state, { type: "provider/processing-preferences-loaded", response: { body: { version: 9 } } });
  assert.deepEqual(result.effects, []);
  result = reduce(result.state, { type: "provider/processing-preferences-loaded", response: { body: { version: 8 } } });
  assert.equal(result.state.refreshVersion, 9);
  result = reduce(result.state, { type: "provider/processing-stage-policy-loaded", meta: first,
    response: { body: policyResponse({ preference_version: 8 }) } });
  assert.equal(result.effects.length, 1);
  assert.equal(result.effects[0].init.method, "GET");
  assert.equal(result.state.refreshVersion, 0);
  result = reduce(result.state, { type: "provider/processing-stage-policy-loaded", meta: result.effects[0].meta,
    response: { body: policyResponse({ preference_version: 9 }) } });
  assert.equal(result.state.saved.preferenceVersion, 9);
  assert.deepEqual(result.effects, []);
});

test("a workflow save during a stage mutation is reconciled only after the mutation settles", () => {
  let state = reduce(loadState(), { type: "provider/processing-stage-policy-edited", field: "asr", value: false }).state;
  let result = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  const mutation = result.effects[0].meta;
  result = reduce(result.state, { type: "provider/processing-preferences-saved", response: { body: { version: 9 } } });
  assert.equal(result.state.status, "saving");
  assert.deepEqual(result.effects, []);
  result = reduce(result.state, { type: "provider/processing-stage-policy-saved", meta: mutation,
    response: { body: policyResponse({ preference_version: 8, policy: { asr: false, diarization: false, notes: true } }) } });
  assert.deepEqual(result.effects.map(effect => effect.type), ["call", "fetch"]);
  assert.equal(result.effects[1].init.method, "GET");
  assert.equal(result.effects[1].meta.reconcile, true);
});

test("other form observations cannot clear a real conflict or trigger automatic resubmission", () => {
  let state = reduce(loadState(), { type: "provider/processing-stage-policy-edited", field: "asr", value: false }).state;
  let result = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  result = reduce(result.state, { type: "provider/processing-stage-policy-save-failed", meta: result.effects[0].meta,
    error: { status: 409 } });
  const error = result.state.error;
  result = reduce(result.state, { type: "provider/processing-preferences-loaded", response: { body: { version: 8 } } });
  assert.equal(result.state.status, "conflict");
  assert.equal(result.state.error, error);
  assert.equal(result.state.draft.asr, false);
  assert.deepEqual(result.effects, []);
  result = reduce(result.state, { type: "provider/processing-stage-policy-reload-requested" });
  assert.equal(result.effects[0].init.method, "GET");
  assert.equal(result.effects[0].meta.reconcile, true);
});

test("only a validated stage save notifies the other Settings form", () => {
  let state = reduce(loadState(), { type: "provider/processing-stage-policy-edited", field: "asr", value: false }).state;
  const saving = reduce(state, { type: "provider/processing-stage-policy-save-requested" });
  const meta = saving.effects[0].meta;
  const invalid = reduce(saving.state, { type: "provider/processing-stage-policy-saved", meta,
    response: { body: policyResponse({ preference_version: 9 }) } });
  assert.deepEqual(invalid.effects, []);
  const valid = reduce(saving.state, { type: "provider/processing-stage-policy-saved", meta,
    response: { body: policyResponse({ preference_version: 8, policy: { asr: false, diarization: false, notes: true } }) } });
  assert.deepEqual(valid.effects, [{ type: "call", key: "provider/processing-stage-policy-changed",
    name: "processing.stage-policy-changed", args: [] }]);
  const stale = reduce(valid.state, { type: "provider/processing-preferences-loaded", response: { body: { version: 7 } } });
  assert.equal(stale.state, valid.state);
  assert.deepEqual(stale.effects, []);
});
