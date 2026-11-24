// Pure core for automatic-processing preferences shown in Settings > Devices.
// No DOM, network, timer, storage, clock or provider fallback: the view supplies
// the existing ASR/provider catalogs and this reducer emits data-only effects.

import { ASR_MODEL_TEXT, errorMessageForCode } from "./shell-core.js";

const t = (text, params) => (params ? { text, params } : { text });
const done = (state, effects = []) => ({ state, effects });
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const PROVIDERS = ["chatgpt", "antigravity"];
const LANGUAGES = ["en", "vi"];

const GET = {
  method: "GET",
  headers: { Accept: "application/json" },
  credentials: "same-origin",
  cache: "no-store",
};

const cancelRead = { type: "cancel", key: "provider/processing-preferences" };
const cancelSave = { type: "cancel", key: "provider/processing-preferences-save" };
const SETTINGS_CHANGED = {
  type: "call",
  key: "provider/processing-preferences-changed",
  name: "asr.settings-changed",
  args: ["processing-preferences-save"],
};

export const initialState = {
  open: false,
  status: "idle", // idle | loading | ready | saving | error
  request: 0,
  mutation: 0,
  refreshPending: false,
  saved: null,
  draft: null,
  error: null,
  notice: null,
};

function validChoice(value) {
  return typeof value === "string" && value.length > 0;
}

export function normalizePreferences(payload) {
  if (!isObject(payload) || !isObject(payload.asr) || !isObject(payload.notes)) return null;
  const { asr, notes } = payload;
  const provider = notes.provider === null ? null : PROVIDERS.includes(notes.provider) ? notes.provider : null;
  if (
    !validChoice(asr.choice)
    || !LANGUAGES.includes(asr.language)
    || notes.provider !== null && !PROVIDERS.includes(notes.provider)
    || provider !== null && (!validChoice(notes.model) || !validChoice(notes.reasoning_effort))
    || !LANGUAGES.includes(notes.output_language)
  ) return null;
  const version = Number(payload.version);
  if (!Number.isSafeInteger(version) || version < 1) return null;
  // A legacy PUT may retain dormant model/effort mirrors while its provider is
  // unset. They remain in GET storage, but the UI has no owner recipe to show.
  return {
    asr: { choice: asr.choice, language: asr.language },
    notes: {
      provider,
      model: provider === null ? null : notes.model,
      reasoning_effort: provider === null ? null : notes.reasoning_effort,
      output_language: notes.output_language,
    },
    version,
  };
}

const clone = (value) => ({
  asr: { ...value.asr },
  notes: { ...value.notes },
  version: value.version,
});

function equalWorkflowPreferences(left, right) {
  if (!left || !right) return false;
  return left.asr.language === right.asr.language
    && left.notes.provider === right.notes.provider
    && left.notes.output_language === right.notes.output_language;
}

function requestError(error, provider = null) {
  const status = Number.isFinite(error?.status) ? error.status : 0;
  const payload = error?.body && typeof error.body === "object" ? error.body : null;
  const detail = payload && (payload.detail || payload.error);
  const code = detail && typeof detail === "object" && typeof detail.code === "string"
    ? detail.code
    : `HTTP_${status}`;
  if (code === "notes_owner_unconfigured") {
    const owner = provider === "antigravity" ? "Google Antigravity" : "AI Provider";
    return {
      code,
      text: t("Chưa có model notes đã lưu. Mở Cài đặt → {owner} để chọn model trước khi lưu xử lý tự động.", { owner }),
    };
  }
  return {
    code,
    text: errorMessageForCode(code, status) || t("Không thể đọc cài đặt xử lý tự động. Hãy thử lại."),
  };
}

function readEffect(request, reconcile = false) {
  return {
    type: "fetch",
    key: "provider/processing-preferences",
    url: "/api/processing-preferences",
    init: GET,
    ok: "provider/processing-preferences-loaded",
    error: "provider/processing-preferences-failed",
    meta: { request, reconcile },
  };
}

