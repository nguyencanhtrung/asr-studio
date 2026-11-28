// Pure core for the durable automatic-processing workflow on the current meeting.
// The current meeting owns the visible scope. Navigation and visibility cancel reads;
// request/epoch guards make late replies inert even when an abort cannot stop them.

import { errorMessageForCode } from "./shell-core.js";
import { normalizePreferences } from "./processing-preferences-core.js";

const t = (text, params) => (params ? { text, params } : { text });
const done = (state, effects = []) => ({ state, effects });
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const TERMINAL = new Set(["succeeded", "canceled", "deleted"]);
const GLOBAL_ACTION_BLOCKED_REASONS = new Set([
  "stage_policy_admission_disabled",
  "unknown_stage_policy",
  "unknown_stage_policy_format",
  "unknown_stage_policy_gate",
]);
const STAGE_HOLD_REASONS = new Set([
  ...GLOBAL_ACTION_BLOCKED_REASONS,
  "diarization_rollback_fence",
]);
const MUTATING_ACTIONS = new Set(["retry_notes", "apply_current_settings", "use_current_source"]);
const POLL_DELAY = 1000;
const POLL_ERROR_DELAY = 2000;

const cancelPoll = { type: "cancel", key: "processing/poll" };
const cancelMeetingRead = { type: "cancel", key: "processing/meeting-read" };
const cancelActionRead = { type: "cancel", key: "processing/action-read" };
const cancelAction = { type: "cancel", key: "processing/action" };

export const initialState = {
  meetingId: null,
  loadId: 0,
  workflow: null,
  visible: false,
  hidden: false,
  readId: 0,
  readPending: false,
  pollId: 0,
  pollPending: false,
  action: null,
  error: null,
  notice: null,
};

function getMeeting(payload) {
  const meeting = payload && isObject(payload.meeting) ? payload.meeting : payload;
  return isObject(meeting) ? meeting : null;
}

function getProcessing(payload) {
  const candidate = payload && isObject(payload.processing) ? payload.processing : payload;
  return candidate === null ? null : isObject(candidate) ? candidate : null;
}

export function normalizeWorkflow(payload) {
  const source = getProcessing(payload);
  if (!source) return null;
  if (typeof source.id !== "string" || typeof source.meeting_id !== "string" || typeof source.state !== "string") return null;
  const epoch = Number(source.epoch);
  if (!Number.isSafeInteger(epoch) || epoch < 1 || !Array.isArray(source.steps) || !isObject(source.actions)) return null;
  const steps = source.steps
    .filter((step) => isObject(step) && (step.kind === "transcription" || step.kind === "notes") && typeof step.state === "string")
    .map((step) => ({ ...step }));
  if (steps.length !== 2 || !steps.some((step) => step.kind === "transcription") || !steps.some((step) => step.kind === "notes")) return null;
  const actionBlockedReasonPresent = Object.hasOwn(source, "action_blocked_reason");
  return {
    ...source,
    epoch,
    preferences_version: Number.isSafeInteger(Number(source.preferences_version)) ? Number(source.preferences_version) : null,
    steps,
    current_source: isObject(source.current_source) ? { ...source.current_source } : null,
    action_blocked_reason_present: actionBlockedReasonPresent,
    action_blocked_reason: actionBlockedReasonPresent && typeof source.action_blocked_reason === "string"
      ? source.action_blocked_reason
      : null,
    actions: {
      cancel: source.actions.cancel === true,
      retry_notes: source.actions.retry_notes === true,
      apply_current_settings: source.actions.apply_current_settings === true,
      use_current_source: source.actions.use_current_source === true,
    },
  };
}

function stepOf(workflow, kind) {
  return workflow?.steps?.find((step) => step.kind === kind) || null;
}

function active(workflow) {
  return Boolean(workflow) && !TERMINAL.has(workflow.state);
}

function hasManualStatus({ task = null, generation = null, notes = null } = {}) {
  return Boolean(task)
    || Boolean(generation)
    || typeof notes?.status === "string" && notes.status.length > 0
    || Number(notes?.count) > 0;
}

export function shouldRenderAutomaticStatus(workflow, manual = {}) {
  if (!workflow || hasManualStatus(manual)) return false;
  return workflow.state === "canceled" || !TERMINAL.has(workflow.state);
}

