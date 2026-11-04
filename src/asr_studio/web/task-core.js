// Pure core of the transcription task of the open meeting (phase 4b of #118): start and cancel, the progress poll,
// the pending request id, the language choice, the processing state and the task error, the runtime profile and the
// diagnostic command. No DOM, fetch, timer, storage, clock, randomness or page visibility: node --test loads this
// file directly. Contract: docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design-v1.md §11.9.
//
// The slice `task` shares the page store with `history`. The task object itself lives in the meeting that
// history-core holds (`meeting.transcription.task`): the actions that carry a task answer (`task/poll-loaded`,
// `task/start-succeeded`, `task/cancel-succeeded`, `task/refresh-loaded`) are reduced by both slices, and history-core
// applies them to its meeting. Because `combine` hands a slice only its own state, the view stamps every action with
// `shown`, the facts of the meeting on screen (see `shownOf` in history.js), the way it stamps the page address.
// Random request ids, the page visibility and the values read from sessionStorage enter through actions.
//
// User-visible text is data: t(text, params) here only keeps the Vietnamese source string, and the view translates it.

import { ACTIVE_TASK_STATES, LANGUAGES, failureOf } from "./history-core.js";
import { ASR_MODEL_TEXT, AUTO_TRANSCRIBE_HANDOFF_PREFIX, errorMessageForCode, isWorkspaceAuthenticationFailure } from "./shell-core.js";

const t = (text, params) => (params ? { text, params } : { text });

export const POLL_VISIBLE_MS = 1000;
export const POLL_HIDDEN_MS = 3000;
export const POLL_RETRY_MS = 3000; // after a failed read, whatever the visibility

export const requestStorageKey = (meetingId) => `asr-studio:transcription-request:${meetingId}`;
export const autoStartStorageKey = (meetingId) => `${AUTO_TRANSCRIBE_HANDOFF_PREFIX}${meetingId}`;

const KEY = {
  start: "task/start",
  pollTimer: "task/poll-timer",
  poll: "task/poll",
  copy: "task/copy",
  copyFallback: "task/copy-fallback",
};
const cancelKey = (taskId) => `task/cancel/${taskId}`;
const refreshKey = (origin) => `task/refresh/${origin}`;

const SAFE_TASK_ID = /^[0-9a-f]{32}$/;

// The error codes whose message and diagnostic command the page shows; the server's own text is the fallback.
const TASK_ERROR_MESSAGES = {
  diarization_decoder_unavailable: t("Bộ giải mã âm thanh local không khả dụng. Kiểm tra dependencies âm thanh của ứng dụng."),
  diarization_model_load_failed: t("Không thể nạp model Community-1 local. Kiểm tra các model assets đã được cấp quyền và tải đầy đủ."),
  diarization_cuda_failed: t("Community-1 không chạy được trên CUDA đã chọn. Kiểm tra CUDA hoặc chọn CPU."),
  diarization_input_decode_failed: t("Không thể giải mã bản ghi. Tải lại file âm thanh hợp lệ rồi thử lại."),
  diarization_failed: t("Nhận dạng người nói thất bại. Kiểm tra worker logs rồi thử lại."),
  publication_failed: t("Không thể lưu transcript hoàn chỉnh. Kiểm tra bộ nhớ lưu trữ rồi thử lại."),
};

// Reasons an automatic start was refused; the manual button stays for a deliberate retry.
const AUTO_START_REASONS = {
  system_busy: t("Máy đang bận vì một tác vụ khác đang chạy."),
  processing_unavailable: t("Slot xử lý của máy chưa sẵn sàng."),
  worker_unavailable: t("Worker chép lời chưa sẵn sàng."),
  model_unavailable: t("Model chép lời local chưa được chuẩn bị."),
  diarization_model_unavailable: t("Model nhận dạng người nói chưa được chuẩn bị."),
};

// Refused by the app before it created a task, whatever the HTTP status: a retry has nothing to verify. A 503 without
// one of these codes may come from something in front of the app, and stays uncertain.
const REFUSED_BEFORE_ACCEPT = new Set([...Object.keys(AUTO_START_REASONS), "model_requires_gpu"]);
const MODEL_REFUSALS = {
  model_unavailable: t("Model đã chọn chưa được chuẩn bị. Tải lại trang để xem cách chuẩn bị, hoặc chọn model khác."),
  model_requires_gpu: t("Model đã chọn chỉ chạy trên GPU NVIDIA. Hãy chọn model khác."),
};