function reconciledDraft(server, draft, baseline) {
  const localLanguage = draft.asr.language !== baseline.asr.language;
  const localProvider = draft.notes.provider !== baseline.notes.provider;
  const localOutputLanguage = draft.notes.output_language !== baseline.notes.output_language;
  const provider = localProvider ? draft.notes.provider : server.notes.provider;
  return {
    asr: {
      choice: server.asr.choice,
      language: localLanguage ? draft.asr.language : server.asr.language,
    },
    notes: {
      ...draft.notes,
      provider,
      model: localProvider ? draft.notes.model : server.notes.model,
      reasoning_effort: localProvider ? draft.notes.reasoning_effort : server.notes.reasoning_effort,
      output_language: localOutputLanguage ? draft.notes.output_language : server.notes.output_language,
    },
    version: server.version,
  };
}

function saveEffect(state, mutation) {
  const { asr, notes } = state.draft;
  return {
    type: "fetch",
    key: "provider/processing-preferences-save",
    url: "/api/processing-preferences",
    init: {
      method: "PATCH",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({
        expected_version: state.saved.version,
        asr_language: asr.language,
        notes_provider: notes.provider,
        notes_output_language: notes.output_language,
      }),
    },
    ok: "provider/processing-preferences-saved",
    error: "provider/processing-preferences-save-failed",
    meta: { mutation },
  };
}

function fieldEdited(state, action) {
  if (!state.draft || state.status === "saving") return done(state);
  const draft = clone(state.draft);
  if (action.field === "asr_language" && LANGUAGES.includes(action.value)) draft.asr.language = action.value;
  if (action.field === "notes_provider" && (action.value === null || PROVIDERS.includes(action.value))) {
    draft.notes.provider = action.value;
    // Clear only the local workflow mirror. The PATCH body never sends these owner fields.
    if (action.value !== state.draft.notes.provider) {
      draft.notes.model = null;
      draft.notes.reasoning_effort = null;
    }
  }
  if (action.field === "notes_output_language" && LANGUAGES.includes(action.value)) draft.notes.output_language = action.value;
  return done({ ...state, draft, error: null, notice: null });
}

function startReconcile(state) {
  if (!state.open || !state.saved) return done({ ...state, refreshPending: true });
  const request = state.request + 1;
  return done(
    { ...state, status: "loading", request, refreshPending: false, error: null },
    [readEffect(request, true)],
  );
}

function queueReconcile(state) {
  if (!state.open) return done(state);
  if (!state.saved || state.status === "loading" || state.status === "saving") {
    return done({ ...state, refreshPending: true });
  }
  return startReconcile(state);
}

function continuePendingRefresh(state) {
  return state.refreshPending && state.open && state.saved ? startReconcile(state) : done(state);
}

export function reduce(state, action) {
  switch (action.type) {
    case "provider/dialog-opened": {
      const request = state.request + 1;
      return done(
        {
          ...state,
          open: true,
          status: "loading",
          request,
          refreshPending: false,
          saved: null,
          draft: null,
          error: null,
          notice: null,
        },
        [readEffect(request)],
      );
    }
    case "provider/dialog-closed":
      return done(
        {
          ...state,
          open: false,
          status: "idle",
          request: state.request + 1,
          mutation: state.mutation + 1,
          refreshPending: false,
          saved: null,
          draft: null,
          error: null,
          notice: null,
        },
        [cancelRead, cancelSave],
      );
    case "provider/settings-section-selected":
      return action.section === "processing" && action.previousSection !== "processing"
        ? queueReconcile(state)
        : done(state);
    case "provider/asr-settings-changed":
      // The successful Automation PATCH broadcasts its own source-tagged event.
      // It must keep the success notice and must not issue a redundant GET.
      return action.source === "processing-preferences-save" ? done(state) : queueReconcile(state);
    case "provider/notes-owner-changed":
      return queueReconcile(state);
    case "provider/processing-preferences-reconcile-requested":
      return queueReconcile(state);
    case "provider/processing-preferences-loaded": {
      if (action.meta?.request !== state.request || !state.open) return done(state);
      const preferences = normalizePreferences(action.response?.body);
      if (!preferences) {
        return done({ ...state, status: "error", error: { code: "invalid_response", text: t("Máy chủ trả về cài đặt xử lý tự động không hợp lệ.") } });
      }
      const reconciled = action.meta?.reconcile === true && state.draft && state.saved
        ? reconciledDraft(preferences, state.draft, state.saved)
        : clone(preferences);
      return continuePendingRefresh({ ...state, status: "ready", saved: preferences, draft: reconciled, error: null });
    }
    case "provider/processing-preferences-failed": {
      if (action.meta?.request !== state.request || !state.open) return done(state);
      return continuePendingRefresh({ ...state, status: "error", error: requestError(action.error, state.draft?.notes.provider), notice: null });
    }
    case "provider/processing-preference-edited":
      return fieldEdited(state, action);
    case "provider/processing-preferences-save-requested": {
      const workflowDirty = !equalWorkflowPreferences(state.saved, state.draft);
      const ownerDirty = action.ownerDrift === true;
      if (!state.open || !state.saved || !state.draft || state.status === "saving"
        || action.eligibility?.canSave !== true
        || (!workflowDirty && !ownerDirty)) return done(state);
      const mutation = state.mutation + 1;
      return done({ ...state, status: "saving", mutation, error: null, notice: null }, [saveEffect(state, mutation)]);
    }
    case "provider/processing-preferences-saved": {
      if (action.meta?.mutation !== state.mutation || !state.open) return done(state);
      const preferences = normalizePreferences(action.response?.body);
      if (!preferences) {
        return done({ ...state, status: "ready", error: { code: "invalid_response", text: t("Máy chủ chưa xác nhận cài đặt xử lý tự động.") } });
      }
      const confirmed = { ...state, status: "ready", saved: preferences, draft: clone(preferences), error: null, notice: t("Đã lưu cài đặt xử lý tự động.") };
      const pending = continuePendingRefresh(confirmed);
      return done(pending.state, [SETTINGS_CHANGED, ...pending.effects]);
    }
    case "provider/processing-preferences-save-failed":
      if (action.meta?.mutation !== state.mutation || !state.open) return done(state);
      return continuePendingRefresh({ ...state, status: "ready", error: requestError(action.error, state.draft?.notes.provider), notice: null });
    default:
      return done(state);
  }
}

