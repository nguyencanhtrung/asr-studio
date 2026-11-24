// Pure core for automatic-processing preferences shown in Settings > Devices.
// No DOM, network, timer, storage, clock or provider fallback: the view supplies the
// existing ASR/provider catalogs and this reducer emits data-only effects.

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
    || provider === null && (notes.model !== null || notes.reasoning_effort !== null)
    || provider !== null && (!validChoice(notes.model) || !validChoice(notes.reasoning_effort))
    || !LANGUAGES.includes(notes.output_language)
  ) return null;
  const version = Number(payload.version);
  if (!Number.isSafeInteger(version) || version < 1) return null;
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

function equalPreferences(left, right) {
  if (!left || !right) return false;
  return left.asr.language === right.asr.language
    && left.notes.provider === right.notes.provider
    && left.notes.model === right.notes.model
    && left.notes.reasoning_effort === right.notes.reasoning_effort
    && left.notes.output_language === right.notes.output_language;
}

function requestError(error) {
  const status = Number.isFinite(error?.status) ? error.status : 0;
  const payload = error?.body && typeof error.body === "object" ? error.body : null;
  const detail = payload && (payload.detail || payload.error);
  const code = detail && typeof detail === "object" && typeof detail.code === "string"
    ? detail.code
    : `HTTP_${status}`;
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

export function reduce(state, action) {
  switch (action.type) {
    case "provider/dialog-opened": {
      const request = state.request + 1;
      return done(
        { ...state, open: true, status: "loading", request, error: null, notice: null },
        [readEffect(request)],
      );
    }
    case "provider/processing-preferences-reconcile-requested": {
      if (!state.open || !state.saved || state.status === "saving") return done(state);
      const request = state.request + 1;
      return done(
        { ...state, status: "loading", request, error: null },
        [readEffect(request, true)],
      );
    }
    case "provider/dialog-closed":
      return done(
        { ...state, open: false, status: state.saved ? "ready" : "idle", request: state.request + 1, mutation: state.mutation + 1 },
        [cancelRead, cancelSave],
      );
    case "provider/processing-preferences-loaded": {
      if (action.meta?.request !== state.request || !state.open) return done(state);
      const preferences = normalizePreferences(action.response?.body);
      if (!preferences) {
        return done({ ...state, status: "error", error: { code: "invalid_response", text: t("Máy chủ trả về cài đặt xử lý tự động không hợp lệ.") } });
      }
      const reconciled = action.meta?.reconcile === true && state.draft && state.saved
        ? reconciledDraft(preferences, state.draft, state.saved)
        : clone(preferences);
      return done({ ...state, status: "ready", saved: preferences, draft: reconciled, error: null });
    }
    case "provider/processing-preferences-failed":
      if (action.meta?.request !== state.request || !state.open) return done(state);
      return done({ ...state, status: "error", error: requestError(action.error), notice: null });
    case "provider/processing-preference-edited":
      return fieldEdited(state, action);
    case "provider/processing-preferences-save-requested": {
      if (!state.open || !state.draft || state.status === "saving" || equalPreferences(state.saved, state.draft)) return done(state);
      const mutation = state.mutation + 1;
      return done({ ...state, status: "saving", mutation, error: null, notice: null }, [saveEffect(state, mutation)]);
    }
    case "provider/processing-preferences-saved": {
      if (action.meta?.mutation !== state.mutation || !state.open) return done(state);
      const preferences = normalizePreferences(action.response?.body);
      if (!preferences) {
        return done({ ...state, status: "ready", error: { code: "invalid_response", text: t("Máy chủ chưa xác nhận cài đặt xử lý tự động.") } });
      }
      return done(
        { ...state, status: "ready", saved: preferences, draft: clone(preferences), error: null, notice: t("Đã lưu cài đặt xử lý tự động.") },
        [SETTINGS_CHANGED],
      );
    }
    case "provider/processing-preferences-save-failed":
      if (action.meta?.mutation !== state.mutation || !state.open) return done(state);
      return done({ ...state, status: "ready", error: requestError(action.error), notice: null });
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
  if (catalog?.status === "connected") return catalog.account ? t("Đã kết nối với {account}", { account: catalog.account }) : t("Đã kết nối");
  return provider === "chatgpt"
    ? t("ChatGPT chưa được kết nối. Không tự chuyển sang provider khác.")
    : t("Google Antigravity chưa được kết nối. Không tự chuyển sang provider khác.");
}

function withAsrDiscrepancy(message, discrepancy) {
  return discrepancy
    ? [message, " ", t("Model ASR sở hữu khác lựa chọn workflow đã lưu. Mở Cài đặt → ASR Model để đồng bộ.")]
    : message;
}

function withOwnerDiscrepancy(message, discrepancy) {
  return discrepancy
    ? [message, " ", t("Cài đặt sở hữu khác recipe workflow đã lưu. Lưu để áp dụng cài đặt sở hữu.")]
    : message;
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
  const selected = validChoice(asr.defaultModel) ? asr.defaultModel : null;
  const selectedRow = selected ? models.find((choice) => choice.id === selected) : null;
  const hardware = typeof asr.hardwareModel === "string" && asr.hardwareModel ? asr.hardwareModel : null;
  const ownerChoice = selected || "auto";
  const discrepancy = validChoice(savedChoice) && savedChoice !== ownerChoice;
  let message;
  let state = "ready";
  if (asr.defaultDamaged && !selected) {
    state = "warning";
    message = t("Không đọc được model mặc định đã lưu, nên đang dùng theo phần cứng của máy. Hãy chọn lại.");
  } else if (selected) {
    const name = selectedRow ? choiceLabel(selectedRow) : selected;
    if (selectedRow && selectedRow.ready === false) {
      state = "warning";
      message = t("Model mặc định {model} chưa chạy được trên máy này. Lần chép lời Tự động sẽ bị từ chối cho đến khi bạn cài lại hoặc chọn mặc định khác.", { model: name });
    } else {
      message = t("Lần chép lời Tự động dùng {model}. Mỗi lần chạy vẫn chọn được model khác.", { model: name });
    }
  } else if (hardware) {
    message = t("Mặc định theo phần cứng của máy: {model}.", { model: hardware });
  } else {
    message = t("Mặc định theo phần cứng của máy.");
  }
  return {
    state,
    text: withAsrDiscrepancy(message, discrepancy),
    model: selected,
    discrepancy,
  };
}

function providerOwnerView(provider, catalog, saved) {
  if (!provider) return null;
  const name = provider === "chatgpt" ? "ChatGPT" : "Google Antigravity";
  if (!catalog || !catalog.status) return { state: "loading", text: t("Đang kiểm tra"), discrepancy: false };
  const model = validChoice(catalog.ownerModel) ? catalog.ownerModel : null;
  const effort = validChoice(catalog.ownerEffort) ? catalog.ownerEffort : null;
  const discrepancy = saved?.provider === provider
    && (saved.model !== model || saved.reasoning_effort !== effort);
  const message = model && effort
    ? t("Đã lưu {model} · effort {effort} cho notes.", { model: modelLabel(catalog, model), effort })
    : t("Chưa chọn model cho {name}. Hãy chọn một model trước khi tạo notes.", { name });
  return {
    state: model && effort ? "ready" : "missing",
    model,
    effort,
    discrepancy,
    text: withOwnerDiscrepancy(message, discrepancy),
  };
}

function modelLabel(catalog, model) {
  const row = Array.isArray(catalog?.models) ? catalog.models.find((item) => item?.id === model) : null;
  return row?.display_name || model;
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
  const notesOwner = providerOwnerView(draft.notes.provider, catalog, draft.notes);
  const providerReady = draft.notes.provider === null
    || Boolean(notesOwner?.model && notesOwner.effort);
  const valid = LANGUAGES.includes(draft.asr.language)
    && LANGUAGES.includes(draft.notes.output_language)
    && providerReady
    && (draft.notes.provider === null || PROVIDERS.includes(draft.notes.provider));
  const dirty = !equalPreferences(state.saved, draft);
  return {
    status: state.status,
    loading: state.status === "loading",
    saving: state.status === "saving",
    draft,
    saved: state.saved,
    version: draft.version,
    dirty,
    canSave: valid && dirty && state.status === "ready",
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