const LANGUAGE_HINTS = new Set(Object.keys(LANGUAGES));

const ASR_MODEL_GUIDANCE = {
  not_prepared: t("Model này chưa được chuẩn bị trên máy. Chạy lệnh sau trong thư mục ứng dụng (chỉ tải model, không gửi bản ghi), rồi tải lại trang."),
  gpu_required: t("Model này chỉ chạy trên GPU NVIDIA, còn worker đang chạy CPU. Hãy chọn model khác."),
  worker_unavailable: t("Worker chép lời chưa sẵn sàng nên chưa kiểm tra được model."),
};

export const initialState = {
  hidden: false, // the page is in the background: the poll slows down
  poll: null, // { meetingId, taskId, phase: waiting | fetching }: the one task being followed
  pending: null, // { meetingId, requestId, languageHint, asrModel }: a start whose answer is not known yet (also kept in sessionStorage)
  language: null, // { meetingId, selection }: the language the user chose for the next run
  asrModel: null, // { meetingId, selection }: the model the user chose for the next run; never a system default
  start: null, // { meetingId, requestId, auto, refreshing }: a start request on the wire (global, as the old flag was)
  cancelling: [], // ids of tasks with a cancel request on the wire
  error: null, // { meetingId, message }: the last start or cancel refusal
  notice: null, // { meetingId, kind: poll-lost | refresh-lost | session-ended }: a line for the status text
  copy: null, // { taskId, how: copied | selected }: the answer to the copy button
  autoDone: null, // the meeting whose automatic start was taken
  rev: 0, // the task half of the page is due a repaint
};

const out = (state, effects = []) => ({ state, effects });
const then = (result, step) => {
  const next = step(result.state);
  return { state: next.state, effects: [...result.effects, ...(next.effects ?? [])] };
};
const repaint = (state) => ({ ...state, rev: state.rev + 1 });
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

export const isActive = (task) => Boolean(task && ACTIVE_TASK_STATES.includes(task.status));
const validLanguage = (value) => (typeof value === "string" && LANGUAGE_HINTS.has(value) ? value : null);
const validModel = (value) => (typeof value === "string" && Object.hasOwn(ASR_MODEL_TEXT, value) ? value : null);

// ---- language ----

function defaultLanguage(shown, task = null) {
  const taskLanguage = validLanguage(task && task.language_hint);
  if (taskLanguage) return taskLanguage;
  return validLanguage(shown && shown.generationLanguage) || validLanguage(shown && shown.language) || "en";
}

const pendingFor = (state, meetingId) => (state.pending && state.pending.meetingId === meetingId ? state.pending : null);

// What the language control shows: a pending request's language is the only safe value to display or retry;
// then the language of the task that is running, the user's choice, the last task's and the meeting's own.
export function displayedLanguage(state, shown) {
  const task = shown.task;
  const pending = pendingFor(state, shown.id);
  const remembered = state.language && state.language.meetingId === shown.id ? validLanguage(state.language.selection) : null;
  return (
    (pending && validLanguage(pending.languageHint))
    || (isActive(task) && validLanguage(task.language_hint))
    || remembered
    || validLanguage(task && task.language_hint)
    || defaultLanguage(shown, task)
  );
}

// The same order for the model, except that the last task's model is not read back: a new page starts on Tự động,
// and a choice lasts while this page stays on the meeting.
export function displayedModel(state, shown) {
  const pending = pendingFor(state, shown.id);
  const remembered = state.asrModel && state.asrModel.meetingId === shown.id ? validModel(state.asrModel.selection) : null;
  return (
    (pending && validModel(pending.asrModel))
    || (isActive(shown.task) && validModel(shown.task.asr_model))
    || remembered
    || "auto"
  );
}

// A worker that is down keeps its own refusal; a model known to be unusable cannot start. An uncertain start may
// still be retried: the server answers it from the run it already accepted.
function modelBlocked(state, shown) {
  if (pendingFor(state, shown.id)) return false;
  const choice = (shown.asrModels || []).find((entry) => entry.id === displayedModel(state, shown));
  return Boolean(choice && ["not_prepared", "gpu_required"].includes(choice.reason));
}

