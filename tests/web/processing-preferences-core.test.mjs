import assert from "node:assert/strict";
import test from "node:test";

import { initialState, normalizePreferences, reduce, selectPreferences } from "../../src/asr_studio/web/processing-preferences-core.js";

const PREFS = {
  asr: { choice: "auto", language: "vi" },
  notes: { provider: null, model: null, reasoning_effort: null, output_language: "en" },
  version: 4,
};

const CATALOGS = {
  asrModels: [{ id: "auto", ready: true }, { id: "whisper-small", ready: true }],
  providers: {
    chatgpt: {
      status: "connected",
      account: "test@example.com",
      models: [{ id: "m1", display_name: "Model 1" }],
      effortOptions: [{ value: "default", label: "Default" }, { value: "high", label: "High" }],
    },
    antigravity: { status: "disconnected", models: [], effortOptions: [] },
  },
};

test("automatic preference reads preserve the server version and explain an unset provider", () => {
  assert.deepEqual(normalizePreferences(PREFS), PREFS);
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  assert.equal(opened.state.status, "loading");
  assert.equal(opened.effects[0].url, "/api/processing-preferences");

  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  const view = selectPreferences(loaded, CATALOGS);
  assert.equal(view.version, 4);
  assert.equal(view.waiting, true);
  assert.match(view.providerStatus.text, /Transcript/);
  assert.equal(view.dirty, false);
  assert.equal(view.modelOptions.length, 0);
});

test("changing provider never silently chooses its first account or model", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  const selected = reduce(loaded, { type: "provider/processing-preference-edited", field: "notes_provider", value: "chatgpt" }).state;
  assert.equal(selected.draft.notes.provider, "chatgpt");
  assert.equal(selected.draft.notes.model, null);
  assert.equal(selected.draft.notes.reasoning_effort, null);
  const view = selectPreferences(selected, CATALOGS);
  assert.equal(view.canSave, false);
  assert.equal(view.providerConnected, true);
  assert.equal(view.modelValue, "");
  assert.deepEqual(view.modelOptions.map((option) => option.value), ["m1"]);
});

test("saving sends exactly the additive preference contract and ignores stale replies", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  let state = reduce(loaded, { type: "provider/processing-preference-edited", field: "asr_choice", value: "whisper-small" }).state;
  state = reduce(state, { type: "provider/processing-preference-edited", field: "notes_provider", value: "chatgpt" }).state;
  state = reduce(state, { type: "provider/processing-preference-edited", field: "notes_model", value: "m1" }).state;
  state = reduce(state, { type: "provider/processing-preference-edited", field: "notes_reasoning_effort", value: "high" }).state;
  const view = selectPreferences(state, CATALOGS);
  assert.equal(view.canSave, true);
  const saving = reduce(state, { type: "provider/processing-preferences-save-requested" });
  const effect = saving.effects.find((item) => item.type === "fetch");
  assert.deepEqual(JSON.parse(effect.init.body), {
    asr_choice: "whisper-small",
    asr_language: "vi",
    notes_provider: "chatgpt",
    notes_model: "m1",
    notes_reasoning_effort: "high",
    notes_output_language: "en",
  });
  const stale = reduce(saving.state, {
    type: "provider/processing-preferences-saved",
    meta: { mutation: saving.state.mutation - 1 },
    response: { body: { ...PREFS, version: 5 } },
  }).state;
  assert.equal(stale.status, "saving");
});
