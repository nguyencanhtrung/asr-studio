import assert from "node:assert/strict";
import test from "node:test";

import { initialState, normalizePreferences, reduce, selectEligibility, selectPreferences } from "../../src/asr_studio/web/processing-preferences-core.js";

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
      ownerKnown: true,
      models: [{ id: "m1", display_name: "Model 1" }],
      effortOptions: [{ value: "default", label: "Default" }, { value: "high", label: "High" }],
    },
    antigravity: {
      status: "disconnected",
      ownerModel: "g1",
      ownerEffort: "high",
      ownerKnown: true,
      models: [],
      effortOptions: [],
    },
  },
};

const saveAction = (state, catalogs = CATALOGS) => ({
  type: "provider/processing-preferences-save-requested",
  ownerDrift: selectEligibility(state, catalogs).ownerDrift,
  eligibility: selectEligibility(state, catalogs),
});

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
  assert.equal(view.notesOwner.discrepancy, false);
  assert.equal(view.notesOwner.text.text, "Đã lưu {model} · effort {effort} cho notes.");
  assert.deepEqual(view.notesOwner.text.params, { model: "Model 1", effort: "high" });
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
  const saving = reduce(state, saveAction(state));
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

  const saving = reduce(reconciled, saveAction(reconciled));
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
  const saving = reduce(edited, saveAction(edited));
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
  assert.equal(view.workflowDirty, false);
  assert.equal(view.ownerDrift, true);
  assert.equal(view.canSave, true);
  assert.equal(view.notesOwner.text[2].text, "Cài đặt sở hữu khác recipe workflow đã lưu. Lưu để áp dụng cài đặt sở hữu.");
  const ownerSave = reduce(loaded, {
    type: "provider/processing-preferences-save-requested",
    ownerDrift: view.ownerDrift,
    eligibility: view.eligibility,
  });
  assert.equal(ownerSave.state.status, "saving");
  assert.deepEqual(JSON.parse(ownerSave.effects[0].init.body), {
    expected_version: 4,
    asr_language: "vi",
    notes_provider: "chatgpt",
    notes_output_language: "en",
  });

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
  const rejected = reduce(missingDraft, {
    type: "provider/processing-preferences-save-requested",
    ownerDrift: false,
    eligibility: missing.eligibility,
  });
  assert.equal(rejected.state.status, "ready");
  assert.deepEqual(rejected.effects, []);

  const asrDivergence = selectPreferences(loaded, {
    ...catalogs,
    asr: {
      ...CATALOGS.asr,
      defaultModel: "whisper-small",
      models: [...CATALOGS.asr.models, { id: "whisper-large-v3", ready: true }],
    },
  });
  assert.equal(asrDivergence.asrOwner.discrepancy, true);
  assert.equal(asrDivergence.asrOwner.text[2].text, "Mở Cài đặt → ASR Model để đồng bộ nếu muốn.");
});

test("a committed owner pair remains eligible while disconnected or its catalog is unavailable", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: {
      ...PREFS,
      notes: { provider: "antigravity", model: "old-model", reasoning_effort: "default", output_language: "en" },
    } },
  }).state;
  const edited = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "notes_output_language",
    value: "vi",
  }).state;
  const disconnected = selectPreferences(edited, {
    ...CATALOGS,
    providers: { ...CATALOGS.providers, antigravity: { ...CATALOGS.providers.antigravity, catalogStatus: "ready" } },
  });
  assert.equal(disconnected.notesOwner.state, "disconnected");
  assert.equal(disconnected.notesOwner.ownerConfigured, true);
  assert.equal(disconnected.canSave, true);
  assert.equal(reduce(edited, {
    type: "provider/processing-preferences-save-requested",
    ownerDrift: disconnected.ownerDrift,
    eligibility: disconnected.eligibility,
  }).state.status, "saving");

  const failedCatalog = selectPreferences(edited, {
    ...CATALOGS,
    providers: { ...CATALOGS.providers, antigravity: { ...CATALOGS.providers.antigravity, status: "connected", catalogStatus: "error" } },
  });
  assert.equal(failedCatalog.notesOwner.state, "error");
  assert.equal(failedCatalog.notesOwner.ownerConfigured, true);
  assert.equal(failedCatalog.canSave, true);
});