// ---- the pending request id ----

// The stored record: JSON {requestId, languageHint, asrModel}, or the plain request id that older pages wrote.
function parseStored(raw, fallbackLanguage) {
  if (!raw) return null;
  let requestId = raw;
  let languageHint = fallbackLanguage;
  let asrModel = "auto";
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed === "string") {
      requestId = parsed;
    } else if (parsed && typeof parsed === "object") {
      requestId = parsed.requestId;
      languageHint = validLanguage(parsed.languageHint) || fallbackLanguage;
      asrModel = validModel(parsed.asrModel) || "auto";
    }
  } catch {
    // an older page stored the request id as plain text
  }
  if (typeof requestId !== "string" || !requestId) return null;
  return { requestId, languageHint: validLanguage(languageHint) || "en", asrModel };
}

const rememberEffect = (pending) => ({
  type: "storage",
  key: requestStorageKey(pending.meetingId),
  area: "session",
  op: "write",
  value: JSON.stringify({ requestId: pending.requestId, languageHint: pending.languageHint, asrModel: pending.asrModel }),
});

function remember(state, pending) {
  return out({ ...state, pending }, [rememberEffect(pending)]);
}

function forget(state, meetingId) {
  const pending = pendingFor(state, meetingId) ? null : state.pending;
  return out({ ...state, pending }, [{ type: "storage", key: requestStorageKey(meetingId), area: "session", op: "remove" }]);
}

// A terminal task in a freshly loaded meeting can be the previous completed run while a newer start is still
// uncertain: the stored request is retired only after the same run is seen (or when none is pending).
function rotate(state, meetingId, task) {
  if (!task || isActive(task)) return out(state);
  const pending = pendingFor(state, meetingId);
  return !pending || pending.requestId === task.request_id ? forget(state, meetingId) : out(state);
}

const clearedError = (state, meetingId) => (state.error && state.error.meetingId === meetingId ? null : state.error);

// ---- polling ----

const pollDelay = (state) => (state.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);

function schedulePoll(state, meetingId, taskId, delay) {
  const effect = { type: "schedule", key: KEY.pollTimer, delay, action: { type: "task/poll-due", meetingId, taskId } };
  return out({ ...state, poll: { meetingId, taskId, phase: "waiting" } }, [effect]);
}

function stopPolling(state) {
  if (!state.poll) return out(state);
  return out({ ...state, poll: null }, [{ type: "cancel", key: KEY.pollTimer }, { type: "cancel", key: KEY.poll }]);
}

function sessionEnded(state, meetingId) {
  const stopped = stopPolling({
    ...state,
    start: null,
    error: null,
    notice: { meetingId, kind: "session-ended" },
    copy: null,
  });
  return out(
    repaint(stopped.state),
    [...stopped.effects, { type: "navigate", key: "task/session-ended", url: "/access" }],
  );
}

// Follow a running task; a task that has ended is not polled. One chain per task: a second call for the task
// already followed changes nothing.
function startPolling(state, meetingId, task) {
  if (!isActive(task)) return stopPolling(state);
  if (state.poll && state.poll.taskId === task.id) return out(state);
  return then(stopPolling(state), (s) => schedulePoll(s, meetingId, task.id, pollDelay(s)));
}

