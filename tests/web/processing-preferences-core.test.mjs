import assert from "node:assert/strict";
import test from "node:test";

import { initialState, normalizePreferences, reduce, selectPreferences } from "../../src/asr_studio/web/processing-preferences-core.js";

const PREFS = {
  asr: { choice: "auto", language: "vi" },
  notes: { provider: null, model: null, reasoning_effort: null, output_language: "en" },
  version: 4,
};

const CATALOGS = {
  asr: {
    status: "ready",
    models: [{ id: "auto", ready: true }, { id: "whisper-small", ready: true }],
    defaultModel: null,
    defaultDamaged: false,
    hardwareModel: "whisper-small",
  },
  providers: {
    chatgpt: {
      status: "connected",
      account: "test@example.com",
      ownerModel: "m1",
      ownerEffort: "high",
      models: [{ id: "m1", display_name: "Model 1" }],
      effortOptions: [{ value: "default", label: "Default" }, { value: "high", label: "High" }],
    },
    antigravity: {
      status: "disconnected",
      ownerModel: "g1",
      ownerEffort: "high",
      models: [],
      effortOptions: [],
    },
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
  assert.equal(view.notesOwner, null);
  assert.equal(view.asrOwner.discrepancy, false);
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
  assert.equal(view.canSave, true);
  assert.equal(view.providerConnected, true);
  assert.equal(view.notesOwner.state, "ready");
  assert.equal(view.notesOwner.model, "m1");
  assert.equal(view.notesOwner.effort, "high");
  assert.equal(view.notesOwner.discrepancy, true);
  assert.equal(view.notesOwner.text[2].text, "Cài đặt sở hữu khác recipe workflow đã lưu. Lưu để áp dụng cài đặt sở hữu.");
  assert.equal(reduce(selected, { type: "provider/processing-preference-edited", field: "notes_model", value: "stale" }).state.draft.notes.model, null);
});

test("saving sends only workflow fields and ignores stale replies", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  let state = reduce(loaded, { type: "provider/processing-preference-edited", field: "asr_choice", value: "whisper-small" }).state;
  state = reduce(state, { type: "provider/processing-preference-edited", field: "notes_provider", value: "chatgpt" }).state;
  state = reduce(state, { type: "provider/processing-preference-edited", field: "asr_language", value: "en" }).state;
  const view = selectPreferences(state, CATALOGS);
  assert.equal(view.canSave, true);
  const saving = reduce(state, { type: "provider/processing-preferences-save-requested" });
  const effect = saving.effects.find((item) => item.type === "fetch");
  assert.equal(effect.init.method, "PATCH");
  assert.deepEqual(JSON.parse(effect.init.body), {
    expected_version: 4,
    asr_language: "en",
    notes_provider: "chatgpt",
    notes_output_language: "en",
  });
  const stale = reduce(saving.state, {
    type: "provider/processing-preferences-saved",
    meta: { mutation: saving.state.mutation - 1 },
    response: { body: { ...PREFS, version: 5 } },
  }).state;
  assert.equal(stale.status, "saving");

  const confirmed = reduce(saving.state, {
    type: "provider/processing-preferences-saved",
    meta: { mutation: saving.state.mutation },
    response: { body: {
      asr: { choice: "auto", language: "en" },
      notes: { provider: "chatgpt", model: "m1", reasoning_effort: "high", output_language: "en" },
      version: 5,
    } },
  });
  assert.equal(confirmed.effects[0].name, "asr.settings-changed");
  assert.deepEqual(confirmed.effects[0].args, ["processing-preferences-save"]);
  assert.equal(confirmed.state.notice.text, "Đã lưu cài đặt xử lý tự động.");
  const refreshedAfterSave = reduce(confirmed.state, {
    type: "provider/processing-preferences-reconcile-requested",
  }).state;
  assert.equal(refreshedAfterSave.notice.text, "Đã lưu cài đặt xử lý tự động.");
});

