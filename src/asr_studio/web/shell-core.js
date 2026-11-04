// Pure core of the create-meeting dialog: state, reducer, selectors. No DOM, fetch, timer, storage
// or clock: node --test loads this file directly. Contract: docs/adr/0010-frontend-reducer-core.md;
// state table: docs/frontend-design-v1.md §11.5.
//
// User-visible text is data: t(text, params) here only keeps the Vietnamese source string, and the view
// translates it with the real t(). The name matches the call sites in the other scripts so the i18n
// test finds every string. A param is a primitive, a t() result, a bytes()/decimal() marker, or an array of those.

export const LAST_LANGUAGE_KEY = "asr-studio:last-language-hint";
export const AUTO_TRANSCRIBE_KEY = "asr-studio:auto-transcribe";
// history.js removes this one-shot key before it sends the transcription request.
export const AUTO_TRANSCRIBE_HANDOFF_PREFIX = "asr-studio:auto-transcribe:";

const t = (text, params) => (params ? { text, params } : { text });
export const ASR_MODEL_TEXT = {
  auto: { label: t("Tự động") },
  "whisper-large-v3": { label: "Whisper large-v3", note: t("Đa ngôn ngữ, tự phát hiện ngôn ngữ. Đôi khi lặp lại câu.") },
  "pho-whisper-large": { label: "PhoWhisper-large", note: t("Tối ưu cho tiếng Việt. Không dành cho English hoặc nội dung trộn nhiều thuật ngữ kỹ thuật.") },
  "qwen3-asr-1.7b": { label: "Qwen3-ASR-1.7B", note: t("Đa ngôn ngữ, chỉ chạy trên GPU. Không có điểm tin cậy: “cần kiểm tra” chỉ bắt đoạn lặp và vấn đề người nói, nên đừng so số này với model khác.") },
};
export const bytes = (value) => ({ fmt: "bytes", value });
export const decimal = (value) => ({ fmt: "decimal", value });

export const STATE_LABELS = {
  idle: t("Chưa chọn file"),
  selected: t("Đã chọn file"),
  extracting: t("Đang tách audio"),
  uploading: t("Đang tải lên"),
  checking: t("Đang kiểm tra"),
  ready: t("Sẵn sàng tạo"),
  creating: t("Đang tạo"),
  saved: t("Đã tạo"),
  error: t("Có lỗi"),
  aborted: t("Đã hủy"),
};