const refreshFetch = (meetingId, origin) => ({
  type: "fetch",
  key: refreshKey(origin),
  url: `/api/meetings/${encodeURIComponent(meetingId)}`,
  init: { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" },
  ok: "task/refresh-loaded",
  error: "task/refresh-failed",
  meta: { meetingId, origin },
});

function pollDue(state, action) {
  const { poll } = state;
  const { meetingId, taskId } = action;
  if (!poll || poll.phase !== "waiting" || poll.meetingId !== meetingId || poll.taskId !== taskId) return out(state);
  if (!action.shown || action.shown.id !== meetingId) return out({ ...state, poll: null });
  const fetchEffect = {
    type: "fetch",
    key: KEY.poll,
    url: `/api/tasks/${encodeURIComponent(taskId)}`,
    init: { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" },
    ok: "task/poll-loaded",
    error: "task/poll-failed",
    meta: { meetingId, taskId },
  };
  return out({ ...state, poll: { ...poll, phase: "fetching" } }, [fetchEffect]);
}

const followed = (state, meta) => Boolean(state.poll && state.poll.meetingId === meta.meetingId && state.poll.taskId === meta.taskId && state.poll.phase === "fetching");

function pollFailed(state, meta, error) {
  if (!followed(state, meta)) return out(state);
  if (isWorkspaceAuthenticationFailure(error?.status, error?.body)) return sessionEnded(state, meta.meetingId);
  const notice = { meetingId: meta.meetingId, kind: "poll-lost" };
  return then(schedulePoll({ ...state, notice }, meta.meetingId, meta.taskId, POLL_RETRY_MS), (s) => out(repaint(s)));
}

function pollLoaded(state, action) {
  const { meta } = action;
  if (!followed(state, meta)) return out(state);
  if (!action.shown || action.shown.id !== meta.meetingId) return out({ ...state, poll: null });
  const task = action.response.body;
  if (!isObject(task)) return pollFailed(state, meta);
  const seen = { ...state, notice: null, copy: null };
  if (isActive(task)) return then(schedulePoll(seen, meta.meetingId, task.id, pollDelay(seen)), (s) => out(repaint(s)));
  let result = then(rotate(seen, meta.meetingId, task), stopPolling);
  if (task.status === "succeeded") result = then(result, (s) => out(s, [refreshFetch(meta.meetingId, "poll")]));
  return then(result, (s) => out(repaint(s)));
}

// ---- start ----

function startRequest(state, shown, requestId, auto) {
  if (!shown || state.start || shown.blocked || (!auto && modelBlocked(state, shown))) return out(state);
  let result = out(state);
  let pending = pendingFor(state, shown.id);
  if (!pending) {
    pending = { meetingId: shown.id, requestId, languageHint: displayedLanguage(state, shown), asrModel: displayedModel(state, shown) };
    result = remember(state, pending);
  }
  const startEffect = {
    type: "fetch",
    key: KEY.start,
    url: "/api/tasks",
    init: {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ meeting_id: shown.id, request_id: pending.requestId, language_hint: pending.languageHint, asr_model: pending.asrModel }),
    },
    ok: "task/start-succeeded",
    error: "task/start-failed",
    meta: { meetingId: shown.id, requestId: pending.requestId },
  };
  const start = { meetingId: shown.id, requestId: pending.requestId, auto, refreshing: false };
  return then(result, (s) => out(repaint({ ...s, start, error: clearedError(s, shown.id), notice: null, copy: null }), [startEffect]));
}

function startFailed(state, error, auto) {
  const { meetingId } = state.start;
  if (isWorkspaceAuthenticationFailure(error?.status, error?.body)) return sessionEnded(state, meetingId);
  const failure = failureOf(error, t("Không thể xác nhận yêu cầu."));
  const definitive = Boolean(failure.api && Number.isInteger(failure.status) && ((failure.status >= 400 && failure.status < 500) || REFUSED_BEFORE_ACCEPT.has(failure.code)));
  const guidance = definitive
    ? t("Bấm lại để gửi một yêu cầu mới.")
    : t("Bấm lại để xác minh bằng cùng request ID, ngôn ngữ gợi ý và model.");
  let message = [failure.message, " ", guidance];
  const selectedModel = state.asrModel?.meetingId === meetingId ? state.asrModel.selection : "auto";
  if (Object.hasOwn(MODEL_REFUSALS, failure.code) && selectedModel !== "auto") {
    // A named model has its own preparation/GPU guidance, including for an automatic handoff.
    message = MODEL_REFUSALS[failure.code];
  } else if (auto && Object.hasOwn(AUTO_START_REASONS, failure.code)) {
    message = t("{reason} Cuộc họp đã được lưu nhưng chưa chép lời. Ứng dụng không tự thử lại; bấm “Bắt đầu chép lời” khi máy sẵn sàng.", { reason: AUTO_START_REASONS[failure.code] });
  } else if (Object.hasOwn(MODEL_REFUSALS, failure.code)) {
    // The shared text for this code names a notes provider, which has no place here.
    message = MODEL_REFUSALS[failure.code];
  }
  const base = definitive ? forget(state, meetingId) : out(state);
  // A request restored after a reload is the only record of its model: the selector must keep showing what was refused.
  const refused = definitive && pendingFor(state, meetingId);
  return then(base, (s) => out(repaint({
    ...s,
    start: null,
    error: { meetingId, message },
    asrModel: refused ? { meetingId, selection: refused.asrModel } : s.asrModel,
  })));
}