test("committed provider summaries keep the saved effort through every catalog state", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: {
      ...PREFS,
      notes: { provider: "antigravity", model: "old-model", reasoning_effort: "default", output_language: "en" },
    } },
  }).state;
  const states = [
    { status: "disconnected", catalogStatus: "ready", models: [] },
    { status: "connected", catalogStatus: "loading", models: [] },
    { status: "connected", catalogStatus: "error", models: [] },
    { status: "connected", catalogStatus: "ready", models: [{ id: "g1", display_name: "G1", ready: false }] },
  ];
  for (const provider of states) {
    const view = selectPreferences(loaded, {
      ...CATALOGS,
      providers: { ...CATALOGS.providers, antigravity: { ...CATALOGS.providers.antigravity, ...provider } },
    });
    assert.equal(view.notesOwner.effort, "high");
    assert.match(JSON.stringify(view.notesOwner.text), /high/);
  }
  const disconnected = selectPreferences(loaded, {
    ...CATALOGS,
    providers: { ...CATALOGS.providers, antigravity: { ...CATALOGS.providers.antigravity, status: "disconnected" } },
  });
  assert.doesNotMatch(JSON.stringify(disconnected.notesOwner.text), /chưa được kết nối/);

  const noSavedModel = selectPreferences(loaded, {
    ...CATALOGS,
    providers: {
      ...CATALOGS.providers,
      antigravity: { ...CATALOGS.providers.antigravity, ownerModel: null, ownerEffort: "high", status: "disconnected" },
    },
  });
  assert.equal(noSavedModel.notesOwner.ownerConfigured, false);
  assert.doesNotMatch(JSON.stringify(noSavedModel.notesOwner.text), /high/);
  assert.match(JSON.stringify(noSavedModel.notesOwner.text), /Chưa có model notes đã lưu/);
});

test("initial-load and re-entry refreshes are queued, then the newest version wins", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const queued = reduce(opened.state, {
    type: "provider/settings-section-selected",
    section: "processing",
    previousSection: "ai",
  });
  assert.equal(queued.state.refreshPending, true);
  const first = reduce(queued.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  });
  assert.equal(first.state.status, "loading");
  assert.equal(first.effects[0].meta.reconcile, true);
  const second = reduce(first.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: first.state.request, reconcile: true },
    response: { body: { ...PREFS, version: 5, asr: { ...PREFS.asr, language: "en" } } },
  }).state;
  assert.equal(second.status, "ready");
  assert.equal(second.saved.version, 5);
  assert.equal(second.draft.asr.language, "en");

  const closed = reduce(second, { type: "provider/dialog-closed" });
  assert.equal(closed.state.saved, null);
  assert.equal(closed.state.draft, null);
  const reopened = reduce(closed.state, { type: "provider/dialog-opened" });
  assert.notEqual(reopened.state.request, first.state.request);
  const stale = reduce(reopened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: reopened.state.request - 1 },
    response: { body: { ...PREFS, asr: { ...PREFS.asr, language: "en" } } },
  }).state;
  assert.equal(stale.draft, null);
  assert.equal(stale.status, "loading");
});

test("owner and re-entry notifications are retained and coalesced by origin", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: PREFS },
  }).state;
  const external = reduce(loaded, { type: "provider/notes-owner-changed", source: "provider-owner-changed" });
  assert.equal(external.state.status, "loading");
  assert.equal(external.effects[0].meta.reconcile, true);

  const edited = reduce(loaded, {
    type: "provider/processing-preference-edited",
    field: "asr_language",
    value: "en",
  }).state;
  const saving = reduce(edited, saveAction(edited));
  const duringSave = reduce(saving.state, { type: "provider/asr-settings-changed", source: "asr-owner-save" });
  assert.equal(duringSave.state.refreshPending, true);
  const saved = reduce(duringSave.state, {
    type: "provider/processing-preferences-saved",
    meta: { mutation: duringSave.state.mutation },
    response: { body: { ...PREFS, asr: { ...PREFS.asr, language: "en" }, version: 5 } },
  });
  assert.equal(saved.effects[0].name, "asr.settings-changed");
  assert.equal(saved.effects[0].args[0], "processing-preferences-save");
  assert.equal(saved.effects[1].meta.reconcile, true);
  assert.equal(saved.state.notice.text, "Đã lưu cài đặt xử lý tự động.");

  const echoed = reduce(saved.state, { type: "provider/asr-settings-changed", source: "processing-preferences-save" });
  assert.deepEqual(echoed.effects, []);
  assert.equal(echoed.state.notice.text, "Đã lưu cài đặt xử lý tự động.");
});