export function formatBytes(value, locale) {
  if (value < 1024) return `${value} byte`;
  const units = ["KiB", "MiB", "GiB"];
  let amount = value;
  let unit = "byte";
  for (const nextUnit of units) {
    amount /= 1024;
    unit = nextUnit;
    if (amount < 1024 || nextUnit === "GiB") break;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(amount)} ${unit}`;
}

function formatDuration(seconds) {
  if (seconds >= 3600 && seconds % 3600 === 0) return t("{n} giờ", { n: seconds / 3600 });
  if (seconds >= 60 && seconds % 60 === 0) return t("{n} phút", { n: seconds / 60 });
  if (seconds >= 60) return t("{n} phút", { n: decimal(seconds / 60) });
  return t("{n} giây", { n: seconds });
}

const SERVER_ERROR_MESSAGES = {
  invalid_media: t("File không đạt yêu cầu. Hãy kiểm tra định dạng, thời lượng và nội dung file."),
  duration_mismatch: t("File bị cắt ngắn hoặc thời lượng không hợp lệ. Hãy chọn file khác."),
  invalid_meeting_date: t("Ngày họp chưa đầy đủ hoặc không hợp lệ. Nhập đủ ngày, tháng, năm hoặc bấm “Xóa ngày” để bỏ ngày."),
  meeting_not_found: t("Không có cuộc họp này."),
  stale_version: t("Dữ liệu đã thay đổi. Hãy tải lại rồi thử lại."),
  meeting_task_active: t("Cuộc họp còn tác vụ đang chạy. Hãy đợi tác vụ hoàn tất rồi thử lại."),
  system_busy: t("Máy đang bận vì một tác vụ khác đang chạy."),
  processing_unavailable: t("Slot xử lý của máy chưa sẵn sàng."),
  worker_unavailable: t("Worker chép lời chưa sẵn sàng."),
  transcription_failed: t("Chép lời local thất bại. Kiểm tra log của worker rồi thử lại."),
  runner_protocol_error: t("Runner chép lời local không trả về kết quả hoàn chỉnh."),
  recording_unavailable: t("Worker không còn truy cập được bản ghi. Hãy tải lại cuộc họp rồi thử lại."),
  publication_failed: t("Không thể lưu transcript hoàn chỉnh. Kiểm tra bộ nhớ lưu trữ rồi thử lại."),
  notes_publication_failed: t("Không thể lưu notes hoàn chỉnh. Kiểm tra bộ nhớ lưu trữ rồi thử lại."),
  notes_runner_protocol_error: t("Worker notes không trả về kết quả hoàn chỉnh."),
  notes_timeout: t("Tạo notes mất quá nhiều thời gian và đã được dừng. Hãy thử lại."),
  notes_failed: t("Tạo notes thất bại. Kiểm tra log của worker rồi thử lại."),
  notes_source_locked: t("Nguồn đang được dùng để tạo notes. Chỉnh sửa transcript, người nói và ngày họp tạm thời bị khóa."),
  provider_busy: (provider) => provider
    ? t("{name} đang bận với một thao tác kết nối hoặc đăng nhập. Hãy hoàn tất hoặc hủy đăng nhập rồi thử lại; hệ thống chưa đổi gì.", { name: providerName(provider) })
    : t("Một thao tác của nhà cung cấp đang chạy. Hãy đợi một chút rồi thử lại."),
  provider_disconnected: (provider) => t("{name} chưa được kết nối. Hãy kết nối trước khi tạo notes.", { name: providerName(provider) }),
  provider_in_use: (provider) => t("{name} đang được một tác vụ notes khác sử dụng. Hãy đợi tác vụ hoàn tất trước khi đổi model, kiểm tra hoặc ngắt kết nối.", { name: providerName(provider) }),
  reconnect_required: (provider) => t("{provider} cần được kết nối lại trước khi tạo notes.", { provider: providerName(provider) }),
  provider_unavailable: (provider) => t("{name} tạm thời không khả dụng. Hãy kiểm tra kết nối Internet rồi thử lại.", { name: providerName(provider) }),
  provider_quota: (provider) => t("{provider} đã hết hạn mức sử dụng. Hãy kiểm tra mức sử dụng gói.", { provider: providerName(provider) }),
  provider_output_too_large: (provider) => t("Phản hồi của {provider} vượt quá kích thước đầu ra cho phép.", { provider: providerName(provider) }),
  provider_stream_too_large: (provider) => t("Phản hồi của {provider} vượt quá kích thước truyền cho phép.", { provider: providerName(provider) }),
  provider_stream_line_too_large: (provider) => t("Một dòng phản hồi của {provider} vượt quá kích thước cho phép.", { provider: providerName(provider) }),
  provider_invalid_stream: (provider) => t("{provider} trả về phản hồi không đúng định dạng.", { provider: providerName(provider) }),
  provider_invalid_response: (provider) => t("{provider} trả về phản hồi không hợp lệ.", { provider: providerName(provider) }),
  provider_timeout: (provider) => t("{provider} không phản hồi trong thời gian cho phép.", { provider: providerName(provider) }),
  provider_request_too_large: (provider) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {provider}.", { provider: providerName(provider) }),
  notes_request_too_large: (provider) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {provider}.", { provider: providerName(provider) }),
  notes_context_budget_exceeded: (provider) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {provider}.", { provider: providerName(provider) }),
  notes_token_budget_exceeded: (provider) => t("{provider} hết hạn mức token trước khi hoàn tất notes. Không có notes mới được lưu.", { provider: providerName(provider) }),
  notes_content_filtered: (provider) => t("{provider} dừng tạo notes vì bộ lọc nội dung. Không có notes mới được lưu.", { provider: providerName(provider) }),
  notes_incomplete: (provider) => t("{provider} chưa hoàn tất câu trả lời notes. Không có notes mới được lưu.", { provider: providerName(provider) }),
  notes_invalid_output: (provider) => t("{provider} trả về notes không hợp lệ. Không có notes mới được lưu.", { provider: providerName(provider) }),
  notes_synthesis_limit_exceeded: (provider) => t("Transcript vượt quá giới hạn tổng hợp notes của {provider}.", { provider: providerName(provider) }),
  subscription_sharing_usage_limit_exceeded: t("Gói ChatGPT đã chạm giới hạn sử dụng. Hãy kiểm tra Cài đặt → Mức sử dụng."),
  subscription_sharing_user_not_eligible: t("Gói ChatGPT hiện không dùng được cho tài khoản, workspace hoặc yêu cầu này."),
  subscription_sharing_route_not_supported: t("Gói ChatGPT hiện không dùng được cho tài khoản, workspace hoặc yêu cầu này."),
  subscription_sharing_unsupported_capability: t("ChatGPT không hỗ trợ yêu cầu này với model đã chọn."),
  chatpass_v2_scope_not_authorized: t("Tài khoản chưa cấp quyền dùng gói ChatGPT. Hãy kết nối lại và cấp quyền cần thiết."),
  chatpass_v2_invalid_authorization_context: t("Cấu hình cấp quyền ChatGPT không hợp lệ. Hãy kiểm tra cấu hình kết nối trước khi đăng nhập lại."),
  scope_not_authorized: t("Tài khoản chưa cấp quyền dùng gói ChatGPT. Hãy kết nối lại và cấp quyền cần thiết."),
  model_unavailable: (provider) => t("Model đã chọn không khả dụng với {provider}. Hãy chọn model khác.", { provider: providerName(provider) }),
  model_not_available: (provider) => t("Model đã chọn không khả dụng với {provider}. Hãy chọn model khác.", { provider: providerName(provider) }),
  invalid_model: (provider) => t("Model đã chọn không khả dụng với {provider}. Hãy chọn model khác.", { provider: providerName(provider) }),
};

function providerName(provider) {
  return provider === "antigravity" ? "Google Antigravity" : provider === "chatgpt" ? "ChatGPT" : t("nhà cung cấp");
}

function detailOf(payload) {
  const detail = payload && (payload.detail ?? payload.error);
  return detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
}

export function responseCode(status, payload) {
  const detail = detailOf(payload);
  return typeof detail?.code === "string" && detail.code ? detail.code : `HTTP_${status}`;
}

// The workspace middleware has one unambiguous session-expiry response. Provider APIs may also
// answer 401, but their structured detail belongs to the provider and must not redirect the page.
export function isWorkspaceAuthenticationFailure(status, payload) {
  return status === 401 && payload && typeof payload === "object" && !Array.isArray(payload)
    && payload.detail === "Authentication required";
}

export function errorMessageForCode(code, status, provider, payload) {
  if (code === "duration_limit") {
    const maxDuration = Number(detailOf(payload)?.max_duration_seconds);
    return Number.isSafeInteger(maxDuration) && maxDuration > 0
      ? t("File vượt giới hạn thời lượng {duration}. Hãy chọn bản ghi ngắn hơn.", { duration: formatDuration(maxDuration) })
      : t("File vượt quá thời lượng tối đa được công bố.");
  }
  if (typeof code === "string" && Object.hasOwn(SERVER_ERROR_MESSAGES, code)) {
    const message = SERVER_ERROR_MESSAGES[code];
    return typeof message === "function" ? message(provider) : message;
  }
  if (code === "provider_http_error" && [400, 422].includes(status)) {
    return t("{provider} đã từ chối yêu cầu. Hãy kiểm tra model đã chọn hoặc quyền truy cập.", { provider: providerName(provider) });
  }
  if (status === undefined || status === null || status <= 0) return null;
  if (status === 401) return t("Phiên truy cập không còn hợp lệ. Hãy mở lại link trong terminal.");
  if (status === 403) return t("Yêu cầu bị từ chối. Hãy tải lại workspace rồi thử lại.");
  if (status === 404) return t("Không tìm thấy dữ liệu yêu cầu.");
  if (status === 409) return t("Dữ liệu đã thay đổi hoặc đang được sử dụng. Hãy tải lại rồi thử lại.");
  if (status === 413) return t("File vượt quá giới hạn dung lượng được công bố.");
  if (status === 415) return t("Định dạng file này chưa được hỗ trợ. Hãy chọn định dạng trong danh sách.");
  if (status === 422) return t("File không đạt yêu cầu. Hãy kiểm tra định dạng, thời lượng và nội dung file.");
  if (status >= 500) return t("Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.");
  return t("Máy chủ chưa thể hoàn tất yêu cầu. Hãy kiểm tra thông tin rồi thử lại.");
}

// Server detail text is never UI copy: the stable code selects a localized message, and an
// unknown code falls back to a localized status message rather than exposing raw provider text.
export function responseMessage(status, payload, provider) {
  return errorMessageForCode(responseCode(status, payload), status, provider, payload);
}

function policyField(policy, names) {
  for (const name of names) {
    if (policy[name] !== undefined && policy[name] !== null) return policy[name];
  }
  return undefined;
}

// -> { policy } or { error: t() }
function normalizePolicy(payload) {
  const source = payload && payload.policy && typeof payload.policy === "object" ? payload.policy : payload;
  if (!source || typeof source !== "object") return { error: t("Chính sách file không đúng định dạng.") };
  const rawExtensions = policyField(source, ["supported_extensions", "extensions", "allowed_extensions"]);
  const maxBytes = Number(policyField(source, ["max_bytes", "max_size_bytes", "max_file_size_bytes"]));
  const maxDurationSeconds = Number(policyField(source, ["max_duration_seconds", "max_duration_s"]));
  const rawChunkBytes = policyField(source, ["chunk_bytes", "chunk_size_bytes"]);
  const chunkBytes = rawChunkBytes === undefined ? DEFAULT_UPLOAD_CHUNK_BYTES : Number(rawChunkBytes);
  if (
    !Array.isArray(rawExtensions)
    || !Number.isSafeInteger(maxBytes)
    || maxBytes <= 0
    || !Number.isSafeInteger(maxDurationSeconds)
    || maxDurationSeconds <= 0
    || !Number.isSafeInteger(chunkBytes)
    || chunkBytes <= 0
  ) {
    return { error: t("Chính sách chưa công bố đủ định dạng, dung lượng và thời lượng tối đa.") };
  }
  const extensions = rawExtensions
    .map((extension) => String(extension).trim().toLowerCase())
    .map((extension) => (extension.startsWith(".") ? extension : `.${extension}`))
    .filter((extension) => /^\.[a-z0-9][a-z0-9.+_-]*$/.test(extension));
  if (extensions.length === 0) return { error: t("Chính sách chưa công bố định dạng file được hỗ trợ.") };
  return {
    policy: {
      extensions: [...new Set(extensions)],
      maxBytes,
      maxDurationSeconds,
      chunkBytes: Math.min(chunkBytes, DEFAULT_UPLOAD_CHUNK_BYTES),
    },
  };
}

function normalizeAsrModels(payload) {
  if (!payload || !Array.isArray(payload.asr_models)) return null;
  return payload.asr_models.filter((choice) => (
    choice
    && typeof choice === "object"
    && Object.hasOwn(ASR_MODEL_TEXT, choice.id)
  ));
}

function fileExtension(filename) {
  const finalDot = filename.lastIndexOf(".");
  return finalDot >= 0 ? filename.slice(finalDot).toLowerCase() : "";
}

function formValuesAreValid(form) {
  const title = form.title.trim();
  const dateIsValid = validMeetingDate(form.date);
  return Boolean(title && Array.from(title).length <= 200 && ["en", "vi"].includes(form.language) && dateIsValid);
}

function validMeetingDate(value) {
  if (!value) return true;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1000 || year > 9999 || month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= days[month - 1];
}

const NO_PROGRESS = { visible: false, indeterminate: false, percent: 0, text: "0%" };
const BUSY_PHASES = ["extracting", "uploading", "checking", "creating"];
const CANCELLABLE_PHASES = ["extracting", "uploading", "checking"];
const DEFAULT_UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024;
const MAX_CHUNK_RETRIES = 6;
const RETRY_BACKOFF_BASE_MS = 1_000;
const RETRY_BACKOFF_CAP_MS = 32_000;
// 1+2+4+8+16+32 seconds is 63 seconds, beyond the server's 60-second
// _UPLOAD_IDLE_TIMEOUT_SECONDS, so a half-open writer can expire within this bounded window.
function retryDelayMs(retryNumber) {
  return Math.min(RETRY_BACKOFF_CAP_MS, RETRY_BACKOFF_BASE_MS * 2 ** (retryNumber - 1));
}
// Video containers whose audio track the browser copies out before the upload.
const VIDEO_EXTENSIONS = [".mp4", ".mov", ".webm"];

export const initialState = {
  open: false,
  phase: "idle",
  workflow: t("Chưa chọn file."),
  policy: null,
  policyFailed: false,
  policyRequest: 0, // id of the latest policy fetch; other replies are stale
  asrModels: null,
  asrModelsRequest: 0, // id of the latest model catalog fetch; other replies are stale
  file: null,
  fileError: null,
  fileRefused: false,
  form: { title: "", date: "", language: "", autoTranscribe: true, asrModel: "auto" },
  autoTitle: "",
  uploadId: null,
  sendFile: null, // what the upload sends: the file itself, or the audio the browser extracted from it
  uploadOffset: 0, // bytes durably committed by the server before the current chunk
  uploadRetries: 0, // automatic retries already used for the current chunk
  uploadStateRetries: 0, // recovery GET retries already used after a failed upload request
  validationStateRetries: 0, // transport retries used while recovering an ambiguous completion
  checkingSince: null, // clock value stamped by the first server-checking tick
  notice: null, // result of the extraction step, shown beside the validation line
  runId: 0, // id of the latest upload run; replies of other runs are stale
  starting: false, // cleaning old uploads and creating the new upload slot
  cancelling: false,
  cleanupWait: [], // upload ids whose DELETE the starting run waits for
  pendingCleanup: [], // upload ids whose DELETE failed; retried on the next file or run
  failure: null, // upload error waiting for its cleanup to finish
  serverReady: false,
  retryable: false,
  createAttemptFailed: false,
  createUncertain: false,
  createRetryPayload: null,
  validationUncertain: false, // completion outcome is unknown; retain the upload for recheck/cancel
  completionRecovery: false, // the client is reconciling a completion response before proceeding
  createRequest: 0, // id of the latest create request
  progress: NO_PROGRESS,
  validation: null,
  formError: null,
};

const isBusy = (state) => BUSY_PHASES.includes(state.phase);
export const isActive = (state) => state.starting || isBusy(state);
const retryingUncertain = (state) => state.createUncertain && Boolean(state.createRetryPayload);
const canCancel = (state) => CANCELLABLE_PHASES.includes(state.phase) || state.validationUncertain;
const validFile = (state) => Boolean(state.policy && state.file && !state.fileError && !state.fileRefused);
const modelChoices = (state) => state.asrModels || [];

function selectAsrModel(state, disabled) {
  if (!state.form.autoTranscribe) return null;
  const choices = modelChoices(state);
  if (!choices.length || !choices.some((choice) => choice.id === "auto")) return null;
  const value = choices.some((choice) => choice.id === state.form.asrModel) ? state.form.asrModel : "auto";
  const selected = choices.find((choice) => choice.id === value);
  const note = value === "auto"
    ? selected.model_name
      ? t("Theo phần cứng của máy: {model}.", { model: selected.model_name })
      : t("Theo phần cứng của máy.")
    : ASR_MODEL_TEXT[value].note;
  const warning = choices.some((choice) => choice.id !== "auto" && !choice.ready)
    ? t("Model chưa sẵn sàng không chọn được ở đây; trang cuộc họp có lệnh chuẩn bị.")
    : null;
  return {
    value,
    disabled,
    options: choices.map((choice) => ({
      id: choice.id,
      label: ASR_MODEL_TEXT[choice.id].label,
      ready: Boolean(choice.ready),
      disabled: choice.id !== "auto" && !choice.ready,
    })),
    help: warning ? [note, " ", warning] : note,
  };
}

// A valid file and form are enough: the one button uploads and checks first when needed.
export const canSubmit = (state) =>
  !isActive(state)
  && state.phase !== "saved"
  && (
    retryingUncertain(state)
    || (state.validationUncertain && Boolean(state.uploadId))
    || (validFile(state) && formValuesAreValid(state.form))
  );

function guidance(state) {
  const { form } = state;
  if (!state.policy) return t("Đang tải giới hạn file của ứng dụng…");
  if (!state.file) return t("Chọn file, nhập tên và chọn ngôn ngữ để tiếp tục.");
  if (!validFile(state)) return t("Chọn một file hợp lệ để tiếp tục.");
  if (!form.title.trim()) return t("Nhập tên cuộc họp để tiếp tục.");
  if (!form.language) return t("Chọn English hoặc Tiếng Việt để tiếp tục.");
  if (state.validationUncertain) return t("Không thể xác nhận trạng thái file. Upload vẫn được giữ lại; hãy kiểm tra lại hoặc hủy.");
  if (state.createUncertain) return t("Gửi lại đúng yêu cầu để xác minh kết quả; máy chủ sẽ không tạo bản trùng.");
  if (isActive(state)) return t("Cuộc họp sẽ được tạo ngay khi máy chủ xác nhận file đã sẵn sàng.");
  if (state.serverReady) return t("File đã sẵn sàng và thông tin cuộc họp đã đầy đủ.");
  return t("Thông tin đã đầy đủ. Bấm “Tạo cuộc họp” để tải lên, kiểm tra và tạo cuộc họp.");
}

function limits(state) {
  if (state.policy) {
    return t("Định dạng: {formats} · Tối đa {size} · Tối đa {duration}.", {
      formats: state.policy.extensions.join(", "),
      size: bytes(state.policy.maxBytes),
      duration: formatDuration(state.policy.maxDurationSeconds),
    });
  }
  return state.policyFailed ? t("Không tải được giới hạn file.") : t("Đang tải giới hạn của ứng dụng…");
}

// Everything the view shows, derived from state.
export function selectView(state) {
  const active = isActive(state);
  const locked = active || retryingUncertain(state) || state.validationUncertain;
  const submit = canSubmit(state);
  return {
    limits: limits(state),
    guidance: guidance(state),
    accept: state.policy ? state.policy.extensions.join(",") : "",
    asrModel: selectAsrModel(state, locked),
    fileInputDisabled: locked || !state.policy,
    changeFileDisabled: locked,
    fieldsDisabled: locked,
    retryHidden: !state.retryable || !state.file || active,
    retryDisabled: !submit,
    retryLabel: state.validationUncertain
      ? t("Kiểm tra lại trạng thái")
      : retryingUncertain(state)
        ? t("Xác minh và thử lại")
        : t("Tải lại từ đầu"),
    cancelHidden: !canCancel(state),
    createDisabled: !submit,
    createLabel: state.validationUncertain
      ? t("Kiểm tra lại trạng thái")
      : retryingUncertain(state)
      ? t("Xác minh và thử lại")
      : state.createAttemptFailed
        ? t("Thử tạo lại")
        : t("Tạo cuộc họp"),
    closeDisabled: state.phase === "creating" || state.starting,
  };
}

const jsonRequest = (method, body) => ({
  method,
  headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
  credentials: "same-origin",
  cache: "no-store",
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

const cleanupEffect = (uploadId, purpose) => ({
  type: "fetch",
  key: `shell/cleanup/${uploadId}`,
  url: `/api/uploads/${encodeURIComponent(uploadId)}`,
  init: { method: "DELETE", headers: { Accept: "application/json", "Content-Type": "application/json" }, credentials: "same-origin", cache: "no-store" },
  ok: "shell/upload-cleaned",
  error: "shell/upload-cleanup-failed",
  meta: { uploadId, purpose },
});

const NETWORK = t("Không kết nối được với máy chủ trên máy này.");

// A runner error as the reducer reasons about it: an HTTP answer (api, status, body) or a failed transport.
// fallback is the text for a failure that carries no message at all.
function failureOf(error, fallback) {
  if (error.status !== undefined) return { api: true, status: error.status, body: error.body, code: responseCode(error.status, error.body), message: responseMessage(error.status, error.body) };
  if (error.kind === "network") return { api: false, kind: error.kind, message: t("Mất kết nối trong lúc truyền file.") };
  if (error.kind === "timeout") return { api: false, kind: error.kind, message: t("Thời gian truyền file đã hết.") };
  if (error.kind === "aborted") return { api: false, kind: error.kind, message: t("Đã hủy lượt truyền file.") };
  return { api: false, message: error.message ? NETWORK : fallback };
}
const otherFailure = (message) => ({ api: false, message });

const live = (state, meta) => meta?.runId === state.runId;
const set = (state, patch, effects) => ({ state: { ...state, ...patch }, effects });
const same = (state) => ({ state });

function fresh(state, patch) {
  return {
    ...initialState,
    pendingCleanup: state.pendingCleanup,
    runId: state.runId + 1,
    policyRequest: state.policyRequest + 1,
    asrModelsRequest: state.asrModelsRequest + 1,
    createRequest: state.createRequest,
    ...patch,
  };
}

const without = (list, item) => list.filter((entry) => entry !== item);
const withItem = (list, item) => (list.includes(item) ? list : [...list, item]);

function prefillTitle(state, filename) {
  const finalDot = filename.lastIndexOf(".");
  const stem = finalDot >= 0 ? filename.slice(0, finalDot) : filename;
  const suggestion = Array.from(stem.trim()).slice(0, 200).join("").trim();
  // The name is only a suggestion: it never replaces a title the user typed.
  if (state.form.title.trim() && state.form.title !== state.autoTitle) return {};
  return { form: { ...state.form, title: suggestion }, autoTitle: suggestion };
}

function startUpload(state) {
  if (state.validationUncertain && state.uploadId) return recheckValidation(state);
  if (!state.policy || !state.file || state.fileError || isActive(state)) return same(state);
  const stale = [...(state.uploadId ? [state.uploadId] : []), ...without(state.pendingCleanup, state.uploadId)];
  const next = {
    ...state,
    runId: state.runId + 1,
    starting: true,
    retryable: false,
    serverReady: false,
    createAttemptFailed: false,
    createUncertain: false,
    createRetryPayload: null,
    failure: null,
    formError: null,
    progress: NO_PROGRESS,
    validation: null,
    notice: null,
    sendFile: null,
    uploadOffset: 0,
    uploadRetries: 0,
    uploadStateRetries: 0,
    validationStateRetries: 0,
    validationUncertain: false,
    completionRecovery: false,
    workflow: t("Đang tạo lượt upload. Chưa truyền byte nào."),
    uploadId: null,
    cleanupWait: stale,
  };
  if (stale.length === 0) return proceed(next);
  return { state: next, effects: stale.map((uploadId) => cleanupEffect(uploadId, "start")) };
}

// After the old uploads are gone: a video goes through the extraction step first.
function proceed(state) {
  if (VIDEO_EXTENSIONS.includes(fileExtension(state.file.name))) return startExtract(state);
  return createUploadSlot({ ...state, sendFile: state.file });
}

function startExtract(state) {
  return {
    state: {
      ...state,
      starting: false,
      phase: "extracting",
      workflow: t("Đang tách audio khỏi video ngay trên trình duyệt (copy luồng, không mã hóa lại). Chưa truyền byte nào."),
      progress: { visible: true, percent: 0, text: "0%" },
    },
    effects: [{
      type: "extract",
      key: "shell/extract",
      file: state.file,
      ok: "shell/extract-done",
      error: "shell/extract-failed",
      progress: "shell/extract-progressed",
      meta: { runId: state.runId },
    }],
  };
}

const EXTRACT_REASONS = {
  unsupported: t("trình duyệt này chưa hỗ trợ"),
  "no-audio": t("video không có luồng audio"),
  codec: t("codec audio không copy được sang file audio"),
};

function createUploadSlot(state) {
  const sent = state.sendFile;
  return {
    state: { ...state, phase: "selected", workflow: t("File đã chọn. Đang chuẩn bị lượt upload mới từ byte đầu tiên.") },
    effects: [{
      type: "fetch",
      key: "shell/upload-create",
      url: "/api/uploads",
      init: jsonRequest("POST", { filename: sent.name, size_bytes: sent.size }),
      ok: "shell/upload-created",
      error: "shell/upload-create-failed",
      meta: { runId: state.runId },
    }],
  };
}

function uploadChunkEffect(state, offset = state.uploadOffset) {
  const file = state.sendFile;
  if (!file || !Number.isSafeInteger(file.size) || file.size < 1) return null;
  const chunkBytes = state.policy?.chunkBytes ?? DEFAULT_UPLOAD_CHUNK_BYTES;
  const end = Math.min(file.size, offset + chunkBytes);
  const body = typeof file.slice === "function"
    ? file.slice(offset, end)
    : offset === 0 && end === file.size
      ? file
      : null;
  if (!body || end <= offset) return null;
  return {
    type: "upload",
    key: "shell/upload",
    url: `/api/uploads/${encodeURIComponent(state.uploadId)}/content?offset=${offset}`,
    headers: { "Content-Type": "application/octet-stream" },
    body,
    ok: "shell/upload-sent",
    error: "shell/upload-failed",
    progress: "shell/upload-progressed",
    meta: { runId: state.runId, offset, chunkBytes: end - offset },
  };
}

function sendUploadChunk(state, offset = state.uploadOffset, retries = state.uploadRetries) {
  const effect = uploadChunkEffect(state, offset);
  if (!effect) return failUpload(state, otherFailure(t("Không thể cắt phần file cần tải lên.")));
  return {
    state: { ...state, uploadOffset: offset, uploadRetries: retries, uploadStateRetries: 0 },
    effects: [effect],
  };
}

function uploadStateEffect(state) {
  return {
    type: "fetch",
    key: "shell/upload-state",
    url: `/api/uploads/${encodeURIComponent(state.uploadId)}`,
    init: jsonRequest("GET"),
    ok: "shell/upload-state-loaded",
    error: "shell/upload-state-failed",
    meta: { runId: state.runId, offset: state.uploadOffset },
  };
}

function uploadValidationStateEffect(state) {
  return {
    type: "fetch",
    key: "shell/upload-validation-state",
    url: `/api/uploads/${encodeURIComponent(state.uploadId)}`,
    init: jsonRequest("GET"),
    ok: "shell/upload-validation-state-loaded",
    error: "shell/upload-validation-state-failed",
    meta: { runId: state.runId },
  };
}

function validationPollEffect(state, delay = 1_000) {
  return {
    type: "schedule",
    key: "shell/upload-validation-poll",
    delay,
    action: { type: "shell/upload-validation-poll", meta: { runId: state.runId } },
  };
}

function uploadProgress(state, offset, loaded) {
  const total = state.sendFile?.size ?? 0;
  const cumulative = Math.min(total, Math.max(0, offset + loaded));
  const percent = total > 0 ? Math.min(100, Math.round((cumulative / total) * 100)) : 0;
  return {
    visible: true,
    indeterminate: false,
    percent,
    text: `${percent}%`,
    cumulative,
  };
}

function checkingProgress(state, elapsedSeconds) {
  const size = bytes(state.sendFile?.size ?? state.uploadOffset ?? 0);
  const seconds = Number.isFinite(elapsedSeconds) ? Math.max(0, Math.floor(elapsedSeconds)) : null;
  return {
    visible: true,
    indeterminate: true,
    percent: null,
    text: seconds === null
      ? t("Đang kiểm tra · đã truyền {size}.", { size })
      : t("Đang kiểm tra · đã truyền {size} · {seconds} giây.", { size, seconds }),
  };
}

function startUploadCompletion(state, { preserveCheckingSince = false } = {}) {
  return {
    state: {
      ...state,
      uploadOffset: state.sendFile.size,
      uploadRetries: 0,
      validationStateRetries: 0,
      validationUncertain: false,
      completionRecovery: false,
      checkingSince: preserveCheckingSince ? state.checkingSince : null,
      phase: "checking",
      workflow: t("Đã truyền xong byte. Máy chủ đang kiểm tra định dạng, thời lượng và nội dung file."),
      validation: t("Đang kiểm tra file trên máy chủ. Đây là bước xác nhận file, không phải tiến độ xử lý transcript."),
      progress: checkingProgress(state, null),
    },
    effects: [
      {
        type: "fetch",
        key: "shell/upload-complete",
        url: `/api/uploads/${encodeURIComponent(state.uploadId)}/complete`,
        init: jsonRequest("POST", {}),
        ok: "shell/upload-completed",
        error: "shell/upload-completion-failed",
        meta: { runId: state.runId },
      },
      {
        type: "schedule",
        key: "shell/upload-checking-clock",
        delay: 0,
        action: { type: "shell/upload-checking-tick", meta: { runId: state.runId } },
      },
    ],
  };
}

function recheckValidation(state) {
  const next = {
    ...state,
    runId: state.runId + 1,
    phase: "checking",
    retryable: false,
    serverReady: false,
    validationUncertain: false,
    completionRecovery: true,
    validationStateRetries: 0,
    checkingSince: null,
    formError: null,
    validation: t("Đang kiểm tra lại trạng thái file trên máy chủ."),
    workflow: t("Đang xác minh upload sau khi phản hồi hoàn tất bị gián đoạn."),
  };
  return {
    state: next,
    effects: [
      { type: "cancel", key: "shell/upload-validation-state" },
      { type: "cancel", key: "shell/upload-validation-poll" },
      { type: "cancel", key: "shell/upload-checking-clock" },
      uploadValidationStateEffect(next),
      {
        type: "schedule",
        key: "shell/upload-checking-clock",
        delay: 0,
        action: { type: "shell/upload-checking-tick", meta: { runId: next.runId } },
      },
    ],
  };
}

function recoverCompletion(state, failure) {
  const next = {
    ...state,
    completionRecovery: true,
    validationUncertain: false,
    validationStateRetries: 0,
    failure,
    workflow: t("Đang xác minh upload sau khi phản hồi hoàn tất bị gián đoạn."),
    validation: t("File đang được kiểm tra trên máy chủ."),
  };
  return { state: next, effects: [uploadValidationStateEffect(next)] };
}

function validationStatusFailure(body) {
  const code = typeof body?.failure_code === "string" && body.failure_code
    ? body.failure_code
    : "invalid_media";
  const status = code === "duration_limit" ? 413 : code === "unsupported_codec" ? 415 : 422;
  const payload = { detail: { code } };
  return { api: true, status, body: payload, code, message: responseMessage(status, payload) };
}

function validationRecoveryUncertain(state, failure) {
  return set(state, {
    phase: "error",
    serverReady: false,
    retryable: Boolean(state.file),
    fileRefused: false,
    validationUncertain: true,
    completionRecovery: true,
    failure: null,
    formError: [
      t("Chưa thể xác nhận trạng thái kiểm tra file. {message}", { message: failure.message }),
      t("Upload vẫn được giữ lại trên máy chủ. Hãy kiểm tra lại trạng thái hoặc hủy upload."),
    ],
    validation: t("Chưa xác nhận được file đã sẵn sàng; chưa có cuộc họp nào được tạo."),
    workflow: t("Chưa thể xác nhận trạng thái kiểm tra file."),
  }, [
    { type: "cancel", key: "shell/upload-checking-clock" },
    { type: "cancel", key: "shell/upload-validation-poll" },
  ]);
}

function readyForCreate(state) {
  return startCreate({
    ...state,
    failure: null,
    serverReady: true,
    retryable: false,
    checkingSince: null,
    validationStateRetries: 0,
    validationUncertain: false,
    completionRecovery: false,
    progress: { visible: true, indeterminate: false, percent: 100, text: "100%" },
    validation: t("File đã qua kiểm tra và sẵn sàng tạo cuộc họp."),
    phase: "ready",
    workflow: t("File đã sẵn sàng. Bạn có thể tạo cuộc họp."),
  });
}

function uploadFailureMessage(failure, cleaned) {
  let reason = failure.message;
  if (failure.status === 413 && failure.code !== "duration_limit") reason = t("File vượt giới hạn dung lượng. Hãy chọn file nhỏ hơn.");
  if (failure.status === 415) reason = t("Định dạng file chưa được hỗ trợ. Hãy chọn định dạng trong danh sách.");
  if (failure.status === 422) reason = t("{reason} Hãy chọn file phù hợp rồi tải lại từ đầu.", { reason });
  return [
    t("Không tạo cuộc họp. {reason} File chưa được đưa vào xử lý.", { reason }),
    cleaned
      ? t(" Phần upload dở đã được dọn.")
      : t(" Ứng dụng chưa xác nhận đã dọn phần upload dở; hãy thử lại để tạo lượt upload mới."),
  ];
}

// An upload step failed: the old upload is cleaned first, then the message says whether that worked.
function failUpload(state, failure) {
  const base = { ...state, starting: false, serverReady: false, failure };
  const cancelEffects = [
    { type: "cancel", key: "shell/upload-checking-clock" },
    { type: "cancel", key: "shell/upload-validation-state" },
    { type: "cancel", key: "shell/upload-validation-poll" },
  ];
  if (!state.uploadId) return { ...finishFailure(base, true), effects: cancelEffects };
  return {
    state: { ...base, uploadId: null, pendingCleanup: withItem(state.pendingCleanup, state.uploadId), failure: { ...failure, uploadId: state.uploadId } },
    effects: [...cancelEffects, cleanupEffect(state.uploadId, "failure")],
  };
}

function finishFailure(state, cleaned) {
  const { failure } = state;
  const retryable = !failure.api || failure.status >= 500 || failure.status === 401 || failure.status === 403;
  return set(state, {
    failure: null,
    validationUncertain: false,
    completionRecovery: false,
    retryable: retryable && Boolean(state.file),
    fileRefused: !retryable,
    formError: uploadFailureMessage(failure, cleaned),
    validation: t("File chưa sẵn sàng; không có cuộc họp nào được tạo."),
    phase: "error",
    workflow: t("Upload chưa hoàn tất."),
  });
}

function cancelUpload(state) {
  if (!canCancel(state) || state.cancelling) return same(state);
  // The id stays pending until its DELETE is confirmed, so closing while that hangs does not lose it.
  // A failure still waiting for its own DELETE is dropped: the cancel result stands.
  const base = {
    ...state,
    runId: state.runId + 1,
    starting: false,
    serverReady: false,
    cancelling: true,
    uploadId: null,
    failure: null,
    validationUncertain: false,
    completionRecovery: false,
    validationStateRetries: 0,
    pendingCleanup: state.uploadId ? withItem(state.pendingCleanup, state.uploadId) : state.pendingCleanup,
  };
  const effects = ["extract", "upload", "upload-state", "upload-retry", "upload-complete", "upload-validation-state", "upload-validation-poll", "upload-checking-clock"].map((name) => ({ type: "cancel", key: `shell/${name}` }));
  if (!state.uploadId) return { ...finishCancel(base, true), effects }; // an extraction in flight has no upload id
  return { state: base, effects: [...effects, cleanupEffect(state.uploadId, "cancel")] };
}

function finishCancel(state, cleaned) {
  return set(state, {
    cancelling: false,
    retryable: Boolean(state.file),
    createAttemptFailed: false,
    createUncertain: false,
    createRetryPayload: null,
    formError: [
      t("Đã hủy upload. Không tạo cuộc họp. File chưa được xử lý."),
      cleaned
        ? t(" Phần upload dở đã được dọn.")
        : t(" Chưa xác nhận được việc dọn upload dở; lần thử lại sẽ bắt đầu bằng một upload mới."),
    ],
    validation: t("File chưa sẵn sàng; không có cuộc họp nào được tạo."),
    phase: "aborted",
    workflow: t("Upload đã bị hủy. Có thể tải lại từ đầu."),
  });
}

// A DELETE of a half-made upload ended. A failure keeps the id for a later retry.
function cleanupSettled(state, uploadId, purpose, cleaned) {
  const waited = state.cleanupWait.includes(uploadId);
  const next = {
    ...state,
    pendingCleanup: cleaned ? without(state.pendingCleanup, uploadId) : withItem(state.pendingCleanup, uploadId),
    cleanupWait: without(state.cleanupWait, uploadId),
  };
  if (purpose === "failure" && next.failure) return finishFailure(next, cleaned);
  if (purpose === "cancel" && next.cancelling) return finishCancel(next, cleaned);
  // Only the reply that empties the wait list creates the slot; a repeat for the same id cannot.
  if (waited && next.starting && next.cleanupWait.length === 0) return proceed(next);
  return same(next);
}

function startCreate(state) {
  if (!state.serverReady || !state.uploadId) return same(state);
  if (!state.createRetryPayload && !formValuesAreValid(state.form)) return same(state);
  const payload = state.createRetryPayload || {
    upload_id: state.uploadId,
    title: state.form.title.trim(),
    meeting_date: state.form.date || null,
    language_hint: state.form.language,
  };
  const createRequest = state.createRequest + 1;
  return {
    state: {
      ...state,
      createRequest,
      createAttemptFailed: false,
      createUncertain: false,
      formError: null,
      phase: "creating",
      workflow: t("Đang lưu cuộc họp và metadata."),
    },
    effects: [
      { type: "cancel", key: "shell/upload-checking-clock" },
      { type: "cancel", key: "shell/upload-validation-state" },
      { type: "cancel", key: "shell/upload-validation-poll" },
      {
        type: "fetch",
        key: "shell/create",
        url: "/api/meetings",
        init: jsonRequest("POST", payload),
        ok: "shell/meeting-created",
        error: "shell/meeting-create-failed",
        meta: { id: createRequest, payload },
      },
    ],
  };
}

function createFailed(state, failure, payload) {
  const definite = failure.api && failure.status >= 400 && failure.status < 500;
  const code = failure.code || "";
  if (definite && code === "upload_not_ready") {
    return set(state, {
      serverReady: false,
      retryable: Boolean(state.file),
      createAttemptFailed: false,
      createUncertain: false,
      createRetryPayload: null,
      formError: t("File không còn ở trạng thái sẵn sàng. Không có cuộc họp mới được tạo; hãy tải file lại từ đầu."),
      phase: "error",
      workflow: t("File không còn sẵn sàng."),
    });
  }
  const uncertain = !definite;
  const { message } = failure;
  return set(state, {
    createAttemptFailed: true,
    createUncertain: uncertain,
    createRetryPayload: uncertain ? payload : null,
    formError: uncertain
      ? t("{message} Không thể xác nhận cuộc họp đã được tạo hay chưa. Hãy dùng “Xác minh và thử lại”; yêu cầu được gửi lại an toàn với cùng thông tin. Bạn cũng có thể đóng hộp thoại và kiểm tra lại sau.", { message })
      : t("{message} Cuộc họp chưa được lưu. File vẫn sẵn sàng; sửa thông tin nếu cần rồi thử lại.", { message }),
    phase: "error",
    workflow: uncertain ? t("Chưa thể xác nhận kết quả lưu.") : t("Máy chủ từ chối tạo cuộc họp."),
  });
}

function policyFailed(state, message) {
  return set(state, {
    policyFailed: true,
    formError: t("{message} Đóng hộp thoại rồi mở lại để tải chính sách mới.", { message }),
    phase: "error",
    workflow: t("Chưa thể bắt đầu vì thiếu chính sách file."),
  });
}

function selectFile(state, file) {
  if (!file || state.createUncertain || state.validationUncertain || isBusy(state)) return same(state);
  const previous = state.uploadId;
  const retry = without(state.pendingCleanup, previous);
  const effects = [...(previous ? [previous] : []), ...retry].map((uploadId) => cleanupEffect(uploadId, "select"));
  const base = {
    ...state,
    ...prefillTitle(state, file.name),
    file,
    fileError: null,
    fileRefused: false,
    formError: null,
    progress: NO_PROGRESS,
    validation: null,
    serverReady: false,
    retryable: false,
    createAttemptFailed: false,
    createUncertain: false,
    createRetryPayload: null,
    uploadId: null,
    sendFile: null,
    notice: null,
  };
  const refuse = (fileError, workflow) => ({ state: { ...base, fileError, phase: "error", workflow }, effects });
  if (!state.policy) {
    return refuse(t("Không thể chọn file trước khi tải được chính sách định dạng và giới hạn."), t("Chưa thể kiểm tra file."));
  }
  const extension = fileExtension(file.name);
  if (!state.policy.extensions.includes(extension)) {
    return refuse(
      t("Định dạng {extension} chưa được hỗ trợ. Chọn một trong: {formats}.", {
        extension: extension || t("không có phần mở rộng"),
        formats: state.policy.extensions.join(", "),
      }),
      t("File chưa đạt giới hạn đã công bố."),
    );
  }
  if (file.size > state.policy.maxBytes) {
    return refuse(
      t("File lớn hơn giới hạn {size}. Hãy chọn file nhỏ hơn.", { size: bytes(state.policy.maxBytes) }),
      t("File chưa đạt giới hạn đã công bố."),
    );
  }
  return {
    state: { ...base, phase: "selected", workflow: t("Đã chọn file. Nhập thông tin rồi bấm “Tạo cuộc họp”.") },
    effects,
  };
}

function closeDialog(state) {
  if (!state.open || state.phase === "creating" || state.starting) return same(state);
  // A cancel still waiting for its DELETE does not hold the dialog: close leaves that id pending.
  if (!state.cancelling && canCancel(state)) return cancelUpload(state);
  const effects = [
    { type: "cancel", key: "shell/policy" },
    { type: "cancel", key: "shell/asr-models" },
    { type: "cancel", key: "shell/upload-checking-clock" },
    { type: "cancel", key: "shell/upload-validation-state" },
    { type: "cancel", key: "shell/upload-validation-poll" },
  ];
  if (!state.createUncertain && !state.validationUncertain && state.uploadId) effects.push(cleanupEffect(state.uploadId, "close"));
  return { state: fresh(state, { open: false }), effects };
}

// The browser closed the dialog by itself (a second Escape): make the state agree and stop what is in flight.
function dialogClosed(state) {
  if (!state.open) return same(state);
  // A save in flight finishes and navigates; only the open flag follows the dialog.
  if (state.phase === "creating") return set(state, { open: false });
  const effects = ["policy", "asr-models", "extract", "upload-create", "upload", "upload-state", "upload-retry", "upload-complete", "upload-validation-state", "upload-validation-poll", "upload-checking-clock"].map((name) => ({ type: "cancel", key: `shell/${name}` }));
  const leftover = state.createUncertain || state.validationUncertain ? null : state.uploadId;
  if (leftover) effects.push(cleanupEffect(leftover, "close"));
  const pendingCleanup = leftover ? withItem(state.pendingCleanup, leftover) : state.pendingCleanup;
  return { state: fresh({ ...state, pendingCleanup }, { open: false }), effects };
}

export function reduce(state, action) {
  switch (action.type) {
    // action.stored: the remembered choices the view read from storage.
    case "shell/dialog-opened": {
      if (state.open) return same(state);
      const stored = action.stored ?? {};
      const next = fresh(state, {
        open: true,
        form: {
          ...initialState.form,
          language: ["en", "vi"].includes(stored.language) ? stored.language : "",
          autoTranscribe: stored.autoTranscribe !== "0",
        },
      });
      return {
        state: next,
        effects: [
          {
            type: "fetch",
            key: "shell/policy",
            url: "/api/uploads/policy",
            init: jsonRequest("GET"),
            ok: "shell/policy-loaded",
            error: "shell/policy-load-failed",
            meta: { id: next.policyRequest },
          },
          {
            type: "fetch",
            key: "shell/asr-models",
            url: "/api/asr-models",
            init: jsonRequest("GET"),
            ok: "shell/asr-models-loaded",
            error: "shell/asr-models-load-failed",
            meta: { id: next.asrModelsRequest },
          },
        ],
      };
    }
    case "shell/policy-loaded": {
      if (!state.open || action.meta.id !== state.policyRequest) return same(state);
      const { policy, error } = normalizePolicy(action.response.body);
      if (error) return policyFailed(state, error);
      return set(state, { policy, phase: "idle", workflow: t("Chưa chọn file.") });
    }
    case "shell/policy-load-failed":
      if (!state.open || action.meta.id !== state.policyRequest) return same(state);
      return policyFailed(state, failureOf(action.error, t("Không tải được chính sách file.")).message);
    case "shell/asr-models-loaded":
      if (!state.open || action.meta.id !== state.asrModelsRequest) return same(state);
      return set(state, { asrModels: normalizeAsrModels(action.response.body) });
    case "shell/dialog-close-requested":
      return closeDialog(state);
    case "shell/dialog-closed":
      return dialogClosed(state);
    case "shell/file-selected":
      return selectFile(state, action.file);
    case "shell/multiple-files-dropped":
      return set(state, {
        fileError: t("Chọn từng file một để tạo cuộc họp."),
        phase: "error",
        workflow: t("Chỉ nhận một file cho mỗi cuộc họp."),
      });
    // action.form: { title, date, language } as the view reads them.
    case "shell/form-edited": {
      const form = { ...state.form, title: action.form.title, date: action.form.date, language: action.form.language };
      if (state.createAttemptFailed && state.serverReady) {
        return set(state, {
          form,
          createAttemptFailed: false,
          phase: "ready",
          workflow: t("File đã sẵn sàng. Kiểm tra thông tin rồi thử tạo cuộc họp lại."),
          formError: null,
        });
      }
      return set(state, { form });
    }
    case "shell/auto-transcribe-toggled":
      return set(state, { form: { ...state.form, autoTranscribe: action.value } }, [
        { type: "storage", key: AUTO_TRANSCRIBE_KEY, area: "local", op: "write", value: action.value ? "1" : "0" },
      ]);
    case "shell/asr-model-chosen": {
      if (action.model === "auto") return set(state, { form: { ...state.form, asrModel: "auto" } });
      const choice = modelChoices(state).find((entry) => entry.id === action.model);
      if (!choice || !choice.ready) return same(state);
      return set(state, { form: { ...state.form, asrModel: action.model } });
    }
    case "shell/submit-requested":
      if (!canSubmit(state)) return same(state);
      return state.serverReady ? startCreate(state) : startUpload(state);
    case "shell/upload-cancel-requested":
      return cancelUpload(state);
    case "shell/extract-progressed": {
      if (!live(state, action.meta) || state.phase !== "extracting") return same(state);
      const percent = Math.min(100, Math.max(0, Math.round(action.ratio * 100)));
      return set(state, { progress: { visible: true, percent, text: `${percent}%` } });
    }
    case "shell/extract-done": {
      if (!live(state, action.meta) || state.phase !== "extracting") return same(state);
      const audio = action.value;
      return createUploadSlot({
        ...state,
        starting: true,
        sendFile: audio,
        notice: t("Đã tách audio khỏi video: gửi {after} thay vì {before}.", { after: bytes(audio.size), before: bytes(state.file.size) }),
      });
    }
    case "shell/extract-failed": {
      if (!live(state, action.meta) || state.phase !== "extracting") return same(state);
      const reason = EXTRACT_REASONS[action.error.kind] ?? t("lỗi khi đọc file video");
      return createUploadSlot({
        ...state,
        starting: true,
        sendFile: state.file,
        notice: t("Không tách được audio trên trình duyệt ({reason}); gửi nguyên file video, có thể lâu hơn.", { reason }),
      });
    }
    case "shell/upload-created": {
      if (!live(state, action.meta) || !state.starting) return same(state);
      const id = action.response.body && action.response.body.id;
      if (typeof id !== "string" || !id) return failUpload(state, otherFailure(t("Máy chủ không trả về mã upload.")));
      return sendUploadChunk({
        ...state,
        uploadId: id,
        starting: false,
        phase: "uploading",
        workflow: t("Đang tải file lên theo từng phần byte; tiến độ là của cả file."),
        uploadOffset: 0,
        uploadRetries: 0,
        checkingSince: null,
        progress: { visible: true, indeterminate: false, percent: 0, text: "0%" },
      }, 0, 0);
    }
    case "shell/upload-create-failed":
      if (!live(state, action.meta)) return same(state);
      return failUpload(state, failureOf(action.error, t("Máy chủ chưa thể nhận file.")));
    case "shell/upload-failed": {
      if (!live(state, action.meta) || state.phase !== "uploading") return same(state);
      const failure = failureOf(action.error, t("Máy chủ chưa thể nhận file."));
      const code = failure.code || "";
      const resumable =
        failure.kind !== "aborted"
        && (!failure.api
        || failure.status >= 500
        || failure.status === 408
        || code === "offset_mismatch"
        || code === "upload_timeout"
        || code === "upload_busy"
        || code === "upload_interrupted");
      if (!resumable) return failUpload(state, failure);
      return {
        state: {
          ...state,
          workflow: t("Đang kiểm tra số byte đã nhận để tiếp tục lượt upload."),
        },
        effects: [uploadStateEffect(state)],
      };
    }
    case "shell/upload-state-failed": {
      if (!live(state, action.meta) || state.phase !== "uploading") return same(state);
      const failure = failureOf(action.error, t("Không đọc được tiến độ upload trên máy chủ."));
      const recoverable =
        !failure.api || failure.status >= 500 || failure.status === 408 || failure.status === 429;
      if (!recoverable || state.uploadStateRetries >= MAX_CHUNK_RETRIES) {
        return failUpload(state, failure);
      }
      const retries = state.uploadStateRetries + 1;
      return {
        state: {
          ...state,
          uploadStateRetries: retries,
          workflow: t("Đang thử đọc lại số byte đã nhận để tiếp tục lượt upload."),
        },
        effects: [{
          type: "schedule",
          key: "shell/upload-state",
          delay: retryDelayMs(retries),
          action: {
            type: "shell/upload-state-retry",
            meta: { runId: state.runId, offset: state.uploadOffset },
          },
        }],
      };
    }
    case "shell/upload-state-retry":
      if (
        !live(state, action.meta)
        || state.phase !== "uploading"
        || action.meta.offset !== state.uploadOffset
      ) return same(state);
      return { state, effects: [uploadStateEffect(state)] };
    case "shell/upload-retry":
      if (
        !live(state, action.meta)
        || state.phase !== "uploading"
        || action.meta.offset !== state.uploadOffset
      ) return same(state);
      return sendUploadChunk(state, state.uploadOffset, state.uploadRetries);
    case "shell/upload-state-loaded": {
      if (
        !live(state, action.meta)
        || state.phase !== "uploading"
        || action.meta.offset !== state.uploadOffset
      ) return same(state);
      const body = action.response.body;
      const received = body && Number(body.received_bytes);
      const total = state.sendFile?.size;
      if (
        !Number.isSafeInteger(received)
        || !Number.isSafeInteger(total)
        || received < 0
        || received > total
        || (body.status !== "receiving" && body.status !== "uploaded")
      ) {
        return failUpload(state, otherFailure(t("Máy chủ trả về tiến độ upload không hợp lệ.")));
      }
      const next = {
        ...state,
        uploadOffset: received,
        uploadRetries: received === state.uploadOffset ? state.uploadRetries + 1 : 0,
        uploadStateRetries: 0,
        progress: uploadProgress(state, received, 0),
      };
      if (received === total) return startUploadCompletion(next);
      if (received === state.uploadOffset) {
        if (next.uploadRetries > MAX_CHUNK_RETRIES) {
          return failUpload(next, otherFailure(t("Không thể tiếp tục phần file sau nhiều lần thử.")));
        }
        return {
          state: { ...next, workflow: t("Đang thử lại phần file chưa được xác nhận.") },
          effects: [{
            type: "schedule",
            key: "shell/upload-retry",
            delay: retryDelayMs(next.uploadRetries),
            action: { type: "shell/upload-retry", meta: { runId: state.runId, offset: received } },
          }],
        };
      }
      return sendUploadChunk(next, received, 0);
    }
    case "shell/upload-completion-failed": {
      if (!live(state, action.meta)) return same(state);
      const failure = failureOf(action.error, t("Máy chủ chưa thể kiểm tra file."));
      const ambiguous = !failure.api || failure.status >= 500 || failure.status === 408 || failure.status === 429;
      return ambiguous ? recoverCompletion(state, failure) : failUpload(state, failure);
    }
    case "shell/upload-validation-poll":
      if (!live(state, action.meta) || state.phase !== "checking") return same(state);
      return { state, effects: [uploadValidationStateEffect(state)] };
    case "shell/upload-validation-state-loaded": {
      if (!live(state, action.meta) || state.phase !== "checking") return same(state);
      const body = action.response.body;
      if (body?.status === "ready") return readyForCreate(state);
      if (body?.status === "validating") {
        const next = {
          ...state,
          completionRecovery: true,
          validationStateRetries: 0,
          validation: t("File đang được kiểm tra trên máy chủ."),
          workflow: t("Máy chủ đang kiểm tra file; ứng dụng sẽ tiếp tục chờ trạng thái xác nhận."),
        };
        return { state: next, effects: [validationPollEffect(next)] };
      }
      if (body?.status === "uploaded") {
        return startUploadCompletion(
          { ...state, completionRecovery: true, validationStateRetries: 0 },
          { preserveCheckingSince: true },
        );
      }
      if (body?.status === "failed") return failUpload(state, validationStatusFailure(body));
      return failUpload(
        state,
        otherFailure(t("Máy chủ không còn xác nhận file ở trạng thái đang kiểm tra.")),
      );
    }
    case "shell/upload-validation-state-failed": {
      if (!live(state, action.meta) || state.phase !== "checking") return same(state);
      const failure = failureOf(action.error, t("Không đọc được trạng thái kiểm tra file trên máy chủ."));
      const recoverable = !failure.api || failure.status >= 500 || failure.status === 408 || failure.status === 429;
      if (!recoverable) return failUpload(state, failure);
      if (state.validationStateRetries >= MAX_CHUNK_RETRIES) {
        return validationRecoveryUncertain(state, failure);
      }
      const retries = state.validationStateRetries + 1;
      return {
        state: {
          ...state,
          validationStateRetries: retries,
          completionRecovery: true,
          workflow: t("Đang thử đọc lại trạng thái kiểm tra file trên máy chủ."),
        },
        effects: [validationPollEffect(state, retryDelayMs(retries))],
      };
    }
    case "shell/upload-progressed": {
      if (!live(state, action.meta) || state.phase !== "uploading") return same(state);
      const offset = action.meta.offset ?? state.uploadOffset;
      if (offset !== state.uploadOffset) return same(state);
      const loaded = Number.isFinite(action.loaded) ? action.loaded : 0;
      const progress = uploadProgress(state, offset, loaded);
      if (action.lengthComputable && action.total > 0) return set(state, { progress });
      return set(state, {
        progress: { ...progress, text: t("{size} đã gửi", { size: bytes(progress.cumulative) }) },
      });
    }
    case "shell/upload-checking-tick": {
      if (!live(state, action.meta) || state.phase !== "checking") return same(state);
      const now = Number(action.now);
      const started = Number.isFinite(state.checkingSince)
        ? state.checkingSince
        : (Number.isFinite(now) ? now : null);
      const elapsed = Number.isFinite(now) && Number.isFinite(started) ? Math.max(0, now - started) / 1000 : null;
      return {
        state: { ...state, checkingSince: started, progress: checkingProgress(state, elapsed) },
        effects: [{
          type: "schedule",
          key: "shell/upload-checking-clock",
          delay: 1_000,
          action: { type: "shell/upload-checking-tick", meta: { runId: state.runId } },
        }],
      };
    }
    case "shell/upload-sent": {
      if (!live(state, action.meta) || state.phase !== "uploading") return same(state);
      const body = action.response.body;
      const reported = body && Number(body.received_bytes);
      const nextOffset = Number.isSafeInteger(reported)
        ? reported
        : action.meta.offset + action.meta.chunkBytes;
      const total = state.sendFile?.size;
      if (!Number.isSafeInteger(total) || nextOffset < action.meta.offset || nextOffset > total) {
        return failUpload(state, otherFailure(t("Máy chủ trả về số byte upload không hợp lệ.")));
      }
      const next = {
        ...state,
        uploadOffset: nextOffset,
        uploadRetries: 0,
        progress: uploadProgress(state, nextOffset, 0),
      };
      if (nextOffset === total) return startUploadCompletion(next);
      return sendUploadChunk(next, nextOffset, 0);
    }
    case "shell/upload-completed": {
      if (!live(state, action.meta) || state.phase !== "checking") return same(state);
      const body = action.response.body;
      if (body?.status === "ready") return readyForCreate(state);
      if (body?.status === "validating") {
        const next = {
          ...state,
          completionRecovery: false,
          validationStateRetries: 0,
          validation: t("File đang được kiểm tra trên máy chủ."),
          workflow: t("Máy chủ đang kiểm tra file; ứng dụng sẽ tiếp tục chờ trạng thái xác nhận."),
        };
        return { state: next, effects: [validationPollEffect(next)] };
      }
      if (body?.status === "uploaded") {
        return startUploadCompletion(state, { preserveCheckingSince: true });
      }
      if (body?.status === "failed") return failUpload(state, validationStatusFailure(body));
      return failUpload(state, otherFailure(t("Máy chủ chưa xác nhận file ở trạng thái ready. Không thể tạo cuộc họp lúc này.")));
    }
    case "shell/meeting-created": {
      if (state.phase !== "creating" || action.meta.id !== state.createRequest) return same(state);
      const { body } = action.response;
      const meeting = body && body.meeting && typeof body.meeting === "object" ? body.meeting : body;
      if (!meeting || typeof meeting.id !== "string" || !meeting.id) {
        return createFailed(state, otherFailure(t("Máy chủ không trả về mã cuộc họp sau khi lưu.")), action.meta.payload);
      }
      return {
        state: { ...state, phase: "saved", workflow: t("Cuộc họp đã được lưu."), serverReady: false, createRetryPayload: null },
        effects: [
          { type: "storage", key: LAST_LANGUAGE_KEY, area: "local", op: "write", value: action.meta.payload.language_hint },
          ...(state.form.autoTranscribe
            ? [{ type: "storage", key: `${AUTO_TRANSCRIBE_HANDOFF_PREFIX}${meeting.id}`, area: "session", op: "write", value: state.form.asrModel }]
            : []),
          { type: "navigate", key: "shell/navigate", url: `/meetings/${encodeURIComponent(meeting.id)}` },
        ],
      };
    }
    case "shell/meeting-create-failed":
      if (state.phase !== "creating" || action.meta.id !== state.createRequest) return same(state);
      return createFailed(state, failureOf(action.error, t("Không thể xác nhận kết quả lưu cuộc họp.")), action.meta.payload);
    case "shell/upload-cleaned":
      return cleanupSettled(state, action.meta.uploadId, action.meta.purpose, true);
    case "shell/upload-cleanup-failed":
      // The server no longer has the upload: that is as clean as it gets.
      return cleanupSettled(state, action.meta.uploadId, action.meta.purpose, action.error.status === 404);
    default:
      return same(state);
  }
}