function startSucceeded(state, action) {
  const { meta } = action;
  if (!state.start || state.start.requestId !== meta.requestId) return out(state);
  const task = action.response.body;
  if (!isObject(task)) return startFailed(state, {}, state.start.auto);
  const { meetingId } = state.start;
  return then(forget(state, meetingId), (forgotten) => {
    const here = action.shown && action.shown.id === meetingId;
    const base = { ...forgotten, error: clearedError(forgotten, meetingId) };
    if (!here) return out(repaint({ ...base, start: null }));
    const seen = { ...base, notice: null, copy: null };
    return then(startPolling(seen, meetingId, task), (s) => {
      if (task.status !== "succeeded") return out(repaint({ ...s, start: null }));
      return out(repaint({ ...s, start: { ...s.start, refreshing: true } }), [refreshFetch(meetingId, "start")]);
    });
  });
}

// ---- cancel ----

function cancelClicked(state, action) {
  const task = action.shown && action.shown.task;
  if (!isActive(task) || task.status === "stopping" || state.cancelling.includes(task.id)) return out(state);
  const effect = {
    type: "fetch",
    key: cancelKey(task.id),
    url: `/api/tasks/${encodeURIComponent(task.id)}/cancel`,
    init: {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({}),
    },
    ok: "task/cancel-succeeded",
    error: "task/cancel-failed",
    meta: { meetingId: action.shown.id, taskId: task.id },
  };
  return out(repaint({ ...state, cancelling: [...state.cancelling, task.id] }), [effect]);
}

function cancelFailed(state, meta, error) {
  const cancelling = state.cancelling.filter((id) => id !== meta.taskId);
  if (isWorkspaceAuthenticationFailure(error?.status, error?.body)) return sessionEnded({ ...state, cancelling }, meta.meetingId);
  const failure = failureOf(error, t("Không thể gửi yêu cầu hủy. Trạng thái tác vụ chưa được thay đổi trên giao diện."));
  return out(repaint({ ...state, cancelling, error: { meetingId: meta.meetingId, message: failure.message } }));
}

function cancelSucceeded(state, action) {
  const { meta } = action;
  if (!state.cancelling.includes(meta.taskId)) return out(state);
  const updated = action.response.body;
  if (!isObject(updated)) return cancelFailed(state, meta, {});
  const cancelling = state.cancelling.filter((id) => id !== meta.taskId);
  const base = { ...state, cancelling, error: clearedError(state, meta.meetingId) };
  if (!action.shown || action.shown.id !== meta.meetingId) return out(repaint(base));
  return then(startPolling({ ...base, copy: null }, meta.meetingId, updated), (s) => out(repaint(s)));
}

// ---- reading the meeting again after a run succeeded ----

function refreshLoaded(state, action) {
  const { meta } = action;
  const payload = action.response.body;
  const meeting = isObject(payload) && isObject(payload.meeting) ? payload.meeting : payload;
  const own = meta.origin === "start" ? { ...state, start: null } : state;
  const usable = action.shown && action.shown.id === meta.meetingId && isObject(meeting) && meeting.transcription;
  if (!usable) return out(repaint(own));
  return then(rotate(own, meta.meetingId, meeting.transcription.task), (s) => out(repaint({ ...s, notice: null })));
}

function refreshFailedLoading(state, action) {
  const { meta } = action;
  if (meta.origin === "start") return state.start ? startFailed(state, action.error, state.start.auto) : out(state);
  if (isWorkspaceAuthenticationFailure(action.error?.status, action.error?.body)) return sessionEnded(state, meta.meetingId);
  return out(repaint({ ...state, notice: { meetingId: meta.meetingId, kind: "refresh-lost" } }));
}

// ---- the diagnostic command ----

function diagnosticCommand(task) {
  return task && SAFE_TASK_ID.test(task.id || "") ? `docker compose exec -T app python -m asr_studio.cli task diagnose ${task.id}` : "";
}