test("ASR owner and workflow choices stay distinct, including Auto readiness", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: {
      ...PREFS,
      asr: { choice: "qwen3-asr-1.7b", language: "vi" },
    } },
  }).state;
  const catalog = {
    ...CATALOGS,
    asr: {
      status: "ready",
      models: [
        { id: "auto", label: "Auto", ready: true },
        { id: "whisper-small", label: "Whisper", ready: true },
        { id: "qwen3-asr-1.7b", label: "Qwen", ready: true },
      ],
      defaultModel: "whisper-small",
      hardwareModel: "whisper-small",
    },
  };
  const qwen = selectPreferences(loaded, catalog);
  assert.equal(qwen.asrOwner.model, "whisper-small");
  assert.equal(qwen.asrOwner.workflow, "qwen3-asr-1.7b");
  assert.equal(qwen.asrOwner.discrepancy, true);
  assert.deepEqual(qwen.asrOwner.text[0].params, { workflow: "Qwen3-ASR-1.7B", owner: "Whisper small" });

  const unavailable = selectPreferences(loaded, {
    ...catalog,
    asr: {
      ...catalog.asr,
      models: catalog.asr.models.map((model) => model.id === "qwen3-asr-1.7b"
        ? { ...model, ready: false, reason: "not_prepared" }
        : model),
    },
  });
  assert.equal(unavailable.asrOwner.state, "warning");
  assert.match(JSON.stringify(unavailable.asrOwner.text), /Qwen3-ASR-1\.7B/);
  assert.match(JSON.stringify(unavailable.asrOwner.text), /Whisper small/);

  const absent = selectPreferences(loaded, {
    ...catalog,
    asr: { ...catalog.asr, models: catalog.asr.models.filter((model) => model.id !== "qwen3-asr-1.7b") },
  });
  assert.equal(absent.asrOwner.state, "warning");
  assert.match(JSON.stringify(absent.asrOwner.text), /qwen3-asr-1\.7b/);
  assert.match(JSON.stringify(absent.asrOwner.text), /Whisper small/);

  const auto = selectPreferences({
    ...loaded,
    draft: { ...loaded.draft, asr: { choice: "auto", language: "vi" } },
  }, {
    ...catalog,
    asr: { ...catalog.asr, models: catalog.asr.models.map((model) => model.id === "auto" ? { ...model, ready: false, reason: "worker_unavailable" } : model) },
  });
  assert.equal(auto.asrOwner.workflow, "auto");
  assert.equal(auto.asrOwner.state, "warning");
  assert.match(JSON.stringify(auto.asrOwner.text), /worker/);
  assert.match(JSON.stringify(auto.asrOwner.text), /Whisper small/);

  const damaged = selectPreferences({
    ...loaded,
    draft: { ...loaded.draft, asr: { choice: "auto", language: "vi" } },
  }, {
    ...catalog,
    asr: {
      ...catalog.asr,
      models: [{ id: "auto", label: "Auto", ready: true }],
      defaultModel: null,
      defaultDamaged: true,
      hardwareModel: "whisper-large-v3",
    },
  });
  assert.equal(damaged.asrOwner.state, "warning");
  assert.match(JSON.stringify(damaged.asrOwner.text), /Không đọc được/);
  assert.match(JSON.stringify(damaged.asrOwner.text), /phần cứng/);

  const damagedExplicit = selectPreferences({
    ...loaded,
    draft: { ...loaded.draft, asr: { choice: "whisper-small", language: "vi" } },
  }, {
    ...catalog,
    asr: {
      ...catalog.asr,
      defaultModel: null,
      defaultDamaged: true,
      hardwareModel: "qwen3-asr-1.7b",
    },
  });
  assert.equal(damagedExplicit.asrOwner.state, "warning");
  assert.match(JSON.stringify(damagedExplicit.asrOwner.text), /Whisper small/);
  assert.match(JSON.stringify(damagedExplicit.asrOwner.text), /Qwen3-ASR-1\.7B/);
  assert.doesNotMatch(JSON.stringify(damagedExplicit.asrOwner.text), /Cài đặt sở hữu hiện là/);

  const damagedUnready = selectPreferences({
    ...loaded,
    draft: { ...loaded.draft, asr: { choice: "auto", language: "vi" } },
  }, {
    ...catalog,
    asr: {
      ...catalog.asr,
      models: catalog.asr.models.map((model) => model.id === "auto"
        ? { ...model, ready: false, reason: "worker_unavailable" }
        : model),
      defaultModel: null,
      defaultDamaged: true,
      hardwareModel: "qwen3-asr-1.7b",
    },
  });
  assert.equal(damagedUnready.asrOwner.state, "warning");
  assert.match(JSON.stringify(damagedUnready.asrOwner.text), /fallback/);
  assert.match(JSON.stringify(damagedUnready.asrOwner.text), /worker/);
});