function selectedProviderCatalog(draft, providers) {
  const catalog = providers?.[draft.notes.provider];
  return catalog && typeof catalog === "object" ? catalog : null;
}

function choiceLabel(choice) {
  return ASR_MODEL_TEXT[choice.id]?.label || choice.label || choice.model_name || choice.id;
}

function providerStatusText(provider, catalog) {
  if (!provider) return t("Không chọn provider notes. Transcript có thể hoàn tất; notes sẽ chờ đến khi bạn chọn provider.");
  if (!catalog || !catalog.status) return t("Đang kiểm tra");
  if (catalog.status === "connected") return catalog.account ? t("Đã kết nối với {account}", { account: catalog.account }) : t("Đã kết nối");
  return provider === "chatgpt"
    ? t("ChatGPT chưa được kết nối. Không tự chuyển sang provider khác.")
    : t("Google Antigravity chưa được kết nối. Không tự chuyển sang provider khác.");
}

function ownerPair(catalog) {
  const model = validChoice(catalog?.ownerModel) ? catalog.ownerModel : null;
  const effort = validChoice(catalog?.ownerEffort) ? catalog.ownerEffort : null;
  return { model, effort };
}

function asrOwnerView(catalog, savedChoice) {
  const asr = catalog && typeof catalog === "object" ? catalog : {};
  if (asr.status === "loading" || !asr.status) {
    return { state: "loading", text: t("Đang tải danh sách model…"), discrepancy: false };
  }
  if (asr.status === "error") {
    return { state: "error", text: t("Không tải được danh sách model. Mở lại Cài đặt để thử lại."), discrepancy: false };
  }
  if (asr.status === "empty") {
    return { state: "error", text: t("Máy chủ không trả về model nào. Mở lại Cài đặt để thử lại."), discrepancy: false };
  }
  const models = Array.isArray(asr.models) ? asr.models.filter((choice) => isObject(choice) && validChoice(choice.id)) : [];
  const committed = validChoice(asr.defaultModel) ? asr.defaultModel : "auto";
  const workflow = validChoice(savedChoice) ? savedChoice : "auto";
  const ownerRow = models.find((choice) => choice.id === committed);
  const effectiveRow = models.find((choice) => choice.id === workflow);
  const ownerLabel = ownerRow ? choiceLabel(ownerRow) : committed === "auto" ? (asr.hardwareModel || "auto") : committed;
  const workflowLabel = effectiveRow ? choiceLabel(effectiveRow) : workflow;
  const discrepancy = workflow !== committed;
  let state = "ready";
  let message;
  if (!effectiveRow) {
    state = "warning";
    message = t("Workflow chép lời dùng {model}, nhưng model này không còn trong catalog. Hãy mở ASR Model để chọn lại.", { model: workflowLabel });
  } else if (effectiveRow.ready === false) {
    state = "warning";
    message = effectiveRow.reason === "worker_unavailable"
      ? t("Workflow chép lời dùng {model}, nhưng worker chưa sẵn sàng để kiểm tra model.", { model: workflowLabel })
      : t("Workflow chép lời dùng {model}, nhưng model này chưa sẵn sàng trên máy này.", { model: workflowLabel });
  } else if (discrepancy) {
    message = t("Workflow chép lời dùng {workflow}. Cài đặt sở hữu hiện là {owner}. Lần chạy mới vẫn dùng workflow đã lưu.", { workflow: workflowLabel, owner: ownerLabel });
  } else {
    message = t("Lần chép lời Tự động dùng {model}. Mỗi lần chạy vẫn chọn được model khác.", { model: workflowLabel });
  }
  return {
    state,
    text: discrepancy && state === "ready"
      ? [message, " ", t("Mở Cài đặt → ASR Model để đồng bộ nếu muốn.")]
      : message,
    model: committed === "auto" ? null : committed,
    workflow,
    discrepancy,
  };
}