function actionMatches(state, action) {
  return Boolean(state.action)
    && state.meetingId === action.meta?.meetingId
    && state.action.requestId === action.meta?.requestId;
}

function request(method, url, ok, error, meta, body) {
  const init = {
    method,
    headers: method === "GET" ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  return { type: "fetch", key: meta.key, url, init, ok, error, meta };
}

function processingRead(state, meta) {
  return request(
    "GET",
    `/api/meetings/${encodeURIComponent(state.meetingId)}/processing`,
    "processing/action-state-loaded",
    "processing/action-state-failed",
    { ...meta, key: "processing/action-read", meetingId: state.meetingId },
  );
}

function meetingRead(state, readId) {
  return request(
    "GET",
    `/api/meetings/${encodeURIComponent(state.meetingId)}/processing`,
    "processing/meeting-read-loaded",
    "processing/meeting-read-failed",
    { key: "processing/meeting-read", meetingId: state.meetingId, loadId: state.loadId, readId },
  );
}

function actionFailure(error) {
  const status = Number.isFinite(error?.status) ? error.status : 0;
  const payload = error?.body && typeof error.body === "object" ? error.body : null;
  const detail = payload && (payload.detail || payload.error);
  const code = detail && typeof detail === "object" && typeof detail.code === "string" ? detail.code : `HTTP_${status}`;
  return {
    code,
    text: errorMessageForCode(code, status) || t("Không thể hoàn tất thao tác xử lý. Hãy tải lại trạng thái rồi thử lại."),
  };
}

function pollEffect(state, delay = POLL_DELAY) {
  return {
    type: "schedule",
    key: "processing/poll",
    delay,
    action: { type: "processing/poll-due", meetingId: state.meetingId, loadId: state.loadId },
  };
}

function refreshEffect(meetingId) {
  return { type: "call", key: "processing/refresh-meeting", name: "meeting.refresh", args: [meetingId] };
}

function pollAgain(state, delay = POLL_DELAY) {
  return active(state.workflow) && state.visible && !state.hidden ? [pollEffect(state, delay)] : [];
}

function install(state, meeting, { loadId, visible = true } = {}) {
  if (!meeting || typeof meeting.id !== "string") return done(state);
  if (state.meetingId && state.meetingId !== meeting.id) return done(state);
  if (Number.isSafeInteger(loadId) && loadId < state.loadId) return done(state);
  const workflow = normalizeWorkflow(meeting.processing);
  const hasProcessing = Object.hasOwn(meeting, "processing");
  const readId = state.readId + 1;
  const next = {
    ...state,
    meetingId: meeting.id,
    loadId: Number.isSafeInteger(loadId) ? Math.max(state.loadId, loadId) : state.loadId,
    workflow,
    visible,
    pollPending: false,
    readId,
    readPending: !hasProcessing,
    action: null,
    error: null,
    notice: null,
  };
  return done(next, [cancelPoll, cancelMeetingRead, ...(hasProcessing ? [] : [meetingRead(next, readId)]), ...pollAgain(next)]);
}

function reset(state, meetingId = null) {
  return done(
    {
      ...initialState,
      meetingId,
      loadId: state.loadId + 1,
      hidden: state.hidden,
    },
    [cancelPoll, cancelMeetingRead, cancelActionRead, cancelAction],
  );
}

function initialIntent(kind, workflow) {
  if (kind === "apply_current_settings" && workflow?.pinned) {
    return {
      asr: { choice: workflow.pinned.asr_choice, language: workflow.pinned.asr_language },
      notes: {
        provider: workflow.pinned.notes_provider,
        model: workflow.pinned.notes_model,
        reasoning_effort: workflow.pinned.notes_reasoning_effort,
        output_language: workflow.pinned.notes_output_language,
      },
    };
  }
  if (kind === "use_current_source") return sourceValues(workflow);
  if (kind === "retry_notes") {
    const notes = stepOf(workflow, "notes");
    return notes?.source_snapshot_id ? { source_snapshot_id: notes.source_snapshot_id, recipe_version: notes.recipe_version } : null;
  }
  return null;
}

function startAction(state, kind, requestId, intended = null) {
  if (!state.workflow || state.action || !state.workflow.actions?.[kind] || isMutationBlocked(state.workflow, kind)) return done(state);
  const action = { kind, requestId, phase: kind === "cancel" ? "sending" : "reading", intended: intended || initialIntent(kind, state.workflow) };
  if (kind === "cancel") {
    const body = { request_id: requestId, expected_pipeline_epoch: state.workflow.epoch };
    return done(
      { ...state, action, error: null, notice: null, pollPending: false },
      [cancelPoll, request("POST", `/api/meetings/${encodeURIComponent(state.meetingId)}/processing/cancel`, "processing/action-succeeded", "processing/action-failed", { key: "processing/action", meetingId: state.meetingId, requestId, kind }, body)],
    );
  }
  return done({ ...state, action, error: null, notice: null, pollPending: false }, [cancelPoll, processingRead(state, { requestId, kind })]);
}

function sourceValues(workflow) {
  const source = workflow?.current_source;
  if (!source) return null;
  const generationId = source.generation_id;
  if (generationId !== null && (typeof generationId !== "string" || !generationId)) return null;
  const sourceVersion = source.source_version === null ? null : Number(source.source_version);
  if ((generationId === null) !== (sourceVersion === null)) return null;
  const values = {
    generation_id: generationId,
    source_version: sourceVersion,
    metadata_version: Number(source.metadata_version),
    notes_source_version: Number(source.notes_source_version),
  };
  return (values.source_version === null || Number.isSafeInteger(values.source_version))
    && Number.isSafeInteger(values.metadata_version)
    && values.metadata_version > 0
    && Number.isSafeInteger(values.notes_source_version)
    && values.notes_source_version > 0
    ? values
    : null;
}

function postReadAction(state, workflow) {
  const action = state.action;
  const epoch = Number(workflow?.epoch);
  if (!action || !workflow || !Number.isSafeInteger(epoch)) return done({ ...state, action: null, error: { code: "invalid_response", text: t("Máy chủ trả về trạng thái xử lý không hợp lệ.") } });
  if (workflow.meeting_id !== state.meetingId || !workflow.actions) return done({ ...state, action: null, error: { code: "meeting_changed", text: t("Cuộc họp đã thay đổi. Hãy tải lại trạng thái rồi thử lại.") } });
  if (action.kind === "apply_current_settings") {
    const nextAction = { ...action, phase: "reading-preferences", epoch };
    return done(
      { ...state, workflow, action: nextAction },
      [request("GET", "/api/processing-preferences", "processing/apply-preferences-loaded", "processing/apply-preferences-failed", { key: "processing/action-read", meetingId: state.meetingId, requestId: action.requestId, kind: action.kind, epoch })],
    );
  }
  if (action.kind === "use_current_source") {
    const source = sourceValues(workflow);
    if (!source) return done({ ...state, workflow, action: null, error: { code: "source_changed", text: t("Chưa có transcript nguồn mới để áp dụng.") } });
    const nextAction = { ...action, phase: "sending", epoch, intended: { ...source } };
    return done(
      { ...state, workflow, action: nextAction },
      [request("POST", `/api/meetings/${encodeURIComponent(state.meetingId)}/processing/use-current-source`, "processing/action-succeeded", "processing/action-failed", { key: "processing/action", meetingId: state.meetingId, requestId: action.requestId, kind: action.kind }, { expected_pipeline_epoch: epoch, expected_generation_id: source.generation_id, expected_source_version: source.source_version, expected_metadata_version: source.metadata_version, expected_notes_source_version: source.notes_source_version })],
    );
  }
  if (action.kind === "retry_notes") {
    const notes = stepOf(workflow, "notes");
    const snapshot = notes && typeof notes.source_snapshot_id === "string" ? notes.source_snapshot_id : null;
    const recipe = Number(notes?.recipe_version);
    if (!snapshot || !Number.isSafeInteger(recipe)) return done({ ...state, workflow, action: null, error: { code: "source_changed", text: t("Nguồn notes đã thay đổi. Hãy tải lại trạng thái rồi thử lại.") } });
    const nextAction = { ...action, phase: "sending", epoch, intended: { source_snapshot_id: snapshot, recipe_version: recipe } };
    return done(
      { ...state, workflow, action: nextAction },
      [request("POST", `/api/meetings/${encodeURIComponent(state.meetingId)}/processing/retry-notes`, "processing/action-succeeded", "processing/action-failed", { key: "processing/action", meetingId: state.meetingId, requestId: action.requestId, kind: action.kind }, { request_id: action.requestId, expected_pipeline_epoch: epoch, source_snapshot_id: snapshot, confirmed_recipe_version: recipe })],
    );
  }
  return done({ ...state, action: null, error: { code: "invalid_request", text: t("Thao tác xử lý không hợp lệ.") } });
}

function applyPreferencesRead(state, action) {
  if (!actionMatches(state, action) || action.meta.kind !== "apply_current_settings") return done(state);
  const preferences = normalizePreferences(action.response?.body);
  const epoch = Number(action.meta.epoch);
  if (!preferences || !Number.isSafeInteger(epoch)) return done({ ...state, action: null, error: { code: "invalid_response", text: t("Máy chủ trả về cài đặt xử lý không hợp lệ.") } });
  const nextAction = { ...state.action, phase: "sending", epoch, intended: preferences };
  return done(
    { ...state, action: nextAction },
    [request("POST", `/api/meetings/${encodeURIComponent(state.meetingId)}/processing/apply-current-settings`, "processing/action-succeeded", "processing/action-failed", { key: "processing/action", meetingId: state.meetingId, requestId: state.action.requestId, kind: state.action.kind }, { expected_pipeline_epoch: epoch, expected_preferences_version: preferences.version })],
  );
}

function actionSucceeded(state, action) {
  if (!actionMatches(state, action)) return done(state);
  const workflow = normalizeWorkflow(action.response?.body);
  if (!workflow || workflow.meeting_id !== state.meetingId) return done({ ...state, action: null, error: { code: "invalid_response", text: t("Máy chủ chưa xác nhận trạng thái xử lý.") } });
  const kind = state.action.kind;
  const notice = kind === "retry_notes"
    ? t("Đã yêu cầu thử lại notes từ nguồn transcript hiện tại. Transcript không chạy lại.")
    : kind === "use_current_source"
      ? state.action.intended?.generation_id
        ? t("Đã áp dụng nguồn transcript đang hiển thị. Notes sẽ dùng phiên bản đã xác nhận.")
        : stageEnabled(workflow, "asr")
          ? stageEnabled(workflow, "notes")
            ? t("Đã xác nhận thông tin cuộc họp. ASR tự động sẽ chép lời bản ghi gốc.")
            : t("Đã xác nhận thông tin cuộc họp. ASR sẽ chép lời bản ghi gốc; Notes đang tắt.")
          : t("Đã xác nhận thông tin cuộc họp. ASR và Notes đang tắt; bản ghi sẽ giữ nguyên.")
      : kind === "apply_current_settings"
        ? t("Đã áp dụng cài đặt đang hiển thị cho workflow này.")
        : t("Đã hủy xử lý tự động. Workflow đã được khóa, không tự chạy lại.");
  const next = { ...state, workflow, action: null, pollPending: false, error: null, notice };
  const refreshSource = kind === "use_current_source" && state.action.intended?.generation_id;
  const effects = [cancelPoll, ...(refreshSource ? [refreshEffect(state.meetingId)] : []), ...pollAgain(next)];
  return done(next, effects);
}

export function reduce(state, action) {
  switch (action.type) {
    case "history/meeting-requested":
      return reset(state, typeof action.meetingId === "string" ? action.meetingId : null);
    case "history/meeting-link-clicked":
      return reset(state, typeof action.meetingId === "string" ? action.meetingId : null);
    case "history/meeting-change-received":
      if (state.meetingId !== action.meetingId) return done(state);
      return reset(state, state.meetingId);
    case "history/home-clicked":
    case "history/home-shown":
    case "history/popped":
      return reset(state);
    case "task/visibility-changed": {
      const hidden = action.hidden === true;
      const next = { ...state, hidden, pollPending: hidden ? false : state.pollPending };
      if (hidden) return done(next, [cancelPoll]);
      return done(next, pollAgain(next));
    }
    case "history/meeting-loaded": {
      const meeting = getMeeting(action.response?.body);
      return install(state, meeting, { loadId: Number(action.meta?.loadId), visible: true });
    }
    case "history/meeting-fetched":
      return install(state, getMeeting(action.meeting), { visible: true });
    case "history/recording-refresh-loaded":
    case "history/asr-settings-read":
    case "task/refresh-loaded":
    case "transcript/reload-loaded": {
      const meeting = getMeeting(action.response?.body);
      if (state.meetingId && meeting?.id !== state.meetingId) return done(state);
      return install(state, meeting, { visible: true });
    }
    case "processing/poll-due": {
      if (action.meetingId !== state.meetingId || action.loadId !== state.loadId || state.hidden || !state.visible || !active(state.workflow) || state.pollPending) return done(state);
      const pollId = state.pollId + 1;
      return done(
        { ...state, pollId, pollPending: true },
        [request("GET", `/api/meetings/${encodeURIComponent(state.meetingId)}/processing`, "processing/poll-loaded", "processing/poll-failed", { key: "processing/poll", meetingId: state.meetingId, loadId: state.loadId, pollId })],
      );
    }
    case "processing/poll-loaded": {
      const meta = action.meta || {};
      if (meta.meetingId !== state.meetingId || meta.loadId !== state.loadId || meta.pollId !== state.pollId || !state.pollPending) return done(state);
      const workflow = normalizeWorkflow(action.response?.body);
      if (!workflow || workflow.meeting_id !== state.meetingId) return done({ ...state, pollPending: false, error: { code: "invalid_response", text: t("Máy chủ trả về trạng thái xử lý không hợp lệ.") } }, pollAgain(state, POLL_ERROR_DELAY));
      const becameComplete = workflow.state === "succeeded" && state.workflow?.state !== "succeeded";
      const next = { ...state, workflow, pollPending: false, error: null, notice: workflow.epoch === state.workflow?.epoch ? state.notice : null };
      return done(next, [...(becameComplete ? [refreshEffect(state.meetingId)] : []), ...pollAgain(next)]);
    }
    case "processing/meeting-read-loaded": {
      const meta = action.meta || {};
      if (meta.meetingId !== state.meetingId || meta.loadId !== state.loadId || meta.readId !== state.readId || !state.readPending) return done(state);
      const workflow = normalizeWorkflow(action.response?.body);
      if (!workflow || workflow.meeting_id !== state.meetingId) {
        return done({ ...state, readPending: false, error: { code: "invalid_response", text: t("Máy chủ trả về trạng thái xử lý không hợp lệ.") } });
      }
      const next = { ...state, workflow, readPending: false, error: null, notice: null };
      return done(next, pollAgain(next));
    }
    case "processing/meeting-read-failed": {
      const meta = action.meta || {};
      if (meta.meetingId !== state.meetingId || meta.loadId !== state.loadId || meta.readId !== state.readId || !state.readPending) return done(state);
      const status = Number(action.error?.status);
      if (status === 404) return done({ ...state, workflow: null, readPending: false, error: null, notice: null });
      return done({ ...state, readPending: false, error: { code: "processing_read_failed", text: t("Không thể đọc trạng thái xử lý tự động. Hãy tải lại cuộc họp.") }, notice: null });
    }
    case "processing/poll-failed": {
      const meta = action.meta || {};
      if (meta.meetingId !== state.meetingId || meta.loadId !== state.loadId || meta.pollId !== state.pollId || !state.pollPending) return done(state);
      const next = { ...state, pollPending: false, error: actionFailure(action.error) };
      return done(next, pollAgain(next, POLL_ERROR_DELAY));
    }
    case "processing/cancel-clicked":
      return startAction(state, "cancel", action.requestId);
    case "processing/retry-notes-clicked":
      return startAction(state, "retry_notes", action.requestId, action.intended || null);
    case "processing/apply-settings-clicked":
      return startAction(state, "apply_current_settings", action.requestId, action.intended || null);
    case "processing/use-source-clicked":
      return startAction(state, "use_current_source", action.requestId, action.intended || null);
    case "processing/action-state-loaded": {
      if (!actionMatches(state, action)) return done(state);
      const workflow = normalizeWorkflow(action.response?.body);
      return postReadAction(state, workflow);
    }
    case "processing/apply-preferences-loaded":
      return applyPreferencesRead(state, action);
    case "processing/action-state-failed":
    case "processing/apply-preferences-failed":
    case "processing/action-failed":
      if (!actionMatches(state, action)) return done(state);
      {
        const next = { ...state, action: null, pollPending: false, error: actionFailure(action.error), notice: null };
        return done(next, [cancelPoll, ...pollAgain(next, POLL_ERROR_DELAY)]);
      }
    case "processing/action-succeeded":
      return actionSucceeded(state, action);
    default:
      return done(state);
  }
}

const WAITING_REASONS = {
  worker_offline: t("Đang chờ worker chép lời.") ,
  model_unavailable: t("Model ASR đang chờ sẵn sàng.") ,
  diarization_model_unavailable: t("Model nhận diện người nói đang chờ sẵn sàng."),
  slot_busy: t("Đang chờ slot xử lý của máy."),
  provider_unconfigured: t("Chưa chọn provider notes. Transcript có thể hoàn tất; notes sẽ chờ."),
  provider_unavailable: t("Provider notes hiện chưa khả dụng."),
  account_changed: t("Tài khoản provider đã thay đổi. Hãy xác nhận lại cài đặt."),
  recipe_changed: t("Công thức notes đã thay đổi. Hãy xác nhận lại rồi thử lại."),
  source_confirmation_required: t("Cần xác nhận nguồn dữ liệu đang hiển thị."),
  interrupted_recording: t("Bản ghi bị gián đoạn. Xử lý tự động chỉ chạy khi bạn xác nhận chép lời phần đã ghi."),
  transcript_required: t("Đang chờ transcript trước khi tạo notes."),
  canceled_by_operator: t("Đã hủy bởi người dùng."),
  stage_disabled: t("Stage này được tắt trong recipe của workflow."),
  stage_policy_admission_disabled: t("Máy chủ chưa cho phép tự động xử lý. Kiểm tra cấu hình rồi tải lại trạng thái; ứng dụng không tự thử lại."),
  unknown_stage_policy: t("Máy chủ chưa nhận diện được cấu hình xử lý nên workflow đang được giữ lại. Kiểm tra cấu hình rồi tải lại trạng thái."),
  unknown_stage_policy_format: t("Định dạng cấu hình xử lý không được hỗ trợ nên workflow đang được giữ lại. Cập nhật cấu hình rồi tải lại trạng thái."),
  unknown_stage_policy_gate: t("Máy chủ chưa xác minh được quyền chạy tự động nên workflow đang được giữ lại. Kiểm tra cấu hình rồi tải lại trạng thái."),
  diarization_rollback_fence: t("Máy chủ đang tạm giữ chế độ không nhận dạng người nói vì lý do an toàn. Chờ người vận hành mở lại rồi tải lại trạng thái."),
};

function stepHoldReason(workflow, reasons = STAGE_HOLD_REASONS) {
  return workflow?.steps?.find((step) => reasons.has(step.waiting_reason))?.waiting_reason || null;
}

function actionBlockedReason(workflow) {
  if (!workflow || TERMINAL.has(workflow.state)) return null;
  if (workflow.action_blocked_reason_present) {
    return GLOBAL_ACTION_BLOCKED_REASONS.has(workflow.action_blocked_reason) ? workflow.action_blocked_reason : null;
  }
  return stepHoldReason(workflow, GLOBAL_ACTION_BLOCKED_REASONS);
}

function displayHoldReason(workflow) {
  if (!workflow || TERMINAL.has(workflow.state)) return null;
  if (workflow.action_blocked_reason_present) {
    if (STAGE_HOLD_REASONS.has(workflow.action_blocked_reason)) return workflow.action_blocked_reason;
    return stepHoldReason(workflow, new Set(["diarization_rollback_fence"]));
  }
  return stepHoldReason(workflow);
}

function stepReasonFor(workflow, step) {
  if (!step) return null;
  if (workflow?.action_blocked_reason_present
    && workflow.action_blocked_reason === null
    && GLOBAL_ACTION_BLOCKED_REASONS.has(step.waiting_reason)) return null;
  return step.waiting_reason;
}

function isMutationBlocked(workflow, kind) {
  return MUTATING_ACTIONS.has(kind) && Boolean(actionBlockedReason(workflow));
}

function hasAvailableMutationAction(workflow) {
  return Boolean(workflow)
    && !actionBlockedReason(workflow)
    && (workflow.actions.retry_notes === true
      || workflow.actions.apply_current_settings === true
      || workflow.actions.use_current_source === true);
}

function holdText(reason) {
  return WAITING_REASONS[reason] || t("Workflow đang được giữ lại để kiểm tra cấu hình xử lý.");
}

function stageEnabled(workflow, kind) {
  const policy = workflow?.stage_policy;
  if (!isObject(policy) || policy.format_version !== 1 || typeof policy.asr !== "boolean"
    || typeof policy.diarization !== "boolean" || typeof policy.notes !== "boolean") return true;
  if (kind === "asr") return policy.asr;
  return policy.asr && policy[kind];
}

function stageSkipped(workflow, kind) {
  return !stageEnabled(workflow, kind) || stepOf(workflow, kind)?.state === "skipped";
}

function stepView(step, waitingReason = step?.waiting_reason) {
  if (!step) return { state: "idle", value: t("Chưa bắt đầu"), reason: null };
  if (["accepted", "running", "stopping"].includes(step.state)) {
    return { state: "active", value: step.state === "stopping" ? t("Đang dừng") : t("Đang xử lý"), reason: null };
  }
  if (step.state === "skipped") return { state: "skipped", value: t("Đã bỏ qua"), reason: WAITING_REASONS[waitingReason] || WAITING_REASONS.stage_disabled };
  if (step.state === "succeeded") return { state: "done", value: t("Hoàn tất"), reason: null };
  if (["failed", "needs_attention"].includes(step.state)) return { state: "problem", value: t("Cần xử lý"), reason: WAITING_REASONS[waitingReason] || t("Workflow cần bạn kiểm tra.") };
  if (step.state === "canceled") return { state: "warning", value: t("Đã hủy"), reason: WAITING_REASONS[waitingReason] || null };
  return { state: "warning", value: t("Đang chờ"), reason: WAITING_REASONS[waitingReason] || t("Đang chờ xử lý.") };
}

function overallText(workflow, holdReason = displayHoldReason(workflow)) {
  if (!workflow) return null;
  const asrSkipped = stageSkipped(workflow, "asr");
  const notesSkipped = stageSkipped(workflow, "notes");
  if (workflow.state === "succeeded") {
    if (asrSkipped && notesSkipped) return t("Bản ghi đã lưu. Transcript và notes đã bỏ qua theo cài đặt.");
    if (asrSkipped) return t("Bản ghi đã lưu. Transcript đã bỏ qua theo cài đặt; Notes không chạy.");
    if (notesSkipped) return t("Transcript đã hoàn tất. Notes đã bỏ qua theo cài đặt.");
    return t("Transcript và notes đã hoàn tất.");
  }
  if (workflow.state === "canceled") return t("Đã hủy xử lý tự động. Workflow không tự chạy lại.");
  if (holdReason) return holdText(holdReason);
  if (workflow.state === "running") {
    if (asrSkipped && notesSkipped) return t("Đang hoàn tất bản ghi. Transcript và notes đã tắt.");
    if (asrSkipped) return t("Đang xử lý notes. Transcript đã bỏ qua theo cài đặt.");
    if (notesSkipped) return t("Đang xử lý transcript. Notes đã bỏ qua theo cài đặt.");
    return t("Đang xử lý transcript và notes. Trạng thái sẽ cập nhật khi workflow tiến triển.");
  }
  if (workflow.state === "needs_attention") {
    return hasAvailableMutationAction(workflow)
      ? t("Workflow cần một xác nhận trước khi tiếp tục.")
      : t("Workflow đang chờ worker, model hoặc provider đã chọn.");
  }
  return t("Workflow đang chờ worker, model hoặc provider đã chọn.");
}

function sourceText(source, workflow = null, { holdReason = null, canConfirm = false } = {}) {
  if (!source) return null;
  if (holdReason) return t("Nguồn hiện tại được giữ nguyên trong lúc workflow chờ xử lý. Chưa có thao tác xác nhận nào khả dụng.");
  if (source.generation_id === null && source.source_version === null) {
    if (!canConfirm) {
      return stageEnabled(workflow, "asr")
        ? t("Chỉ có thông tin cuộc họp · phiên bản {metadata}. Transcript chưa được tạo.", { metadata: source.metadata_version })
        : t("Chỉ có thông tin cuộc họp · phiên bản {metadata}. Transcript và notes đang tắt.", { metadata: source.metadata_version });
    }
    return stageEnabled(workflow, "asr")
      ? t("Chỉ có thông tin cuộc họp · phiên bản {metadata}. Xác nhận để chép lời bản ghi gốc.", {
        metadata: source.metadata_version,
      })
      : t("Chỉ có thông tin cuộc họp · phiên bản {metadata}. Xác nhận nguồn hiện tại.", {
        metadata: source.metadata_version,
      });
  }
  if (!stageEnabled(workflow, "asr")) {
    return t("Nguồn đã chọn · nội dung phiên bản {source} · thông tin cuộc họp phiên bản {metadata}", {
      source: source.source_version,
      metadata: source.metadata_version,
    });
  }
  return t("Transcript đã chọn · nội dung phiên bản {source} · thông tin cuộc họp phiên bản {metadata}", {
    source: source.source_version,
    metadata: source.metadata_version,
  });
}

export function selectProcessing(state) {
  const workflow = state.workflow;
  if (!workflow) return { visible: false, workflow: null, steps: [], actions: {}, message: null, error: state.error, notice: state.notice, action: state.action };
  const action = state.action;
  const intent = action?.intended;
  const asrEnabled = stageEnabled(workflow, "asr");
  const notesEnabled = stageEnabled(workflow, "notes");
  const holdReason = displayHoldReason(workflow);
  const globalHoldReason = actionBlockedReason(workflow);
  const transcriptStep = stepOf(workflow, "transcription");
  const notesStep = stepOf(workflow, "notes");
  const transcript = stepView(transcriptStep, stepReasonFor(workflow, transcriptStep));
  const notes = stepView(notesStep, stepReasonFor(workflow, notesStep));
  const canUseSource = !globalHoldReason && workflow.actions.use_current_source === true;
  const source = sourceText(workflow.current_source, workflow, {
    holdReason: globalHoldReason,
    canConfirm: canUseSource,
  });
  const interruptedStart = canUseSource && !holdReason && workflow.state === "needs_attention" &&
    stepOf(workflow, "transcription")?.waiting_reason === "interrupted_recording";
  const intended = globalHoldReason
    ? null
    : action?.kind === "apply_current_settings" && intent?.asr
      ? t("Áp dụng ASR {choice} · {language}; notes {provider} · {model} · {effort} · {output}.", {
      choice: intent.asr.choice,
      language: intent.asr.language,
      provider: intent.notes.provider || t("chưa chọn provider"),
      model: intent.notes.model || t("chưa chọn model"),
      effort: intent.notes.reasoning_effort || t("mặc định"),
      output: intent.notes.output_language,
      })
      : action?.kind === "use_current_source" && intent && Object.hasOwn(intent, "generation_id")
        ? sourceText(intent, workflow, { canConfirm: canUseSource })
        : action?.kind === "retry_notes" && intent?.source_snapshot_id
          ? t("Tạo lại notes từ bản transcript đã lưu · cài đặt phiên bản {recipe}.", { recipe: intent.recipe_version })
          : null;
  return {
    visible: true,
    workflow,
    steps: [
      { kind: "transcription", label: "Transcript", ...transcript },
      { kind: "notes", label: "Notes", ...notes },
    ],
    actions: {
      cancel: workflow.actions.cancel === true,
      retryNotes: !globalHoldReason && workflow.actions.retry_notes === true,
      applySettings: !globalHoldReason && workflow.actions.apply_current_settings === true,
      useSource: canUseSource,
    },
    message: holdReason
      ? holdText(holdReason)
      : interruptedStart
      ? asrEnabled
        ? notesEnabled
          ? t("Bản ghi bị gián đoạn nên chưa tự xử lý. Bấm Chép lời phần đã ghi để bắt đầu chép lời và tạo notes.")
          : t("Bản ghi bị gián đoạn nên chưa tự xử lý. Bấm Chép lời phần đã ghi để bắt đầu chép lời. Notes đang tắt.")
        : t("Bản ghi bị gián đoạn. ASR và Notes đang tắt cho workflow này; xác nhận để giữ phần đã ghi.")
      : overallText(workflow, holdReason),
    source,
    sourceActionLabel: !canUseSource
      ? null
      : interruptedStart
      ? asrEnabled ? t("Chép lời phần đã ghi") : t("Xác nhận phần đã ghi")
      : workflow.current_source?.generation_id === null && workflow.current_source?.source_version === null
        ? t("Xác nhận thông tin cuộc họp")
        : t("Dùng nguồn đang hiển thị"),
    error: state.error,
    notice: state.notice,
    action,
    intended,
    busy: Boolean(action),
  };
}