test("a failed initial read retries once on same-dialog re-entry and coalesces another notification", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const queued = reduce(opened.state, {
    type: "provider/settings-section-selected",
    section: "processing",
    previousSection: "ai",
  });
  const failed = reduce(queued.state, {
    type: "provider/processing-preferences-failed",
    meta: { request: opened.state.request },
    error: { status: 503, body: { detail: { code: "unavailable" } } },
  });
  assert.equal(failed.state.saved, null);
  assert.equal(failed.state.status, "loading");
  assert.equal(failed.state.refreshPending, false);
  assert.equal(failed.state.initialRetry, true);
  assert.equal(failed.effects.length, 1);
  assert.equal(failed.effects[0].meta.reconcile, false);

  const failedAgain = reduce(failed.state, {
    type: "provider/processing-preferences-failed",
    meta: { request: failed.state.request },
    error: { status: 503, body: { detail: { code: "unavailable" } } },
  });
  assert.equal(failedAgain.state.status, "error");
  assert.equal(failedAgain.state.refreshPending, false);
  assert.deepEqual(failedAgain.effects, []);

  const retried = reduce(failedAgain.state, {
    type: "provider/settings-section-selected",
    section: "processing",
    previousSection: "ai",
  });
  assert.equal(retried.state.status, "loading");
  assert.equal(retried.state.refreshPending, false);
  assert.equal(retried.effects.length, 1);
  assert.equal(retried.effects[0].meta.reconcile, false);

  const coalesced = reduce(retried.state, { type: "provider/notes-owner-changed" });
  assert.equal(coalesced.state.refreshPending, true);
  assert.deepEqual(coalesced.effects, []);

  const firstRecovery = reduce(coalesced.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: retried.state.request },
    response: { body: PREFS },
  });
  assert.equal(firstRecovery.state.status, "loading");
  assert.equal(firstRecovery.effects.length, 1);
  assert.equal(firstRecovery.effects[0].meta.reconcile, true);

  const recovered = reduce(firstRecovery.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: firstRecovery.state.request, reconcile: true },
    response: { body: { ...PREFS, version: 5 } },
  });
  assert.equal(recovered.state.status, "ready");
  assert.equal(recovered.state.saved.version, 5);
  assert.deepEqual(recovered.effects, []);
});