test("reconciling after an owner save refreshes the version without discarding a draft", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  const edited = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "asr_language",
    value: "en",
  }).state;
  const requested = reduce(edited, { type: "provider/processing-preferences-reconcile-requested" });
  assert.equal(requested.state.status, "loading");
  assert.equal(requested.effects[0].meta.reconcile, true);
  assert.equal(requested.effects[0].meta.request, requested.state.request);
  assert.equal(selectPreferences(requested.state, CATALOGS).canSave, false);

  const refreshed = {
    ...PREFS,
    asr: { choice: "whisper-large-v3", language: "vi" },
    notes: { ...PREFS.notes, output_language: "vi" },
    version: 5,
  };
  const reconciled = reduce(requested.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: requested.state.request, reconcile: true },
    response: { body: refreshed },
  }).state;
  assert.equal(reconciled.status, "ready");
  assert.equal(reconciled.saved.version, 5);
  assert.equal(reconciled.draft.version, 5);
  assert.equal(reconciled.draft.asr.choice, "whisper-large-v3");
  assert.equal(reconciled.draft.asr.language, "en");
  assert.equal(reconciled.draft.notes.output_language, "vi");

  const saving = reduce(reconciled, { type: "provider/processing-preferences-save-requested" });
  assert.equal(JSON.parse(saving.effects[0].init.body).expected_version, 5);
});

test("reconciliation keeps only locally edited provider and output fields", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  let edited = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "asr_language",
    value: "en",
  }).state;
  edited = reduce(edited, {
    type: "provider/processing-preference-edited",
    field: "notes_provider",
    value: "chatgpt",
  }).state;
  edited = reduce(edited, {
    type: "provider/processing-preference-edited",
    field: "notes_output_language",
    value: "vi",
  }).state;
  const requested = reduce(edited, { type: "provider/processing-preferences-reconcile-requested" });
  const reconciled = reduce(requested.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: requested.state.request, reconcile: true },
    response: { body: {
      asr: { choice: "whisper-small", language: "vi" },
      notes: { provider: null, model: null, reasoning_effort: null, output_language: "en" },
      version: 5,
    } },
  }).state;
  assert.deepEqual(reconciled.draft, {
    asr: { choice: "whisper-small", language: "en" },
    notes: { provider: "chatgpt", model: null, reasoning_effort: null, output_language: "vi" },
    version: 5,
  });
});

test("a genuine concurrent save remains an explicit preferences-changed error", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  const edited = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "asr_language",
    value: "en",
  }).state;
  const saving = reduce(edited, { type: "provider/processing-preferences-save-requested" });
  const failed = reduce(saving.state, {
    type: "provider/processing-preferences-save-failed",
    meta: { mutation: saving.state.mutation },
    error: {
      status: 409,
      body: { detail: { code: "preferences_changed" } },
    },
  }).state;
  assert.equal(failed.status, "ready");
  assert.equal(failed.error.code, "preferences_changed");
});

test("owner divergence is visible and provider readiness blocks an unconfigured owner", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: {
      ...PREFS,
      notes: { provider: "chatgpt", model: "old-model", reasoning_effort: "default", output_language: "en" },
    } },
  }).state;
  const catalogs = {
    ...CATALOGS,
    providers: { ...CATALOGS.providers, chatgpt: { ...CATALOGS.providers.chatgpt, ownerModel: "m1", ownerEffort: "high" } },
  };
  const view = selectPreferences(loaded, catalogs);
  assert.equal(view.notesOwner.discrepancy, true);
  assert.equal(view.notesOwner.text[2].text, "Cài đặt sở hữu khác recipe workflow đã lưu. Lưu để áp dụng cài đặt sở hữu.");

  const missingDraft = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "asr_language",
    value: "en",
  }).state;
  const missing = selectPreferences(missingDraft, {
    ...catalogs,
    providers: { ...catalogs.providers, chatgpt: { ...catalogs.providers.chatgpt, ownerModel: "", ownerEffort: "" } },
  });
  assert.equal(missing.notesOwner.state, "missing");
  assert.equal(missing.canSave, false);

  const asrDivergence = selectPreferences(loaded, {
    ...catalogs,
    asr: {
      ...CATALOGS.asr,
      defaultModel: "whisper-small",
      models: [...CATALOGS.asr.models, { id: "whisper-large-v3", ready: true }],
    },
  });
  assert.equal(asrDivergence.asrOwner.discrepancy, true);
  assert.equal(asrDivergence.asrOwner.text[2].text, "Model ASR sở hữu khác lựa chọn workflow đã lưu. Mở Cài đặt → ASR Model để đồng bộ.");
});