function providerOwnerView(provider, catalog, saved) {
  if (!provider) return null;
  const name = provider === "chatgpt" ? "ChatGPT" : "Google Antigravity";
  if (!catalog) return { state: "loading", text: t("Đang kiểm tra"), discrepancy: false, ownerConfigured: false };
  const pair = ownerPair(catalog);
  const owner = ownerLabel(catalog, pair);
  const ownerConfigured = Boolean(pair.model && pair.effort);
  const discrepancy = ownerConfigured
    && saved?.provider === provider
    && (saved.model !== pair.model || saved.reasoning_effort !== pair.effort);
  const ownerMessage = (message) => discrepancy
    ? [message, " ", t("Cài đặt sở hữu khác recipe workflow đã lưu. Lưu để áp dụng cài đặt sở hữu.")]
    : message;
  if (!catalog.status) {
    return {
      state: "loading",
      model: pair.model,
      effort: pair.effort,
      text: ownerMessage(pair.model || pair.effort ? t("Đang kiểm tra. Model đã lưu {owner} vẫn được giữ.", { owner }) : t("Đang kiểm tra")),
      discrepancy,
      ownerConfigured,
    };
  }
  const catalogStatus = catalog.catalogStatus || "ready";
  if (catalog.status !== "connected") {
    return {
      state: "disconnected",
      model: pair.model,
      effort: pair.effort,
      discrepancy,
      ownerConfigured,
      text: ownerMessage(pair.model
        ? t("Model đã lưu {owner}; hãy kết nối lại trong panel sở hữu.", { owner })
        : t("Chưa có model notes đã lưu. Hãy kết nối lại trong panel sở hữu để chọn model.")),
    };
  }
  if (catalogStatus === "loading") {
    return {
      state: "loading",
      model: pair.model,
      effort: pair.effort,
      discrepancy,
      ownerConfigured,
      text: ownerMessage(pair.model
        ? t("Đang tải catalog. Model đã lưu {owner} vẫn được giữ; hãy đợi hoặc thử lại.", { owner })
        : t("Đang kiểm tra")),
    };
  }
  if (catalogStatus === "error") {
    return {
      state: "error",
      model: pair.model,
      effort: pair.effort,
      discrepancy,
      ownerConfigured,
      text: ownerMessage(pair.model
        ? t("Không tải được catalog. Model đã lưu {owner} vẫn được giữ; hãy thử lại trong panel sở hữu.", { owner })
        : t("Không tải được catalog của {name}. Hãy thử lại trong panel sở hữu.", { name })),
    };
  }
  if (!pair.model || !pair.effort) {
    return {
      state: "missing",
      model: pair.model,
      effort: pair.effort,
      discrepancy,
      ownerConfigured: false,
      text: t("Chưa chọn model cho {name}. Hãy chọn một model trước khi tạo notes.", { name }),
    };
  }
  const row = Array.isArray(catalog.models) ? catalog.models.find((item) => item?.id === pair.model) : null;
  if (!row || row.ready === false) {
    return {
      state: "unavailable",
      model: pair.model,
      effort: pair.effort,
      discrepancy,
      ownerConfigured,
      text: ownerMessage(t("Model đã lưu {owner} hiện không khả dụng. Hãy chọn model khác trong panel sở hữu.", { owner })),
    };
  }
  const message = t("Đã lưu {model} · effort {effort} cho notes.", { model: modelLabel(catalog, pair.model), effort: pair.effort });
  return {
    state: "ready",
    model: pair.model,
    effort: pair.effort,
    discrepancy,
    ownerConfigured,
    text: ownerMessage(message),
  };
}