function copyClicked(state, action) {
  const command = selectTask(state, action.shown).error.diagnostic?.command;
  if (!command) return out(state);
  return out(state, [{ type: "call", key: KEY.copy, name: "clipboard.write", args: [command], ok: "task/copy-succeeded", error: "task/copy-failed", meta: { taskId: action.shown.task.id } }]);
}

const copied = (state, taskId, how) => out(repaint({ ...state, copy: { taskId, how } }));

// ---- reducer ----

export function reduce(state, action) {
  switch (action.type) {
    case "task/visibility-changed": {
      const next = { ...state, hidden: Boolean(action.hidden) };
      const task = action.shown && action.shown.task;
      // The cadence follows the page: a poll that is waiting restarts at the pace of the new visibility.
      if (next.poll && next.poll.phase === "waiting" && isActive(task)) return schedulePoll(next, next.poll.meetingId, next.poll.taskId, pollDelay(next));
      return out(next);
    }

    case "task/meeting-ready": {
      const { shown } = action;
      if (!shown) return out(state);
      const task = shown.task;
      const language = state.language && state.language.meetingId === shown.id ? state.language : null;
      const asrModel = state.asrModel && state.asrModel.meetingId === shown.id ? state.asrModel : null;
      let result = out({ ...state, copy: null, notice: null, language, asrModel });
      if (!pendingFor(state, shown.id)) {
        const fallback = task && !isActive(task) ? validLanguage(task.language_hint) || "en" : defaultLanguage(shown, task);
        const stored = parseStored(action.stored, fallback);
        if (stored) result = remember(result.state, { meetingId: shown.id, ...stored });
      }
      result = then(result, (s) => rotate(s, shown.id, task));
      result = then(result, (s) => startPolling(s, shown.id, task));
      if (action.autoStart) {
        const handoff = { type: "storage", key: autoStartStorageKey(shown.id), area: "session", op: "remove" };
        const taken = then(result, (s) => out({ ...s, autoDone: shown.id }, [handoff]));
        const due = state.autoDone !== shown.id && !task && !shown.generation;
        result = due
          ? then(taken, (s) => startRequest({ ...s, asrModel: { meetingId: shown.id, selection: validModel(action.autoStart) || "auto" } }, shown, action.requestId, true))
          : taken;
      }
      // The language control repaints when a pending request came back, and the status line when a notice or copy line went away.
      const changed = (s) => s.pending !== state.pending || s.notice !== state.notice || s.copy !== state.copy;
      return then(result, (s) => out(changed(s) ? repaint(s) : s));
    }

    case "task/meeting-left":
      return stopPolling({ ...state, notice: null, copy: null });

    case "task/language-chosen": {
      const language = validLanguage(action.language);
      if (!action.shown || !language) return out(state);
      return out({ ...state, language: { meetingId: action.shown.id, selection: language } });
    }

    case "task/model-chosen": {
      const asrModel = validModel(action.model);
      if (!action.shown || !asrModel) return out(state);
      // The start button and the guidance follow the choice.
      return out(repaint({ ...state, asrModel: { meetingId: action.shown.id, selection: asrModel } }));
    }

    case "task/start-clicked":
      return startRequest(state, action.shown, action.requestId, false);
    case "task/start-succeeded":
      return startSucceeded(state, action);
    case "task/start-failed":
      return state.start && state.start.requestId === action.meta.requestId ? startFailed(state, action.error, state.start.auto) : out(state);

    case "task/cancel-clicked":
      return cancelClicked(state, action);
    case "task/cancel-succeeded":
      return cancelSucceeded(state, action);
    case "task/cancel-failed":
      return state.cancelling.includes(action.meta.taskId) ? cancelFailed(state, action.meta, action.error) : out(state);

    case "task/poll-due":
      return pollDue(state, action);
    case "task/poll-loaded":
      return pollLoaded(state, action);
    case "task/poll-failed":
      return pollFailed(state, action.meta, action.error);

    case "task/refresh-loaded":
      return refreshLoaded(state, action);
    case "task/refresh-failed":
      return refreshFailedLoading(state, action);

    case "task/copy-clicked":
      return copyClicked(state, action);
    case "task/copy-succeeded":
      return copied(state, action.meta.taskId, "copied");
    case "task/copy-failed":
      return out(state, [{ type: "call", key: KEY.copyFallback, name: "clipboard.select", ok: "task/copy-selected", meta: action.meta }]);
    case "task/copy-selected":
      return copied(state, action.meta.taskId, action.value ? "copied" : "selected");

    default:
      return out(state);
  }
}

