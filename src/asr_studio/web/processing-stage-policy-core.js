// Pure state machine for the stage switches used by future Extension recordings.
// It deliberately does not share the legacy processing-preferences request: the
// backend version fence is shared, but the wire body and draft ownership are not.

const t = (text, params) => (params ? { text, params } : { text });
const done = (state, effects = []) => ({ state, effects });
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const STAGES = ["asr", "diarization", "notes"];
const ADMISSION_STATES = ["enabled", "draining", "disabled"];
const GET = {
  method: "GET",
  headers: { Accept: "application/json" },
  credentials: "same-origin",
  cache: "no-store",
};
const cancelRead = { type: "cancel", key: "provider/processing-stage-policy" };
const cancelSave = { type: "cancel", key: "provider/processing-stage-policy-save" };

export const initialState = {
  open: false,
  status: "idle", // idle | loading | ready | saving | paused | unsupported | error | conflict
  request: 0,
  mutation: 0,
  saved: null,
  draft: null,
  error: null,
  notice: null,
};

function validVersion(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function unsupportedVersion(payload) {
  return isObject(payload)
    && ((validVersion(payload.contract_version) && payload.contract_version !== 1)
      || (validVersion(payload.format_version) && payload.format_version !== 1));
}

function clonePolicy(policy) {
  return { asr: policy.asr, diarization: policy.diarization, notes: policy.notes };
}

function validPolicy(policy) {
  return isObject(policy) && STAGES.every((stage) => typeof policy[stage] === "boolean");
}

export function normalizeStagePolicy(payload) {
  if (!isObject(payload) || payload.contract_version !== 1 || payload.format_version !== 1
    || !validVersion(payload.preference_version) || !validPolicy(payload.policy) || !isObject(payload.admission)) return null;
  const { admission } = payload;
  if (!ADMISSION_STATES.includes(admission.state) || admission.format_version !== payload.format_version) return null;
  if (admission.reason !== null && typeof admission.reason !== "string") return null;
  if (typeof payload.updated_at !== "string" || payload.updated_at.length === 0) return null;
  return {
    contractVersion: payload.contract_version,
    formatVersion: payload.format_version,
    preferenceVersion: payload.preference_version,
    policy: clonePolicy(payload.policy),
    admission: {
      state: admission.state,
      formatVersion: admission.format_version,
      reason: admission.reason,
    },
    updatedAt: payload.updated_at,
  };
}

function cloneDraft(draft) {
  return clonePolicy(draft);
}

function samePolicy(left, right) {
  return Boolean(left && right) && STAGES.every((stage) => left[stage] === right[stage]);
}

function dirty(state) {
  return Boolean(state.saved && state.draft && !samePolicy(state.saved.policy, state.draft));
}

function readEffect(request, reconcile = false) {
  return {
    type: "fetch",
    key: "provider/processing-stage-policy",
    url: "/api/processing-stage-policy/v1",
    init: GET,
    ok: "provider/processing-stage-policy-loaded",
    error: "provider/processing-stage-policy-failed",
    meta: { request, reconcile },
  };
}

function errorFor(error, kind = "read") {
  const status = Number.isFinite(error?.status) ? error.status : 0;
  if (status === 404 || status === 405) {
    return { code: "unsupported", text: t("Máy chủ chưa hỗ trợ công tắc xử lý cho Extension mới.") };
  }
  if (status === 409 && kind === "save") {
    return { code: "conflict", text: t("Cài đặt đã đổi ở nơi khác. Bản nháp được giữ; hãy tải lại rồi kiểm tra trước khi lưu lại.") };
  }
  if (status === 409) {
    return { code: "conflict", text: t("Cài đặt đã đổi ở nơi khác. Hãy tải lại để kiểm tra phiên bản mới.") };
  }
  return { code: status ? `HTTP_${status}` : "network_error", text: t("Không thể xác minh công tắc xử lý Extension. Hãy thử lại.") };
}

function unsupportedError() {
  return { code: "unsupported_format", text: t("Phiên bản công tắc xử lý Extension chưa được ứng dụng này hỗ trợ.") };
}

function statusFor(saved) {
  return saved.admission.state === "enabled" ? "ready" : "paused";
}

function reconciledDraft(server, draft, baseline) {
  const next = cloneDraft(server.policy);
  if (!draft || !baseline) return next;
  for (const stage of STAGES) {
    if (draft[stage] !== baseline.policy[stage]) next[stage] = draft[stage];
  }
  return next;
}

function saveEffect(state, mutation) {
  const submitted = {
    expected_preference_version: state.saved.preferenceVersion,
    format_version: state.saved.formatVersion,
    ...cloneDraft(state.draft),
  };
  return {
    type: "fetch",
    key: "provider/processing-stage-policy-save",
    url: "/api/processing-stage-policy/v1",
    init: {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(submitted),
    },
    ok: "provider/processing-stage-policy-saved",
    error: "provider/processing-stage-policy-save-failed",
    meta: { mutation, submitted },
  };
}

function acknowledgesSave(saved, meta) {
  const submitted = meta?.submitted;
  return Boolean(saved && isObject(submitted))
    && saved.preferenceVersion === submitted.expected_preference_version + 1
    && saved.formatVersion === submitted.format_version
    && STAGES.every((stage) => saved.policy[stage] === submitted[stage]);
}

function startRead(state, reconcile = false) {
  if (!state.open) return done(state);
  const request = state.request + 1;
  return done({ ...state, status: "loading", request, error: null, notice: null }, [readEffect(request, reconcile)]);
}

function invalidResponse(state, source = "read") {
  return done({
    ...state,
    status: "error",
    error: {
      code: "invalid_response",
      source,
      text: t("Máy chủ trả về công tắc xử lý Extension không hợp lệ.")
    },
    notice: null,
  });
}

export function reduce(state, action) {
  switch (action.type) {
    case "provider/dialog-opened": {
      const request = state.request + 1;
      return done({ ...state, open: true, status: "loading", request, saved: null, draft: null, error: null, notice: null }, [readEffect(request)]);
    }
    case "provider/dialog-closed":
      return done({ ...initialState, request: state.request + 1, mutation: state.mutation + 1 }, [cancelRead, cancelSave]);
    case "provider/processing-stage-policy-reload-requested":
      return startRead(state, true);
    case "provider/processing-stage-policy-loaded": {
      if (!state.open || action.meta?.request !== state.request) return done(state);
      const saved = normalizeStagePolicy(action.response?.body);
      if (!saved) {
        if (unsupportedVersion(action.response?.body)) {
          return done({ ...state, status: "unsupported", error: unsupportedError(), notice: null });
        }
        return invalidResponse(state);
      }
      const draft = action.meta?.reconcile === true
        ? reconciledDraft(saved, state.draft, state.saved)
        : cloneDraft(saved.policy);
      return done({
        ...state,
        status: statusFor(saved),
        saved,
        draft,
        error: null,
        notice: null,
      });
    }
    case "provider/processing-stage-policy-failed": {
      if (!state.open || action.meta?.request !== state.request) return done(state);
      const error = errorFor(action.error);
      return done({ ...state, status: error.code === "unsupported" ? "unsupported" : error.code === "conflict" ? "conflict" : "error", error, notice: null });
    }
    case "provider/processing-stage-policy-edited": {
      if (!state.draft || state.status === "saving" || !STAGES.includes(action.field) || typeof action.value !== "boolean") return done(state);
      return done({ ...state, draft: { ...state.draft, [action.field]: action.value }, error: null, notice: null });
    }
    case "provider/processing-stage-policy-save-requested": {
      if (!state.open || !state.saved || !state.draft || !dirty(state) || !["ready", "paused"].includes(state.status)) return done(state);
      const mutation = state.mutation + 1;
      return done({ ...state, status: "saving", mutation, error: null, notice: null }, [saveEffect(state, mutation)]);
    }
    case "provider/processing-stage-policy-saved": {
      if (!state.open || action.meta?.mutation !== state.mutation) return done(state);
      const saved = normalizeStagePolicy(action.response?.body);
      if (!saved) {
        if (unsupportedVersion(action.response?.body)) {
          return done({ ...state, status: "unsupported", error: unsupportedError(), notice: null });
        }
        return invalidResponse({ ...state, status: "error" }, "mutation");
      }
      if (!acknowledgesSave(saved, action.meta)) return invalidResponse({ ...state, status: "error" }, "mutation");
      return done({ ...state, status: statusFor(saved), saved, draft: cloneDraft(saved.policy), error: null, notice: t("Đã lưu công tắc xử lý cho bản ghi Extension mới.") });
    }
    case "provider/processing-stage-policy-save-failed": {
      if (!state.open || action.meta?.mutation !== state.mutation) return done(state);
      const error = errorFor(action.error, "save");
      return done({ ...state, status: error.code === "unsupported" ? "unsupported" : error.code === "conflict" ? "conflict" : "error", error, notice: null });
    }
    default:
      return done(state);
  }
}

export function selectStagePolicy(state) {
  const saved = state?.saved;
  const draft = state?.draft;
  const admission = saved?.admission || null;
  const effective = draft ? {
    asr: draft.asr,
    diarization: draft.asr && draft.diarization,
    notes: draft.asr && draft.notes,
  } : null;
  const controls = draft ? {
    asr: state.status === "loading" || state.status === "saving",
    diarization: state.status === "loading" || state.status === "saving" || !draft.asr,
    notes: state.status === "loading" || state.status === "saving" || !draft.asr,
  } : { asr: true, diarization: true, notes: true };
  const dirtyState = dirty(state || {});
  const message = state?.status === "paused"
    ? state.saved?.admission.state === "draining"
      ? t("Máy chủ đang chuyển trạng thái. Stage chưa chạy.")
      : t("Máy chủ chưa cho phép chạy stage. Bản ghi không được xem là đã xử lý.")
    : state?.status === "unsupported"
      ? t("Máy chủ chưa công bố hỗ trợ công tắc xử lý cho Extension mới.")
      : null;
  return {
    status: state?.status || "idle",
    draft,
    saved,
    admission,
    effective,
    controls,
    dirty: dirtyState,
    loading: state?.status === "loading",
    saving: state?.status === "saving",
    canSave: Boolean(state?.open && draft && saved && dirtyState && ["ready", "paused"].includes(state.status)),
    version: saved?.preferenceVersion || null,
    formatVersion: saved?.formatVersion || null,
    notice: state?.notice || null,
    error: state?.error || null,
    message,
  };
}
