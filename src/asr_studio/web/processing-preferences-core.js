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
  args: [],
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
  return Boolean(left && right) && JSON.stringify(left.asr) === JSON.stringify(right.asr)
    && JSON.stringify(left.notes) === JSON.stringify(right.notes);
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

function readEffect(request) {
  return {
    type: "fetch",
    key: "provider/processing-preferences",
    url: "/api/processing-preferences",
    init: GET,
    ok: "provider/processing-preferences-loaded",
    error: "provider/processing-preferences-failed",
    meta: { request },
  };
}

function saveEffect(state, mutation) {
  const { asr, notes } = state.draft;
  return {
    type: "fetch",
    key: "provider/processing-preferences-save",
    url: "/api/processing-preferences",
    init: {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({
        asr_choice: asr.choice,
        asr_language: asr.language,
        notes_provider: notes.provider,
        notes_model: notes.model,
        notes_reasoning_effort: notes.reasoning_effort,
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
  if (action.field === "asr_choice" && validChoice(action.value)) draft.asr.choice = action.value;
  if (action.field === "asr_language" && LANGUAGES.includes(action.value)) draft.asr.language = action.value;
  if (action.field === "notes_provider" && (action.value === null || PROVIDERS.includes(action.value))) {
    draft.notes.provider = action.value;
    // Changing the provider never silently selects its catalog's first model or account.
    if (action.value === null) {
      draft.notes.model = null;
      draft.notes.reasoning_effort = null;
    } else if (action.value !== state.draft.notes.provider) {
      draft.notes.model = null;
      draft.notes.reasoning_effort = null;
    }
  }
  if (action.field === "notes_model" && (action.value === null || validChoice(action.value))) draft.notes.model = action.value;
  if (action.field === "notes_reasoning_effort" && (action.value === null || validChoice(action.value))) draft.notes.reasoning_effort = action.value;
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
      return done({ ...state, status: "ready", saved: preferences, draft: clone(preferences), error: null, notice: null });
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

function choiceLabel(choice) {
  return ASR_MODEL_TEXT[choice.id]?.label || choice.model_name || choice.id;
}

function asrOptions(draft, asrModels) {
  const options = [];
  const choices = Array.isArray(asrModels) ? asrModels.filter((choice) => isObject(choice) && validChoice(choice.id)) : [];
  const known = choices.some((choice) => choice.id === draft.asr.choice);
  if (!known) options.push({ value: draft.asr.choice, label: t("{model} — không còn trong catalog", { model: draft.asr.choice }), disabled: true });
  for (const choice of choices) options.push({ value: choice.id, label: choiceLabel(choice), disabled: false, ready: choice.ready === true });
  if (!options.length) options.push({ value: draft.asr.choice, label: t("Đang tải catalog ASR…"), disabled: true });
  return options;
}

function selectedProviderCatalog(draft, providers) {
  const catalog = providers?.[draft.notes.provider];
  return catalog && typeof catalog === "object" ? catalog : null;
}

function modelOptions(draft, catalog) {
  if (!catalog) return [];
  const models = Array.isArray(catalog.models) ? catalog.models.filter((model) => isObject(model) && validChoice(model.id)) : [];
  const known = models.some((model) => model.id === draft.notes.model);
  const options = [];
  if (!known && draft.notes.model) options.push({ value: draft.notes.model, label: t("{model} — không còn trong catalog", { model: draft.notes.model }), disabled: true });
  for (const model of models) options.push({ value: model.id, label: model.display_name || model.id, disabled: false });
  if (!options.length) options.push({ value: "", label: t("Chưa có model khả dụng"), disabled: true });
  return options;
}

function providerStatusText(provider, catalog) {
  if (!provider) return t("Không chọn provider notes. Transcript có thể hoàn tất; notes sẽ chờ đến khi bạn chọn provider.");
  if (catalog?.status === "connected") return catalog.account ? t("Đã kết nối với {account}", { account: catalog.account }) : t("Đã kết nối");
  return provider === "chatgpt"
    ? t("ChatGPT chưa được kết nối. Không tự chuyển sang provider khác.")
    : t("Google Antigravity chưa được kết nối. Không tự chuyển sang provider khác.");
}

export function selectPreferences(state, catalogs = {}) {
  const draft = state.draft;
  if (!draft) {
    return { status: state.status, loading: state.status === "loading", draft: null, asrOptions: [], providerOptions: [], modelOptions: [], effortOptions: [], canSave: false, dirty: false, waiting: false, message: state.error?.text || null, error: state.error, notice: state.notice };
  }
  const providers = catalogs.providers || {};
  const catalog = selectedProviderCatalog(draft, providers);
  const providerOptions = [
    { value: "", label: t("Không chọn provider notes"), disabled: false },
    { value: "chatgpt", label: "ChatGPT", disabled: false },
    { value: "antigravity", label: "Google Antigravity", disabled: false },
  ];
  const providerValue = draft.notes.provider || "";
  const effortOptions = catalog?.effortOptions || [];
  const knownEffort = effortOptions.some((option) => option.value === draft.notes.reasoning_effort);
  const effortValue = knownEffort ? draft.notes.reasoning_effort : "";
  const providerReady = draft.notes.provider === null
    || catalog?.status === "connected" && modelOptions(draft, catalog).some((option) => option.value === draft.notes.model && !option.disabled) && knownEffort;
  const asrReady = asrOptions(draft, catalogs.asrModels).some((option) => option.value === draft.asr.choice && !option.disabled);
  const valid = asrReady
    && LANGUAGES.includes(draft.asr.language)
    && LANGUAGES.includes(draft.notes.output_language)
    && providerReady
    && (draft.notes.provider !== null ? validChoice(draft.notes.model) && validChoice(draft.notes.reasoning_effort) : draft.notes.model === null && draft.notes.reasoning_effort === null);
  return {
    status: state.status,
    loading: state.status === "loading",
    saving: state.status === "saving",
    draft,
    saved: state.saved,
    version: draft.version,
    dirty: !equalPreferences(state.saved, draft),
    canSave: valid && !equalPreferences(state.saved, draft) && state.status !== "saving",
    asrOptions: asrOptions(draft, catalogs.asrModels),
    providerOptions,
    providerValue,
    modelOptions: modelOptions(draft, catalog),
    modelValue: draft.notes.model || "",
    effortOptions,
    effortValue,
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