// ---- selectors ----

export const selectRequestBusy = (state) => state.start !== null;

// The bar is the share of the recording already transcribed; without a usable duration it is indeterminate.
function progressOf(task) {
  const duration = Number(task.audio_duration_seconds);
  // Transcript timing describes ASR only. Diarization, alignment and publication do not
  // have a trustworthy share-of-audio denominator, so they stay visibly indeterminate.
  const known = task.stage === "transcribing" && Number.isFinite(duration) && duration > 0;
  const fraction = known ? Math.min(1, Math.max(0, Number(task.transcript_end_seconds) || 0) / duration) : 0;
  // Rounding must not announce 100% before the last second is transcribed.
  const percent = fraction >= 1 ? 100 : Math.min(99, Math.round(fraction * 100));
  return { known, fraction, percent: known ? percent : null };
}

export function selectRuntime(runtime) {
  const device = runtime && runtime.device;
  let backend = t("Worker chưa sẵn sàng");
  if (device === "cuda") backend = `GPU · CUDA · ${runtime.compute_type}`;
  else if (device === "cpu") backend = `CPU fallback · ${runtime.compute_type}`;
  return { device: device || "unavailable", backend, model: runtime && runtime.model_name ? runtime.model_name : t("Chưa xác định") };
}

function selectModel(state, shown, disabled) {
  const choices = (shown.asrModels || []).filter((choice) => validModel(choice.id));
  if (!choices.length) return null;
  const value = displayedModel(state, shown);
  const choice = choices.find((entry) => entry.id === value);
  const note = value !== "auto"
    ? ASR_MODEL_TEXT[value].note
    : choice && choice.model_name
      ? t("Theo phần cứng của máy: {model}.", { model: choice.model_name })
      : t("Theo phần cứng của máy.");
  const reason = choice ? choice.reason : null;
  return {
    value,
    disabled,
    options: choices.map((entry) => ({ id: entry.id, label: ASR_MODEL_TEXT[entry.id].label, ready: Boolean(entry.ready) })),
    note,
    blocked: modelBlocked(state, shown),
    guidance: reason && !disabled ? ASR_MODEL_GUIDANCE[reason] ?? null : null,
    command: choice && !disabled ? choice.prepare_command || "" : "",
  };
}

// What the task half of the page shows for the meeting on screen. `shown` is the same facts the view stamps on actions.
export function selectTask(state, shown) {
  const task = shown ? shown.task : null;
  const active = isActive(task);
  const refusal = state.error && shown && state.error.meetingId === shown.id ? state.error.message : null;
  const failed = Boolean(task && ["failed", "interrupted"].includes(task.status));
  const code = task && typeof task.error_code === "string" ? task.error_code : "";
  const knownCode = Object.hasOwn(TASK_ERROR_MESSAGES, code) ? TASK_ERROR_MESSAGES[code] : errorMessageForCode(code);
  let message = "";
  if (refusal) message = refusal;
  else if (failed) message = knownCode || t("Tác vụ chưa hoàn tất.");
  const command = diagnosticCommand(task);
  const diagnostic = !refusal && Object.hasOwn(TASK_ERROR_MESSAGES, code) && command ? { code, id: task.id, command } : null;
  const pending = shown ? pendingFor(state, shown.id) : null;
  const notice = state.notice && shown && state.notice.meetingId === shown.id ? state.notice.kind : null;
  const how = state.copy && task && state.copy.taskId === task.id ? state.copy.how : null;
  return {
    active,
    workerChecking: active && task.worker_state === "checking-worker",
    requestBusy: state.start !== null,
    cancelDisabled: !task || task.status === "stopping" || state.cancelling.includes(task.id),
    stopping: Boolean(task && task.status === "stopping"),
    progress: active ? progressOf(task) : null,
    language: shown ? { value: displayedLanguage(state, shown), disabled: active || Boolean(pending) } : null,
    model: shown ? selectModel(state, shown, active || Boolean(pending)) : null,
    error: { visible: failed || Boolean(refusal), message, diagnostic },
    notice,
    copy: how,
  };
}