function modelLabel(catalog, model) {
  const row = Array.isArray(catalog?.models) ? catalog.models.find((item) => item?.id === model) : null;
  return row?.display_name || model;
}

function ownerLabel(catalog, pair) {
  if (!pair.model) return t("chưa chọn model");
  const model = modelLabel(catalog, pair.model);
  return pair.effort ? t("{model} · effort {effort}", { model, effort: pair.effort }) : model;
}

export function selectEligibility(state, catalogs = {}) {
  const draft = state.draft;
  if (!draft) {
    return {
      valid: false,
      providerReady: false,
      workflowDirty: false,
      ownerDrift: false,
      dirty: false,
      canSave: false,
    };
  }
  const providers = catalogs.providers || {};
  const catalog = selectedProviderCatalog(draft, providers);
  const notesOwner = providerOwnerView(draft.notes.provider, catalog, state.saved?.notes);
  // The PATCH needs a committed owner pair, not a live provider/catalog. The
  // summary still exposes disconnected/error/unavailable state separately.
  const providerReady = draft.notes.provider === null || notesOwner?.ownerConfigured === true;
  const valid = LANGUAGES.includes(draft.asr.language)
    && LANGUAGES.includes(draft.notes.output_language)
    && providerReady
    && (draft.notes.provider === null || PROVIDERS.includes(draft.notes.provider));
  const workflowDirty = !equalWorkflowPreferences(state.saved, draft);
  const ownerDrift = Boolean(notesOwner?.discrepancy);
  const dirty = workflowDirty || ownerDrift;
  return {
    valid,
    providerReady,
    workflowDirty,
    ownerDrift,
    dirty,
    canSave: valid && dirty && state.status === "ready",
  };
}

export function selectPreferences(state, catalogs = {}) {
  const draft = state.draft;
  if (!draft) {
    return {
      status: state.status,
      loading: state.status === "loading",
      draft: null,
      asrOwner: null,
      notesOwner: null,
      providerOptions: [],
      outputLanguageOptions: [],
      canSave: false,
      dirty: false,
      ownerDrift: false,
      waiting: false,
      message: state.error?.text || null,
      error: state.error,
      notice: state.notice,
    };
  }
  const providers = catalogs.providers || {};
  const catalog = selectedProviderCatalog(draft, providers);
  const providerOptions = [
    { value: "", label: t("Không chọn provider notes"), disabled: false },
    { value: "chatgpt", label: "ChatGPT", disabled: false },
    { value: "antigravity", label: "Google Antigravity", disabled: false },
  ];
  const providerValue = draft.notes.provider || "";
  const asrCatalog = catalogs.asr || { status: catalogs.asrModels?.length ? "ready" : "loading", models: catalogs.asrModels };
  const asrOwner = asrOwnerView(asrCatalog, draft.asr.choice);
  const notesOwner = providerOwnerView(draft.notes.provider, catalog, state.saved?.notes);
  const eligibility = selectEligibility(state, catalogs);
  return {
    status: state.status,
    loading: state.status === "loading",
    saving: state.status === "saving",
    draft,
    saved: state.saved,
    version: draft.version,
    dirty: eligibility.dirty,
    workflowDirty: eligibility.workflowDirty,
    ownerDrift: eligibility.ownerDrift,
    canSave: eligibility.canSave,
    eligibility,
    asrOwner,
    notesOwner,
    providerOptions,
    providerValue,
    outputLanguageOptions: [
      { value: "en", label: "English", disabled: false },
      { value: "vi", label: t("Tiếng Việt"), disabled: false },
    ],
    message: state.error?.text || providerStatusText(draft.notes.provider, catalog),
    providerStatus: providerStatusText(draft.notes.provider, catalog),
    providerConnected: draft.notes.provider === null || catalog?.status === "connected",
    waiting: draft.notes.provider === null,
    error: state.error,
    notice: state.notice,
  };
}