for (const code of ["notes_owner_unconfigured", "preferences_changed"]) {
  test(`a ${code} mutation error survives the queued owner refresh until edit or save`, () => {
    const opened = reduce(initialState, { type: "provider/dialog-opened" });
    const loaded = reduce(opened.state, {
      type: "provider/processing-preferences-loaded",
      meta: { request: opened.state.request },
      response: { body: {
        ...PREFS,
        notes: { provider: "chatgpt", model: "old-model", reasoning_effort: "default", output_language: "en" },
      } },
    }).state;
    const edited = reduce(loaded, {
      type: "provider/processing-preference-edited",
      field: "asr_language",
      value: "en",
    }).state;
    const saving = reduce(edited, saveAction(edited));
    const queued = reduce(saving.state, { type: "provider/notes-owner-changed" });
    assert.equal(queued.state.refreshPending, true);

    const failed = reduce(queued.state, {
      type: "provider/processing-preferences-save-failed",
      meta: { mutation: saving.state.mutation },
      error: { status: 409, body: { detail: { code } } },
    });
    assert.equal(failed.state.status, "loading");
    assert.equal(failed.state.error.code, code);
    assert.equal(failed.state.error.source, "mutation");
    assert.equal(failed.effects[0].meta.reconcile, true);

    const refreshed = reduce(failed.state, {
      type: "provider/processing-preferences-loaded",
      meta: { request: failed.state.request, reconcile: true },
      response: { body: {
        ...PREFS,
        notes: { provider: "chatgpt", model: "new-model", reasoning_effort: "high", output_language: "en" },
        version: 5,
      } },
    });
    assert.equal(refreshed.state.status, "ready");
    assert.equal(refreshed.state.error.code, code);
    assert.equal(refreshed.state.draft.asr.language, "en");
    assert.equal(refreshed.state.saved.version, 5);

    const editedAgain = reduce(refreshed.state, {
      type: "provider/processing-preference-edited",
      field: "asr_language",
      value: "vi",
    });
    assert.equal(editedAgain.state.error, null);
  });
}

test("settled provider read failures are explicit and do not invent an owner pair", () => {
  const opened = reduce(initialState, { type: "provider/dialog-opened" });
  const loaded = reduce(opened.state, {
    type: "provider/processing-preferences-loaded",
    meta: { request: opened.state.request },
    response: { body: {
      ...PREFS,
      notes: { provider: "chatgpt", model: "workflow-model", reasoning_effort: "medium", output_language: "en" },
    } },
  }).state;
  const firstReadFailed = selectPreferences(loaded, {
    ...CATALOGS,
    providers: {
      ...CATALOGS.providers,
      chatgpt: { ...CATALOGS.providers.chatgpt, status: undefined, catalogStatus: "error", ownerModel: null, ownerEffort: "default", ownerKnown: false },
    },
  });
  assert.equal(firstReadFailed.notesOwner.state, "error");
  assert.equal(firstReadFailed.notesOwner.ownerConfigured, false);
  assert.equal(firstReadFailed.notesOwner.model, null);
  assert.equal(firstReadFailed.notesOwner.effort, null);
  assert.doesNotMatch(JSON.stringify(firstReadFailed.notesOwner.text), /Đang kiểm tra/);
  assert.doesNotMatch(JSON.stringify(firstReadFailed.providerStatus), /Đang kiểm tra/);

  const catalogFailed = selectPreferences(loaded, {
    ...CATALOGS,
    providers: {
      ...CATALOGS.providers,
      chatgpt: { ...CATALOGS.providers.chatgpt, status: "connected", catalogStatus: "error", ownerModel: "m1", ownerEffort: "high", ownerKnown: true },
    },
  });
  assert.equal(catalogFailed.notesOwner.state, "error");
  assert.equal(catalogFailed.notesOwner.ownerConfigured, true);
  assert.match(JSON.stringify(catalogFailed.notesOwner.text), /Model 1/);
  assert.match(JSON.stringify(catalogFailed.notesOwner.text), /high/);
  assert.doesNotMatch(JSON.stringify(catalogFailed.notesOwner.text), /Đang kiểm tra/);
  assert.match(JSON.stringify(catalogFailed.providerStatus), /Đã kết nối với|test@example\.com/);
  assert.doesNotMatch(JSON.stringify(catalogFailed.providerStatus), /Không tải được catalog/);
});
