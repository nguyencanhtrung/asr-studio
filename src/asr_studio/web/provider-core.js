// Pure core of the provider connections (ChatGPT and Google Antigravity): state, one reducer for every
// provider, selectors. No DOM, network, timer, storage or clock: node --test loads this file directly.
// Contract: docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design.md §11.6.
//
// A provider is a plain-data descriptor (endpoints, login kind, poll delays, effort support, storage
// key, copy). The flows (load, login, poll, cancel, save, verify, disconnect) are written once;
// a difference between providers is a descriptor field, read by a small branch.
// Time enters through `now` on the action (the runner stamps it); stale replies are dropped by ids in the state.
//
// User-visible text is data: t(text, params) only keeps the Vietnamese source string; the view translates it
// with the real t(). A param is a primitive, a t() result, a timestamp() marker, or an array of those.

import { combine } from "./store.js";
import { ASR_MODEL_TEXT, bytes, errorMessageForCode } from "./shell-core.js";
import { ASR_MODEL_GUIDANCE } from "./task-core.js";

const t = (text, params) => (params ? { text, params } : { text });
const fill = (message, params) => ({ ...message, params });
export const timestamp = (value) => ({ fmt: "timestamp", value });

const KNOWN = ["connected", "disconnected", "reconnect_required"];
const SESSION_DONE = ["connected", "cancelled", "expired", "failed"];
const VIETNAMESE = /[À-ỹ]/u;
const CHOICE_CHANGED = { type: "call", key: "provider/choice-changed", name: "provider.choice-changed" };

const EFFORT_LABELS = {
  none: t("Không suy luận (none)"),
  low: t("Thấp (low)"),
  medium: t("Trung bình (medium)"),
  high: "Cao (high)",
  xhigh: t("Rất cao (xhigh)"),
  max: t("Tối đa (max)"),
};
const DEFAULT_EFFORT_LABEL = t("Mặc định của mô hình");

// Request errors every provider shares (the old connectionRequest table).
const REQUEST_ERRORS = {
  revocation_unconfirmed: t("Chưa xác nhận thu hồi phiên ChatGPT cũ. Hãy thử lại hoặc thu hồi quyền trong ChatGPT → Cài đặt → Mức sử dụng."),
  provider_busy: t("Một thao tác ChatGPT khác đang chạy. Hãy đợi một chút rồi thử lại."),
  provider_timeout: t("ChatGPT chưa phản hồi trong thời gian cho phép. Bạn có thể thử lại."),
  NETWORK_ERROR: t("Không kết nối được với máy chủ trên máy này."),
  INVALID_AUTHORIZATION_URL: t("Máy chủ trả về địa chỉ đăng nhập không hợp lệ."),
  UNSAFE_AUTHORIZATION_URL: t("Máy chủ không được gửi thông tin nhận dạng nhạy cảm đến trình duyệt."),
  INVALID_LOGIN_SESSION: t("Máy chủ trả về phiên đăng nhập không hợp lệ."),
  INVALID_LOGIN_STATUS: t("Máy chủ trả về trạng thái phiên đăng nhập không hợp lệ."),
  CANCEL_NOT_CONFIRMED: t("Máy chủ chưa hủy được phiên đăng nhập."),
  POPUP_BLOCKED: t("Trình duyệt đang chặn cửa sổ đăng nhập. Cho phép mở cửa sổ cho không gian làm việc này rồi thử lại."),
  SIGN_IN_FAILED: t("Không thể hoàn tất đăng nhập ChatGPT."),
  invalid_callback_url: t("URL callback không đúng với phiên đăng nhập hoặc cấu hình loopback. Hãy dán lại URL đầy đủ."),
  callback_replayed: t("URL callback này đã được dùng. Hãy bắt đầu hoặc tiếp tục phiên đăng nhập hiện tại."),
  invalid_registration_name: t("Tên đăng ký ChatGPT không hợp lệ."),
  registration_recreation_unavailable: t("Không thể tạo lại đăng ký ChatGPT từ kết nối hiện tại."),
  INVALID_PROVIDER_STATUS: t("Máy chủ chưa công bố trạng thái kết nối ChatGPT hợp lệ."),
  DISCONNECT_NOT_CONFIRMED: t("Máy chủ chưa xác nhận đã ngắt kết nối ChatGPT."),
  provider_reconnect_required: t("Tài khoản ChatGPT cần đăng nhập lại trước khi tiếp tục."),
  chatgpt_reconnect_required: t("Tài khoản ChatGPT cần đăng nhập lại trước khi tiếp tục."),
  reconnect_required: t("Tài khoản ChatGPT cần đăng nhập lại trước khi tiếp tục."),
  provider_auth_error: t("ChatGPT không chấp nhận thông tin đăng nhập hiện tại. Hãy kết nối lại tài khoản."),
  subscription_sharing_invalid_user: t("ChatGPT không chấp nhận thông tin đăng nhập hiện tại. Hãy kết nối lại tài khoản."),
  chatpass_v2_scope_not_authorized: t("Tài khoản chưa cấp quyền dùng gói ChatGPT. Hãy kết nối lại và cấp quyền cần thiết."),
  chatpass_v2_invalid_authorization_context: t("Cấu hình cấp quyền ChatGPT không hợp lệ. Hãy kiểm tra cấu hình kết nối trước khi đăng nhập lại."),
  scope_not_authorized: t("Tài khoản chưa cấp quyền dùng gói ChatGPT. Hãy kết nối lại và cấp quyền cần thiết."),
  invalid_grant: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  invalid_refresh_token: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  token_expired: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  refresh_token_expired: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  refresh_token_invalidated: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  refresh_token_reused: t("Quyền đăng nhập ChatGPT đã hết hạn. Hãy kết nối lại tài khoản."),
  model_not_available: t("Mô hình đã chọn không khả dụng với tài khoản ChatGPT này. Hãy chọn mô hình khác."),
  model_unavailable: t("Mô hình đã chọn không khả dụng với tài khoản ChatGPT này. Hãy chọn mô hình khác."),
  invalid_reasoning_effort: t("Mức suy luận này không được mô hình đã chọn hỗ trợ."),
};

function defaultError(code, status, provider) {
  if (Object.hasOwn(REQUEST_ERRORS, code) && provider !== "antigravity") return REQUEST_ERRORS[code];
  const shared = errorMessageForCode(code, status, provider);
  if (shared) return shared;
  if (status === 401) return t("Phiên truy cập không gian làm việc không còn hợp lệ. Hãy mở lại liên kết trong terminal.");
  if (status === 403) return t("ChatGPT đã từ chối yêu cầu. Hãy kiểm tra quyền truy cập tài khoản rồi thử lại.");
  if (status === 404) return t("Không tìm thấy phiên hoặc dữ liệu yêu cầu.");
  if (status === 409) return t("Trạng thái đã thay đổi. Hãy tải lại thông tin rồi thử lại.");
  if (status === 429) return t("Yêu cầu đã chạm giới hạn hiện tại. Hãy kiểm tra mức sử dụng ChatGPT.");
  if (status >= 500) return t("Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.");
  return t("Không thể hoàn tất thao tác. Hãy kiểm tra trạng thái rồi thử lại.");
}

// A server string is kept only when it is Vietnamese; our own t() result is kept as it is.
function localizedError(code, message, status, provider) {
  const shared = errorMessageForCode(code, status, provider);
  if (shared && ["provider_busy", "provider_in_use", "provider_unavailable"].includes(code)) return shared;
  if (Object.hasOwn(REQUEST_ERRORS, code) && provider !== "antigravity") return REQUEST_ERRORS[code];
  if (typeof message === "string" && VIETNAMESE.test(message)) return message;
  if (message && typeof message === "object") return message;
  if (shared) return shared;
  return defaultError(code, status, provider);
}

// Runner error {message, status?, body?} -> {code, message, status}.
function requestError(error, provider) {
  if (error.status === undefined) return { code: "NETWORK_ERROR", message: REQUEST_ERRORS.NETWORK_ERROR, status: 0 };
  const status = error.status;
  const payload = error.body && typeof error.body === "object" ? error.body : null;
  const detail = payload && (payload.detail || payload.error);
  if (detail && typeof detail === "object") {
    const code = typeof detail.code === "string" ? detail.code : `HTTP_${status}`;
    return { code, message: localizedError(code, detail.message, status, provider), status };
  }
  const code = `HTTP_${status}`;
  const text = typeof detail === "string" && detail.trim() ? detail.trim() : "";
  return { code, message: localizedError(code, text, status, provider), status };
}

const GENERIC_ERROR = { code: "CONNECTION_ERROR" }; // a 2xx reply that is not the expected JSON object

export const CHATGPT = {
  id: "chatgpt",
  launcherName: "OpenAI",
  endpoints: {
    providers: "/api/providers",
    preferences: "/api/notes-preferences",
    models: "/api/providers/chatgpt/models",
    loginSessions: "/api/providers/chatgpt/login-sessions",
    registrationRecreate: "/api/providers/chatgpt/registration/recreate",
    verify: "/api/providers/chatgpt/verify",
    connection: "/api/providers/chatgpt/connection",
  },
  catalog: "preferences", // the saved choice lives in /api/notes-preferences; saving PUTs it there
  storageKey: "asr.chatgpt.pendingLogin",
  login: {
    kind: "popup", // reserve a window, then send it to the authorization URL
    firstPoll: 500,
    poll: 1000,
    clientExpiry: true, // the poll ends the login when expires_at has passed
    restoreExpired: "poll", // a restored login is polled once; the poll finds it expired
    pollErrors: "retry", // a failed poll shows "check again"; no automatic retry
    cancelDisables: true,
    cancelStrict: true, // a cancel reply with an unknown status is an error
    clearOnFinish: true, // finishing a login clears the error and the message of the page
  },
  strictStatus: true, // a status outside connected/disconnected/reconnect_required is not a valid answer
  loadFailure: "keep-last", // a failed load keeps the last known status
  exclusive: false, // saving and verifying can overlap
  verifyWithEffort: true,
  confirmDisconnect: true, // two steps: ask, then DELETE; the reply must say disconnected
  clearMessages: true, // opening the dialog or starting a login clears the message line
  effort: { kind: "per-model", initial: "default" },
  reconnect: {
    suffix: "reconnect_required",
    ignoreStatus: 401, // a 401 is the workspace session, not the provider
    lowercase: true,
    codes: [
      "provider_auth_error",
      "subscription_sharing_invalid_user",
      "scope_not_authorized",
      "chatpass_v2_scope_not_authorized",
      "chatpass_v2_invalid_authorization_context",
      "invalid_grant",
      "invalid_refresh_token",
      "token_expired",
      "refresh_token_expired",
      "refresh_token_invalidated",
      "refresh_token_reused",
    ],
    local: false, // refresh the status first; no local guess
  },
  errorStyle: "chatgpt",
  errors: {},
  copy: {
    status: {
      connected: t("Đã kết nối"),
      reconnect_required: t("Cần đăng nhập lại"),
      disconnected: t("Chưa kết nối"),
      other: t("Chưa rõ trạng thái"),
    },
    loading: t("Đang kiểm tra"),
    unavailable: t("Không khả dụng"),
    unreadable: t("Không đọc được trạng thái"),
    noVerification: t("Chưa kiểm tra"),
    noAccount: t("Chưa có thông tin"),
    invalidStatus: t("Máy chủ chưa công bố trạng thái kết nối ChatGPT hợp lệ."),
    pending: t("Đang chờ bạn hoàn tất đăng nhập trong cửa sổ ChatGPT…"),
    pollFailed: t("Chưa đọc được trạng thái đăng nhập. Bạn có thể thử kiểm tra lại."),
    windowTitle: "Continue with ChatGPT",
    windowText: t("Đang chuẩn bị đăng nhập ChatGPT…"),
    replaced: t("Đã thay thế phiên đăng nhập đang chờ. Hãy tiếp tục ở cửa sổ ChatGPT mới; phản hồi từ cửa sổ cũ sẽ bị bỏ qua."),
    windowClosed: t("Cửa sổ đăng nhập đã đóng. Hãy hủy phiên này rồi bắt đầu lại."),
    loginConnected: t("Đã xác thực tài khoản ChatGPT."),
    callbackPending: t("Dán toàn bộ URL callback sau khi đăng nhập trên máy khác, rồi gửi để hoàn tất ở đây."),
    callbackSubmitting: t("Đang gửi URL callback…"),
    callbackFailed: t("URL callback chưa được chấp nhận. Hãy kiểm tra và dán lại URL đầy đủ."),
    callbackSubmitted: t("Đã gửi URL callback. Đang chờ máy chủ xác nhận…"),
    loginCancelled: t("Đã hủy đăng nhập ChatGPT."),
    loginExpired: t("Phiên đăng nhập đã hết hạn. Hãy bắt đầu lại để kết nối ChatGPT."),
    loginFailed: t("Đăng nhập ChatGPT chưa hoàn tất."),
    loginFailedError: t("Không thể hoàn tất đăng nhập ChatGPT."),
    choose: t("Chọn mô hình"),
    noModels: t("Chưa có mô hình khả dụng"),
    unavailableOption: t("{model} — không khả dụng với tài khoản này"),
    connectToLoad: t("Kết nối ChatGPT để tải mô hình"),
    unavailableSaved: t("Mô hình đã lưu “{model}” hiện không khả dụng. Hãy chọn mô hình khác để thay đổi lựa chọn."),
    chooseToSave: t("Chọn mô hình rồi lưu để dùng cho ghi chú cuộc họp."),
    unsavedModel: t("Lựa chọn mới chưa được lưu."),
    unsavedEffort: t("Mức suy luận mới chưa được lưu."),
    saving: t("Đang lưu mô hình…"),
    saved: t("Đã lưu {model} · effort {effort} cho ghi chú cuộc họp."),
    saveFailed: t("Chưa lưu được mô hình. Lựa chọn đã lưu trước đó vẫn được giữ nguyên."),
    verifying: t("Đang chạy một yêu cầu kiểm tra tổng hợp…"),
    verified: t("Đã xác minh {model} · effort {effort} lúc {time}."),
    verifyFailed: t("Không xác minh được kết nối. Yêu cầu không được tự động gửi lại."),
    disconnecting: t("Đang ngắt kết nối…"),
    disconnect: t("Ngắt kết nối"),
    disconnectedRemote: t("Đã xóa thông tin đăng nhập trong không gian làm việc và xác nhận thu hồi quyền truy cập từ xa."),
    disconnectedLocal: t("Đã ngắt kết nối trong không gian làm việc. ChatGPT chưa xác nhận thu hồi quyền từ xa; hãy kiểm tra ChatGPT → Cài đặt → Mức sử dụng."),
    recreateStarted: t("Quyền kết nối cũ đã được thu hồi. Hãy tiếp tục trong cửa sổ ChatGPT mới."),
    recreateConnected: t("Đã tạo lại đăng ký ASR Studio và xác thực tài khoản ChatGPT."),
    recreateSubmitting: t("Đang tạo lại đăng ký…"),
    recreate: t("Đăng ký lại"),
    recreateConfirm: t("Đăng nhập lại"),
  },
};

export const ANTIGRAVITY = {
  id: "antigravity",
  launcherName: "Google",
  endpoints: {
    providers: "/api/providers",
    preferences: null,
    models: "/api/providers/antigravity/models",
    preference: "/api/providers/antigravity/preference",
    loginSessions: "/api/providers/antigravity/login-sessions",
    verify: "/api/providers/antigravity/verify",
    connection: "/api/providers/antigravity/connection",
  },
  catalog: "self", // the catalog carries the saved model and effort; saving PUTs the provider preference
  storageKey: "asr.antigravity.pendingLogin",
  login: {
    kind: "code", // a link and a code input shown in the dialog; polling starts at once
    firstPoll: 0,
    poll: 1000,
    pollError: 2000,
    clientExpiry: false,
    restoreExpired: "discard", // an expired stored login is dropped without a request
    pollErrors: "reschedule",
    cancelDisables: false,
    cancelStrict: false, // any cancel reply ends the login
    clearOnFinish: false,
  },
  strictStatus: false,
  loadFailure: "badge-error",
  exclusive: true, // one operation at a time: save, verify, disconnect
  verifyWithEffort: false,
  confirmDisconnect: true,
  clearMessages: false,
  effort: { kind: "fixed", initial: "high" },
  effortLabels: { medium: "Medium", high: "High" },
  reconnect: {
    suffix: undefined,
    ignoreStatus: undefined,
    lowercase: false,
    codes: ["reconnect_required", "provider_disconnected"],
    local: true, // mark the status reconnect_required/disconnected at once, then refresh it
  },
  errorStyle: "antigravity",
  errors: {
    omp_unavailable: t("Bản cài này chưa có OMP 18.5.0. Hãy dựng lại image có runner đã pin."),
    omp_invalid_runtime: t("OMP không đáp ứng cấu hình cô lập bắt buộc."),
    reconnect_required: t("Phiên Google Antigravity không còn hiệu lực. Hãy kết nối lại."),
    provider_disconnected: t("Hãy kết nối Google Antigravity trước."),
    provider_quota: t("Tài khoản hoặc model Antigravity đã chạm giới hạn hiện tại."),
    model_unavailable: t("Model này không còn khả dụng với tài khoản Antigravity."),
    provider_timeout: t("OMP hoặc Google Antigravity không phản hồi trong thời gian cho phép."),
    DISCONNECT_NOT_CONFIRMED: t("Máy chủ chưa xác nhận đã ngắt kết nối Antigravity."),
  },
  copy: {
    status: {
      connected: t("Đã kết nối"),
      reconnect_required: t("Cần kết nối lại"),
      disconnected: t("Chưa kết nối"),
      other: t("Không khả dụng"),
    },
    loading: t("Đang kiểm tra"),
    unavailable: t("Không khả dụng"),
    unreadable: null,
    noVerification: null,
    noAccount: t("Chưa có thông tin"),
    invalidStatus: t("Thiếu trạng thái Antigravity."),
    pending: t("Hoàn tất đăng nhập Google trong trang do OMP cung cấp."),
    invalidUrl: t("OMP trả về URL đăng nhập không hợp lệ."),
    unsafeUrl: t("OMP trả về URL đăng nhập không an toàn."),
    serverError: t("Không hoàn tất được thao tác Antigravity. Yêu cầu không được tự động gửi lại."),
    otherError: t("Không hoàn tất được thao tác Antigravity."),
    loginConnected: t("Đã kết nối Google Antigravity qua OMP."),
    loginCancelled: t("Đã hủy đăng nhập Antigravity."),
    choose: t("Chọn mô hình Antigravity"),
    noModels: t("Không có mô hình Antigravity khả dụng"),
    unavailableOption: t("{model} — không còn khả dụng"),
    unavailableSaved: t("Model đã lưu không còn trong catalog. Hãy chọn model khác."),
    connectToLoad: t("Kết nối Antigravity để tải model"),
    unsavedModel: t("Lựa chọn mới chưa được lưu."),
    unsavedEffort: t("Lựa chọn mới chưa được lưu."),
    saved: t("Đã lưu {model} · mức suy nghĩ {effort} cho Antigravity."),
    verifying: t("Đang gửi một yêu cầu tổng hợp qua model đã chọn…"),
    verified: t("Đã xác minh {model} lúc {time}."),
    verifyFailed: t("Kiểm tra thất bại; yêu cầu không được tự động gửi lại."),
    disconnecting: t("Đang ngắt kết nối…"),
    disconnect: t("Ngắt kết nối"),
    disconnectedLocal: t("Đã ngắt kết nối Antigravity và vô hiệu hóa credential OMP đã lưu."),
  },
};

// Header buttons: visible status and the spoken equivalent for each launcher state.
const LAUNCHER_COPY = {
  loading: [t("Đang kiểm tra"), t("đang kiểm tra trạng thái")],
  connected: [t("Đã kết nối"), t("đã kết nối")],
  reconnect_required: [t("Cần đăng nhập lại"), t("cần đăng nhập lại")],
  disconnected: [t("Chưa kết nối"), t("chưa kết nối")],
  error: [t("Không khả dụng"), t("không đọc được trạng thái")],
};

function initial(d) {
  return {
    id: d.id,
    info: null, // the provider object of /api/providers
    launcher: null, // {status, account}: what the header button shows; null until a status is known
    badge: { state: "loading", text: d.copy.loading },
    statusText: null,
    summary: null, // last verification, as text data; null hides the row
    loading: false,
    loadId: 0, // id of the latest load; other replies are stale
    headerGen: null, // loadId when the page-load status read started
    models: [],
    modelOptions: null, // option list of the model select, as data; null leaves the page markup alone
    selectedModel: "",
    savedModel: "",
    selectedEffort: d.effort.initial,
    savedEffort: d.effort.initial,
    preferences: { provider: null, models: {}, reasoning_effort: "default" },
    modelStatus: { state: "", text: null },
    verifyResult: { state: "", text: null },
    message: null,
    error: null, // {code, text, retryPoll}
    login: null, // {id, expiresAt}
    loginGen: 0, // id of the latest login/poll run; other replies are stale
    loginCopy: null,
    loginLink: null,
    inputPrompt: null,
    inputOpen: false,
    callbackPending: false,
    callbackRequest: 0,
    recreatingRegistration: false,
    recreateConfirming: false,
    recreateName: "ASR Studio",
    signInDisabled: false,
    cancelling: false,
    pending: { save: false, verify: false, disconnect: false, recreate: false },
    confirming: false, // chatgpt: the "keep or disconnect" question is open
    afterLoad: null, // "disconnected": answer notes.js when the load that follows a disconnect settles
    ui: null, // one-shot request to the view: focus:* or clear-input
  };
}

export const initialState = { chatgpt: initial(CHATGPT), antigravity: initial(ANTIGRAVITY), launcher: { scroll: false, section: "ai" }, asr: { status: "idle", models: [], request: 0, copy: null, installer: { available: false, models_writable: false }, open: false, notice: null } };

const done = (state, effects = []) => ({ state, effects });
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

function call(d, key, op, url, { method = "GET", body, id, extra } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET" && method !== "HEAD") headers["Content-Type"] = "application/json";
  const init = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) init.body = JSON.stringify(body);
  return {
    type: "fetch",
    key: `${d.id}/${key}`,
    url,
    init,
    ok: "provider/response",
    error: "provider/request-failed",
    meta: { provider: d.id, op, id, ...extra },
  };
}

const loginUrl = (d, id, tail = "") => `${d.endpoints.loginSessions}/${encodeURIComponent(id)}${tail}`;
const windowEffect = (d, op, extra) => ({ type: "open-window", key: `${d.id}/login-window`, op, meta: { provider: d.id }, ...extra });
const pollWait = (d, delay, gen) => ({ type: "schedule", key: `${d.id}/poll-wait`, delay, action: { type: "provider/poll-due", provider: d.id, gen } });
const stopPoll = (d) => [{ type: "cancel", key: `${d.id}/poll-wait` }, { type: "cancel", key: `${d.id}/poll` }];
const persist = (d, login) =>
  login
    ? { type: "storage", key: d.storageKey, area: "session", op: "write", value: JSON.stringify(login) }
    : { type: "storage", key: d.storageKey, area: "session", op: "remove" };

// ---- errors ----

function errorText(d, error) {
  const code = (error && error.code) || "CONNECTION_ERROR";
  if (d.errorStyle === "chatgpt") return localizedError(code, error && error.message, error && error.status, d.id);
  if (["provider_busy", "provider_in_use", "provider_unavailable"].includes(code)) {
    const shared = errorMessageForCode(code, error && error.status, d.id);
    if (shared) return shared;
  }
  const known = Object.hasOwn(d.errors, error?.code) ? d.errors[error.code] : null;
  if (known) return known;
  if (code === "NETWORK_ERROR") return REQUEST_ERRORS.NETWORK_ERROR;
  if (error && error.status === 429) return d.errors.provider_quota;
  if (error && error.status >= 500) return d.copy.serverError;
  return errorMessageForCode(code, error && error.status, d.id) || d.copy.otherError;
}

function showError(d, s, error, retryPoll = false) {
  return { ...s, error: { code: (error && error.code) || "CONNECTION_ERROR", text: errorText(d, error), retryPoll } };
}

function needsReconnect(d, error) {
  const rule = d.reconnect;
  if (!error || error.status === rule.ignoreStatus) return false;
  const raw = String(error.code || "");
  const code = rule.lowercase ? raw.toLowerCase() : raw;
  return (rule.suffix !== undefined && code.endsWith(rule.suffix)) || rule.codes.includes(code);
}

// ---- provider status ----

function launcherOf(status, account) {
  return { status: KNOWN.includes(status) ? status : "error", account: account || "" };
}

function modelName(s, modelId) {
  return s.models.find((model) => model.id === modelId)?.display_name || modelId;
}

function summaryOf(d, s, verification) {
  if (!(verification && verification.model && verification.verified_at)) return null;
  const name = modelName(s, verification.model);
  if (!d.verifyWithEffort) return [name, " · ", timestamp(verification.verified_at)];
  return [name, " · effort ", verification.reasoning_effort || "default", " · ", timestamp(verification.verified_at)];
}

// The old renderProvider: the new status reaches the badge, the ledger, the verification row and the header button.
function applyProvider(d, s, info) {
  const status = info.status;
  const text = KNOWN.includes(status) ? d.copy.status[status] : d.copy.status.other;
  return {
    ...s,
    info,
    badge: { state: KNOWN.includes(status) ? status : "error", text },
    statusText: text,
    summary: summaryOf(d, s, info.verification),
    launcher: launcherOf(status, info.account?.label),
  };
}

function placeholderOptions(label) {
  return [{ value: "", label, disabled: false }];
}

function clearModels(d, s) {
  return { ...s, models: [], selectedModel: "", savedModel: "", modelOptions: placeholderOptions(d.copy.connectToLoad) };
}

function localReconnect(d, s, error) {
  const status = error.code === "provider_disconnected" ? "disconnected" : "reconnect_required";
  const info = { id: d.id, connection_version: 0, ...s.info, status, account: null, verification: null };
  return applyProvider(d, clearModels(d, s), info);
}

// ---- load: providers -> (preferences) -> models ----

function startLoad(d, s) {
  const loadId = s.loadId + 1;
  let next = { ...s, loadId, loading: true, error: null };
  if (d.loadFailure === "keep-last") next = { ...next, badge: { state: "loading", text: d.copy.loading }, launcher: { status: "loading", account: "" } };
  return done(next, [call(d, "load", "providers", d.endpoints.providers, { id: loadId })]);
}

function settle(d, s) {
  let next = { ...s, loading: false };
  const effects = [];
  if (next.afterLoad === "disconnected") {
    next = { ...next, afterLoad: null, pending: { ...next.pending, disconnect: false } };
    effects.push(CHOICE_CHANGED);
  }
  return done(next, effects);
}

function normalizePreferences(payload) {
  return {
    provider: ["chatgpt", "antigravity"].includes(payload.provider) ? payload.provider : null,
    models: isObject(payload.models) ? payload.models : {},
    reasoning_effort: typeof payload.reasoning_effort === "string" ? payload.reasoning_effort : "default",
  };
}

const modelAvailable = (s, modelId) => Boolean(modelId && s.models.some((model) => model.id === modelId));

function supportedEfforts(s, modelId) {
  const model = s.models.find((item) => item.id === modelId);
  return Array.isArray(model?.reasoning_efforts) ? model.reasoning_efforts.filter((effort) => Object.hasOwn(EFFORT_LABELS, effort)) : [];
}

const effortValue = (d, s, modelId, wanted) => (d.effort.kind === "per-model" ? (supportedEfforts(s, modelId).includes(wanted) ? wanted : "default") : wanted);

function renderModels(d, s, catalog) {
  const models = (Array.isArray(catalog.models) ? catalog.models : []).filter(
    (model) => model && typeof model.id === "string" && typeof model.display_name === "string",
  );
  let next = { ...s, models };
  let selectedModel;
  let savedEffort;
  if (d.catalog === "preferences") {
    const preferred = s.preferences.models[d.id];
    const fromCatalog = typeof catalog.selected_model === "string" ? catalog.selected_model : "";
    selectedModel = typeof preferred === "string" && preferred ? preferred : fromCatalog;
    savedEffort = s.preferences.reasoning_effort;
  } else {
    selectedModel = typeof catalog.selected_model === "string" ? catalog.selected_model : "";
    savedEffort = catalog.reasoning_effort === "medium" ? "medium" : "high";
  }
  const options = [];
  const available = models.some((model) => model.id === selectedModel);
  if (!selectedModel) options.push(...placeholderOptions(models.length ? d.copy.choose : d.copy.noModels));
  else if (!available) options.push({ value: selectedModel, label: fill(d.copy.unavailableOption, { model: selectedModel }), disabled: true });
  for (const model of models) options.push({ value: model.id, label: model.display_name, disabled: false });
  next = { ...next, modelOptions: options, savedModel: selectedModel, selectedModel, savedEffort };
  next.selectedEffort = effortValue(d, next, selectedModel, savedEffort);
  if (selectedModel && !available) {
    next.modelStatus = { state: "error", text: fill(d.copy.unavailableSaved, { model: selectedModel }) };
  } else if (d.catalog === "preferences") {
    next.modelStatus = { state: "", text: selectedModel ? null : d.copy.chooseToSave };
  }
  return next;
}

function loadOk(d, s, meta, body) {
  if (meta.id !== s.loadId) return done(s);
  if (!isObject(body)) return loadFailed(d, s, GENERIC_ERROR);
  if (meta.op === "providers") {
    const info = (Array.isArray(body.providers) ? body.providers : []).find((item) => item && item.id === d.id);
    if (!info || (d.strictStatus && !KNOWN.includes(info.status))) {
      return loadFailed(d, s, { code: "INVALID_PROVIDER_STATUS", message: d.copy.invalidStatus });
    }
    const next = applyProvider(d, s, info);
    if (d.endpoints.preferences) return done(next, [call(d, "load", "preferences", d.endpoints.preferences, { id: meta.id })]);
    if (info.status === "connected") return done(next, [call(d, "load", "models", d.endpoints.models, { id: meta.id })]);
    return settle(d, clearModels(d, next));
  }
  if (meta.op === "preferences") {
    const preferences = normalizePreferences(body);
    const next = { ...s, preferences };
    if (s.info.status === "connected") return done(next, [call(d, "load", "models", d.endpoints.models, { id: meta.id })]);
    const selectedModel = preferences.models[d.id] || "";
    return settle(d, {
      ...next,
      models: [],
      selectedModel,
      savedModel: selectedModel,
      savedEffort: preferences.reasoning_effort,
      selectedEffort: "default",
      modelOptions: placeholderOptions(d.copy.connectToLoad),
      modelStatus: { state: "", text: null },
    });
  }
  return settle(d, renderModels(d, s, body)); // models
}

function loadFailed(d, s, error) {
  return fail(d, s, error, "load");
}

// A failed call that may mean the provider needs a new sign-in: re-read the status, then finish the operation.
function fail(d, s, error, after) {
  if (!needsReconnect(d, error)) return resume(d, s, error, after);
  const next = d.reconnect.local ? localReconnect(d, s, error) : s;
  return done(next, [call(d, `refresh-${after}`, "refresh", d.endpoints.providers, { extra: { after, error, loadId: s.loadId } })]);
}

function refreshed(d, s, meta, body) {
  if (meta.after === "load" && meta.loadId !== s.loadId) return done(s); // a newer load owns the page now
  let next = s;
  const info = isObject(body) && Array.isArray(body.providers) ? body.providers.find((item) => item && item.id === d.id) : null;
  if (info && (!d.strictStatus || KNOWN.includes(info.status))) {
    next = applyProvider(d, s, info);
    if (!d.strictStatus && info.status !== "connected") next = clearModels(d, next);
  }
  return resume(d, next, meta.error, meta.after);
}

function setPending(s, op, value) {
  return { ...s, pending: { ...s.pending, [op]: value } };
}

function resume(d, s, error, after) {
  if (after === "load") {
    if (error?.code === "provider_in_use" && s.info?.status === "connected") {
      return settle(d, showError(d, s, error));
    }
    let next = s;
    if (d.loadFailure === "keep-last") {
      if (s.info) next = applyProvider(d, s, s.info);
      else next = { ...s, launcher: { status: "error", account: "" }, badge: { state: "error", text: d.copy.unavailable }, statusText: d.copy.unreadable };
    } else if (!needsReconnect(d, error)) {
      next = { ...s, launcher: { status: "error", account: "" }, badge: { state: "error", text: d.copy.unavailable } };
    }
    return settle(d, showError(d, next, error));
  }
  if (after === "save") {
    const next = setPending(s, "save", false);
    return done(showError(d, d.copy.saveFailed ? { ...next, modelStatus: { state: "error", text: d.copy.saveFailed } } : next, error));
  }
  const next = setPending(s, "verify", false);
  return done(showError(d, { ...next, verifyResult: { state: "error", text: d.copy.verifyFailed } }, error));
}

// ---- login ----

function validateAuthorizationUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return { error: { code: "INVALID_AUTHORIZATION_URL", message: t("Máy chủ trả về địa chỉ đăng nhập không hợp lệ.") } };
  }
  if (url.protocol !== "https:" || url.hostname !== "auth.openai.com" || url.username || url.password) {
    return { error: { code: "INVALID_AUTHORIZATION_URL", message: t("Địa chỉ đăng nhập không thuộc dịch vụ ChatGPT chính thức.") } };
  }
  if (url.searchParams.has("id_token_hint")) {
    return { error: { code: "UNSAFE_AUTHORIZATION_URL", message: t("Máy chủ không được gửi thông tin nhận dạng nhạy cảm đến trình duyệt.") } };
  }
  return { url: url.href };
}

const loginExpired = (login, now) => {
  const expires = Date.parse(login.expiresAt);
  return Number.isFinite(expires) && now >= expires;
};

// The old setPendingLogin / setAntigravityLogin: remember the login and show what the session offers.
function applySession(d, s, session) {
  const login = { id: session.id, expiresAt: session.expires_at };
  let next = { ...s, login };
  const effects = [persist(d, login)];
  if (d.login.kind === "popup") return { state: { ...next, loginCopy: d.copy.pending }, effects };
  next = { ...next, loginCopy: typeof session.instructions === "string" && session.instructions ? session.instructions : d.copy.pending };
  if (typeof session.authorization_url === "string" && session.authorization_url) {
    let url = null;
    try {
      url = new URL(session.authorization_url);
    } catch {
      return { state: next, effects, error: { code: "INVALID_AUTHORIZATION_URL", message: d.copy.invalidUrl } };
    }
    const localHttp = url.protocol === "http:" && ["127.0.0.1", "localhost"].includes(url.hostname);
    if (!(url.protocol === "https:" || localHttp) || url.username || url.password) {
      return { state: next, effects, error: { code: "INVALID_AUTHORIZATION_URL", message: d.copy.unsafeUrl } };
    }
    next = { ...next, loginLink: url.href };
  }
  if (typeof session.input_prompt === "string" && session.input_prompt) next = { ...next, inputPrompt: session.input_prompt, inputOpen: true };
  return { state: next, effects };
}

function finishLogin(d, s, status, payload) {
  const gen = s.loginGen + 1;
  const recreating = s.recreatingRegistration;
  let next = {
    ...s,
    login: null,
    loginGen: gen,
    callbackRequest: s.callbackRequest + 1,
    callbackPending: false,
    recreatingRegistration: false,
    recreateConfirming: false,
    cancelling: false,
    signInDisabled: false,
    loginLink: null,
    inputOpen: false,
    ui: d.id === "chatgpt" ? "clear-callback-input:focus-message" : s.ui,
    pending: { ...s.pending, recreate: false },
  };
  const effects = [...stopPoll(d), persist(d, null)];
  if (d.login.clearOnFinish) next = { ...next, error: null };
  if (status === "connected") {
    next = { ...next, message: recreating ? d.copy.recreateConnected : d.copy.loginConnected };
    const load = startLoad(d, next);
    return done(load.state, [...effects, ...load.effects, CHOICE_CHANGED]);
  }
  if (recreating) {
    const message = status === "expired" ? d.copy.loginExpired : status === "failed" ? d.copy.loginFailed : d.copy.loginCancelled;
    const load = startLoad(d, { ...next, message });
    return done(load.state, [...effects, ...load.effects, CHOICE_CHANGED]);
  }
  if (status === "cancelled") return done({ ...next, message: d.copy.loginCancelled }, effects);
  if (status === "expired") {
    if (d.login.clearOnFinish) return done({ ...next, message: d.copy.loginExpired }, effects);
    return done(showError(d, next, { code: "provider_timeout" }), effects);
  }
  if (status === "failed") {
    if (d.login.clearOnFinish) {
      const error = payload && isObject(payload.error) ? payload.error : {};
      const shown = {
        code: typeof error.code === "string" ? error.code : "SIGN_IN_FAILED",
        message: typeof error.message === "string" ? error.message : d.copy.loginFailedError,
      };
      return done({ ...showError(d, next, shown), message: d.copy.loginFailed }, effects);
    }
    return done(showError(d, next, (payload && payload.error) || { code: "provider_unavailable" }), effects);
  }
  return done(next, effects);
}

function startLogin(d, s) {
  if (s.signInDisabled || s.login) return done(s); // a second click while the first is still starting
  const next = { ...s, error: null, signInDisabled: true, ui: "clear-callback-input", ...(d.clearMessages ? { message: null } : {}) };
  if (d.login.kind === "popup") {
    return done(next, [windowEffect(d, "reserve", { title: d.copy.windowTitle, text: d.copy.windowText, ok: "provider/window-reserved", error: "provider/window-blocked" })]);
  }
  return done(next, [call(d, "login-start", "login-start", d.endpoints.loginSessions, { method: "POST", body: {} })]);
}

function loginStartFailed(d, s, error, recreation = false) {
  const next = recreation
    ? { ...setPending(showError(d, s, error), "recreate", false), recreateConfirming: true }
    : { ...showError(d, s, error), signInDisabled: false };
  return done(
    { ...next, signInDisabled: false },
    d.login.kind === "popup" ? [windowEffect(d, "close")] : [],
  );
}

function loginStarted(d, s, session, now, recreation = false) {
  if (d.login.kind === "code") {
    if (!isObject(session)) return loginStartFailed(d, s, { code: "INVALID_LOGIN_SESSION", message: REQUEST_ERRORS.INVALID_LOGIN_SESSION }, recreation);
    const applied = applySession(d, s, session);
    const next = { ...applied.state, signInDisabled: false };
    if (applied.error) return loginStartFailed(d, next, applied.error, recreation);
    return done(next, [...applied.effects, call(d, "poll", "poll", loginUrl(d, session.id), { extra: { gen: s.loginGen } })]);
  }
  if (!session || session.status !== "pending" || !session.id || !session.expires_at) {
    return loginStartFailed(d, s, { code: "INVALID_LOGIN_SESSION", message: REQUEST_ERRORS.INVALID_LOGIN_SESSION }, recreation);
  }
  const checked = validateAuthorizationUrl(session.authorization_url);
  if (checked.error) return loginStartFailed(d, s, checked.error, recreation);
  const applied = applySession(d, s, session);
  let next = {
    ...applied.state,
    loginGen: s.loginGen + 1,
    recreatingRegistration: recreation,
    recreateConfirming: false,
    pending: { ...s.pending, recreate: false },
    signInDisabled: false,
  };
  if (recreation) {
    next = applyProvider(d, clearModels(d, next), { ...(next.info || {}), id: d.id, status: "disconnected", account: null, verification: null });
    next = { ...next, message: d.copy.recreateStarted };
  }
  else if (session.replaced_previous === true) next = { ...next, message: d.copy.replaced };
  if (loginExpired(next.login, now)) {
    const finished = finishLogin(d, next, "expired");
    return done(finished.state, [...applied.effects, windowEffect(d, "close"), ...finished.effects]);
  }
  return done(next, [
    ...applied.effects,
    ...(recreation ? [CHOICE_CHANGED] : []),
    windowEffect(d, "navigate", { url: checked.url, closed: "provider/window-closed" }),
    pollWait(d, d.login.firstPoll, next.loginGen),
  ]);
}

function pollDue(d, s, gen, now) {
  if (gen !== s.loginGen || !s.login) return done(s);
  if (d.login.clientExpiry && loginExpired(s.login, now)) return finishLogin(d, s, "expired");
  return done(s, [call(d, "poll", "poll", loginUrl(d, s.login.id), { extra: { gen } })]);
}

function pollOk(d, s, meta, session, now) {
  if (meta.gen !== s.loginGen || !s.login) return done(s);
  const status = isObject(session) ? session.status : undefined;
  if (status === "pending") {
    if (d.login.clientExpiry) {
      if (loginExpired(s.login, now)) return finishLogin(d, s, "expired");
      return done({ ...s, loginCopy: d.copy.pending }, [pollWait(d, d.login.poll, meta.gen)]);
    }
    const applied = applySession(d, s, session);
    if (applied.error) return pollFailed(d, applied.state, meta, applied.error, applied.effects);
    return done(applied.state, [...applied.effects, pollWait(d, d.login.poll, meta.gen)]);
  }
  if (SESSION_DONE.includes(status)) return finishLogin(d, s, status, d.login.clearOnFinish ? session : { error: session.error });
  return pollFailed(d, s, meta, { code: "INVALID_LOGIN_STATUS", message: REQUEST_ERRORS.INVALID_LOGIN_STATUS });
}

const loginMissing = (error) => Boolean(error && (error.code === "login_session_not_found" || error.status === 404));

function pollFailed(d, s, meta, error, effects = []) {
  if (meta.gen !== s.loginGen || !s.login) return done(s);
  if (d.login.pollErrors === "retry") return done({ ...showError(d, s, error, true), loginCopy: d.copy.pollFailed }, effects);
  if (loginMissing(error)) return finishLogin(d, s, "expired");
  return done(showError(d, s, error), [...effects, pollWait(d, d.login.pollError, meta.gen)]);
}

function cancelLogin(d, s) {
  if (!s.login) return done(s);
  const gen = s.loginGen + 1;
  const next = { ...s, loginGen: gen, cancelling: d.login.cancelDisables, ...(d.login.clearOnFinish ? { error: null } : {}), ...(d.id === "chatgpt" ? { callbackPending: false, callbackRequest: s.callbackRequest + 1 } : {}) };
  const effects = d.id === "chatgpt" ? [{ type: "cancel", key: `${d.id}/callback` }] : [];
  return done(next, [...stopPoll(d), ...effects, call(d, "login-cancel", "cancel", loginUrl(d, s.login.id), { method: "DELETE", extra: { gen } })]);
}

function cancelOk(d, s, meta, session) {
  if (meta.gen !== s.loginGen) return done(s);
  const status = isObject(session) ? session.status : undefined;
  if (SESSION_DONE.includes(status) || !d.login.cancelStrict) return finishLogin(d, s, status, session);
  return cancelFailed(d, s, meta, { code: "CANCEL_NOT_CONFIRMED", message: REQUEST_ERRORS.CANCEL_NOT_CONFIRMED });
}

function cancelFailed(d, s, meta, error) {
  if (meta.gen !== s.loginGen) return done(s);
  if (d.login.pollErrors === "reschedule" && loginMissing(error)) return finishLogin(d, s, "expired");
  return done(showError(d, { ...s, cancelling: false }, error), [pollWait(d, d.login.poll, meta.gen)]);
}

function submitCallback(d, s, value) {
  const text = typeof value === "string" ? value.trim() : "";
  if (d.id !== "chatgpt" || !s.login || s.cancelling || s.callbackPending || !text) return done(s);
  const request = s.callbackRequest + 1;
  const next = {
    ...s,
    callbackRequest: request,
    callbackPending: true,
    loginCopy: d.copy.callbackSubmitting,
    error: null,
    ui: "clear-callback-input",
  };
  return done(next, [
    call(d, "callback", "callback", loginUrl(d, s.login.id, "/input"), {
      method: "POST",
      body: { callback_url: text },
      extra: { gen: s.loginGen, callback: request },
    }),
  ]);
}

function callbackOk(d, s, meta, session, now) {
  if (meta.gen !== s.loginGen || meta.callback !== s.callbackRequest || !s.login) return done(s);
  const status = isObject(session) ? session.status : undefined;
  const next = { ...s, callbackPending: false, loginCopy: d.copy.callbackSubmitted };
  if (SESSION_DONE.includes(status)) return finishLogin(d, next, status, session);
  if (status === "pending") return done(next, [pollWait(d, d.login.poll, meta.gen)]);
  return callbackFailed(d, next, meta, { code: "INVALID_LOGIN_STATUS", message: REQUEST_ERRORS.INVALID_LOGIN_STATUS });
}

function callbackFailed(d, s, meta, error) {
  if (meta.gen !== s.loginGen || meta.callback !== s.callbackRequest || !s.login) return done(s);
  return done({ ...showError(d, { ...s, callbackPending: false, loginCopy: d.copy.callbackPending, ui: "focus:callback-input" }, error) });
}

function recreateRegistration(d, s, displayName) {
  if (
    d.id !== "chatgpt"
    || !s.info
    || !["connected", "reconnect_required"].includes(s.info.status)
    || !s.recreateConfirming
    || s.pending.recreate
  ) return done(s);
  const name = typeof displayName === "string" ? displayName.trim() : "";
  const next = setPending({ ...s, recreateName: name, error: null, message: null }, "recreate", true);
  return done(next, [
    windowEffect(d, "reserve", {
      title: d.copy.windowTitle,
      text: d.copy.windowText,
      ok: "provider/recreate-window-reserved",
      error: "provider/recreate-window-blocked",
    }),
  ]);
}

function submitInput(d, s, value) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!s.login || !text) return done(s);
  return done(s, [call(d, "login-input", "input", loginUrl(d, s.login.id, "/input"), { method: "POST", body: { value: text } })]);
}

function recreateWindowBlocked(d, s) {
  if (d.id !== "chatgpt" || !s.pending.recreate) return done(s);
  return done({
    ...showError(d, setPending(s, "recreate", false), { code: "POPUP_BLOCKED" }),
    recreateConfirming: true,
  });
}

function inputOk(d, s, session) {
  const cleared = { ...s, inputOpen: false, ui: "clear-input" };
  if (!isObject(session)) return done(showError(d, cleared, GENERIC_ERROR));
  const applied = applySession(d, cleared, session);
  if (applied.error) return done(showError(d, applied.state, applied.error), applied.effects);
  return done(applied.state, applied.effects);
}

function restore(d, s, raw, now) {
  let saved;
  try {
    saved = JSON.parse(raw);
  } catch {
    return done(s, [persist(d, null)]);
  }
  if (!saved || typeof saved.id !== "string" || typeof saved.expiresAt !== "string") return done(s);
  if (d.login.restoreExpired === "poll") {
    if (!saved.id || !Number.isFinite(Date.parse(saved.expiresAt))) return done(s);
    const applied = applySession(d, s, { id: saved.id, expires_at: saved.expiresAt });
    return done(applied.state, [...applied.effects, pollWait(d, 0, s.loginGen)]);
  }
  const expires = Date.parse(saved.expiresAt);
  if (!(Number.isFinite(expires) && expires > now)) return done(s, [persist(d, null)]);
  return done({ ...s, login: { id: saved.id, expiresAt: saved.expiresAt } }, [call(d, "poll", "poll", loginUrl(d, saved.id), { extra: { gen: s.loginGen } })]);
}

// ---- model choice, save, verify, disconnect ----

const busy = (d, s, op) => (d.exclusive ? Object.values(s.pending).some(Boolean) : s.pending[op]);
const providerInUse = (s) => Boolean(s.info && s.info.in_use === true);

function editChoice(d, s, field, value) {
  if (providerInUse(s)) return done(s);
  if (d.catalog === "preferences") {
    if (field === "model") {
      const effort = value === s.savedModel ? s.savedEffort : "default";
      const next = { ...s, selectedModel: value, selectedEffort: effort };
      next.selectedEffort = effortValue(d, next, value, effort);
      return done(value === s.savedModel ? next : { ...next, modelStatus: { state: "", text: d.copy.unsavedModel } });
    }
    return done({ ...s, selectedEffort: value, modelStatus: { state: "", text: d.copy.unsavedEffort } });
  }
  const next = field === "model" ? { ...s, selectedModel: value } : { ...s, selectedEffort: value };
  const same = next.selectedModel === next.savedModel && next.selectedEffort === next.savedEffort;
  return done({ ...next, modelStatus: { state: "", text: same ? null : d.copy.unsavedModel } });
}

function saveChoice(d, s) {
  if (providerInUse(s)) return done(s);
  const model = s.selectedModel;
  const effort = effortValue(d, s, model, s.selectedEffort);
  if (d.catalog === "preferences" ? !modelAvailable(s, model) || busy(d, s, "save") : !model || busy(d, s, "save")) return done(s);
  const next = setPending({ ...s, ...(d.catalog === "preferences" ? { error: null, modelStatus: { state: "", text: d.copy.saving } } : {}) }, "save", true);
  const request =
    d.catalog === "preferences"
      ? call(d, "save", "save", d.endpoints.preferences, { method: "PUT", body: { provider: d.id, model, reasoning_effort: effort }, extra: { model, effort } })
      : call(d, "save", "save", d.endpoints.preference, { method: "PUT", body: { model, reasoning_effort: effort }, extra: { model, effort } });
  return done(next, [request]);
}

function saveOk(d, s, meta, body) {
  const { model, effort } = meta;
  let next = setPending(s, "save", false);
  if (d.catalog === "preferences") {
    const reply = isObject(body) ? body : {};
    const preferences = {
      provider: d.id,
      models: isObject(reply.models) ? reply.models : { [d.id]: model },
      reasoning_effort: typeof reply.reasoning_effort === "string" ? reply.reasoning_effort : effort,
    };
    const savedModel = preferences.models[d.id] || model;
    next = { ...next, preferences, savedModel, selectedModel: savedModel, savedEffort: preferences.reasoning_effort };
    next.selectedEffort = effortValue(d, next, savedModel, preferences.reasoning_effort);
  } else {
    next = { ...next, savedModel: model, savedEffort: effort };
  }
  const label = d.effortLabels ? d.effortLabels[next.savedEffort] ?? next.savedEffort : next.savedEffort;
  next = { ...next, modelStatus: { state: "saved", text: fill(d.copy.saved, { model: modelName(next, next.selectedModel), effort: label }) } };
  return done(next, [CHOICE_CHANGED]);
}

function verifyConnection(d, s) {
  if (providerInUse(s)) return done(s);
  const model = s.selectedModel;
  const effort = effortValue(d, s, model, s.selectedEffort);
  if (d.verifyWithEffort ? !modelAvailable(s, model) || busy(d, s, "verify") : !model || busy(d, s, "verify")) return done(s);
  const next = setPending({ ...s, ...(d.verifyWithEffort ? { error: null } : {}), verifyResult: { state: "", text: d.copy.verifying } }, "verify", true);
  const body = d.verifyWithEffort ? { model, reasoning_effort: effort } : { model };
  return done(next, [call(d, "verify", "verify", d.endpoints.verify, { method: "POST", body, extra: { model, effort } })]);
}

function verifyOk(d, s, meta, result) {
  const reply = isObject(result) ? result : {};
  let next = setPending(s, "verify", false);
  if (d.verifyWithEffort) {
    const effort = reply.reasoning_effort || meta.effort;
    next = {
      ...next,
      verifyResult: { state: "success", text: fill(d.copy.verified, { model: modelName(next, reply.model), effort, time: timestamp(reply.verified_at) }) },
    };
    if (next.info) next = applyProvider(d, next, { ...next.info, verification: { model: reply.model, reasoning_effort: effort, verified_at: reply.verified_at } });
    return done(next);
  }
  next = { ...next, verifyResult: { state: "success", text: fill(d.copy.verified, { model: modelName(next, meta.model), time: timestamp(reply.verified_at) }) } };
  if (next.info) next = applyProvider(d, next, { ...next.info, verification: reply });
  return done(next);
}

function disconnect(d, s) {
  if (providerInUse(s) || (d.confirmDisconnect && !s.confirming) || busy(d, s, "disconnect")) return done(s);
  const next = setPending({ ...s, ...(d.confirmDisconnect ? { error: null } : {}) }, "disconnect", true);
  return done(next, [call(d, "disconnect", "disconnect", d.endpoints.connection, { method: "DELETE" })]);
}

function disconnectOk(d, s, result) {
  if (d.confirmDisconnect && !(isObject(result) && result.status === "disconnected")) {
    return done(showError(d, setPending(s, "disconnect", false), { code: "DISCONNECT_NOT_CONFIRMED", message: REQUEST_ERRORS.DISCONNECT_NOT_CONFIRMED }));
  }
  if (!d.confirmDisconnect) {
    const load = startLoad(d, { ...s, message: d.copy.disconnectedLocal, afterLoad: "disconnected" });
    return load;
  }
  const info = { ...(s.info || {}), id: d.id, status: "disconnected", account: null, verification: null };
  const next = applyProvider(
    d,
    {
      ...setPending(s, "disconnect", false),
      models: [],
      modelOptions: placeholderOptions(d.copy.connectToLoad),
      selectedModel: "",
      verifyResult: { state: "", text: null },
      confirming: false,
      ui: "focus:sign-in",
      message: d.id === "antigravity"
        ? d.copy.disconnectedLocal
        : result.remote_revocation_confirmed === true ? d.copy.disconnectedRemote : d.copy.disconnectedLocal,
    },
    info,
  );
  return done(next, [CHOICE_CHANGED]);
}

// ---- header status read on page load ----

function headerLoaded(d, s, body) {
  const providers = isObject(body) && Array.isArray(body.providers) ? body.providers : [];
  const entry = providers.find((item) => item && item.id === d.id);
  const known = Boolean(entry) && KNOWN.includes(entry.status);
  if (d.loadFailure === "keep-last") {
    // A dialog or login read that started meanwhile is newer than this slower page-load answer.
    if (s.headerGen !== s.loadId) return done(s);
    return done(known ? applyProvider(d, s, entry) : { ...s, launcher: { status: "error", account: "" } });
  }
  if (s.launcher) return done(s); // a status already shown by the dialog read is newer
  return done({ ...s, launcher: launcherOf(known ? entry.status : "error", entry?.account?.label) });
}

// ---- reducers ----

function reduceProvider(d, state, action) {
  const meta = action.meta;
  switch (action.type) {
    case "provider/header-requested":
      return done({ ...state, headerGen: state.loadId });
    case "provider/dialog-opened": {
      const next = d.clearMessages ? { ...state, message: null } : state;
      return startLoad(d, next);
    }
    case "provider/dialog-closed":
      return d.id === "chatgpt" ? done({ ...state, ui: "clear-callback-input" }) : done(state);
    case "provider/response":
    case "provider/request-failed": {
      if (meta.op === "header") return headerLoaded(d, state, action.type === "provider/response" ? action.response.body : null);
      if (meta.provider !== d.id) return done(state);
      return action.type === "provider/response" ? onResponse(d, state, meta, action.response.body, action.now) : onFailure(d, state, meta, requestError(action.error, d.id), action.now);
    }
    default:
      break;
  }
  if ((action.provider ?? meta?.provider) !== d.id) return done(state);
  switch (action.type) {
    case "provider/boot-restored":
      return restore(d, state, action.raw, action.now);
    case "provider/login-requested":
      return startLogin(d, state);
    case "provider/window-reserved":
      return done(state, [call(d, "login-start", "login-start", d.endpoints.loginSessions, { method: "POST", body: {}, extra: { recreation: state.pending.recreate } })]);
    case "provider/window-blocked":
      return done({ ...showError(d, state, { code: "POPUP_BLOCKED" }), signInDisabled: false });
    case "provider/recreate-window-reserved":
      if (d.id !== "chatgpt" || !state.pending.recreate) return done(state);
      return done(state, [call(d, "registration-recreate", "registration-recreate", d.endpoints.registrationRecreate, { method: "POST", body: { display_name: state.recreateName }, extra: { recreation: true } })]);
    case "provider/recreate-window-blocked":
      return recreateWindowBlocked(d, state);
    case "provider/window-closed":
      return done(showError(d, state, { code: "POPUP_BLOCKED", message: d.copy.windowClosed }));
    case "provider/poll-due":
      return pollDue(d, state, action.gen, action.now);
    case "provider/poll-retry-requested":
      return done({ ...state, error: null }, state.login ? [pollWait(d, 0, state.loginGen)] : []);
    case "provider/login-cancel-requested":
      return cancelLogin(d, state);
    case "provider/login-input-submitted":
      return submitInput(d, state, action.value);
    case "provider/login-callback-submitted":
      return submitCallback(d, state, action.value);
    case "provider/registration-recreate-confirm-toggled":
      if (
        d.id !== "chatgpt"
        || !state.info
        || !["connected", "reconnect_required"].includes(state.info.status)
        || state.login
        || state.pending.recreate
      ) return done(state);
      return done({
        ...state,
        recreateConfirming: action.open === true,
        recreateName: action.open === true ? "ASR Studio" : state.recreateName,
        error: null,
        ui: action.open === true ? "focus:registration-name" : "focus:registration-trigger",
      });
    case "provider/registration-recreate-requested":
      return recreateRegistration(d, state, action.displayName);
    case "provider/choice-edited":
      return editChoice(d, state, action.field, action.value);
    case "provider/save-requested":
      return saveChoice(d, state);
    case "provider/verify-requested":
      return verifyConnection(d, state);
    case "provider/disconnect-confirm-toggled":
      if (providerInUse(state)) return done(state);
      return done({ ...state, confirming: action.open, ui: action.open ? "focus:disconnect-confirm" : "focus:disconnect-trigger" });
    case "provider/disconnect-requested":
      return disconnect(d, state);
    case "provider/ui-handled":
      return done({ ...state, ui: null });
    default:
      return done(state);
  }
}

function onResponse(d, s, meta, body, now) {
  switch (meta.op) {
    case "providers":
    case "preferences":
    case "models":
      return loadOk(d, s, meta, body);
    case "refresh":
      return refreshed(d, s, meta, body);
    case "login-start":
      return loginStarted(d, s, body, now, meta.recreation === true);
    case "registration-recreate":
      return loginStarted(d, s, body, now, true);
    case "poll":
      return pollOk(d, s, meta, body, now);
    case "cancel":
      return cancelOk(d, s, meta, body);
    case "input":
      return inputOk(d, s, body);
    case "callback":
      return callbackOk(d, s, meta, body, now);
    case "save":
      return saveOk(d, s, meta, body);
    case "verify":
      return verifyOk(d, s, meta, body);
    default:
      return disconnectOk(d, s, body);
  }
}

function onFailure(d, s, meta, error) {
  switch (meta.op) {
    case "providers":
    case "preferences":
    case "models":
      return meta.id === s.loadId ? loadFailed(d, s, error) : done(s);
    case "refresh":
      return refreshed(d, s, meta, null);
    case "login-start":
      return loginStartFailed(d, s, error, meta.recreation === true);
    case "registration-recreate":
      return loginStartFailed(d, s, error, true);
    case "poll":
      return pollFailed(d, s, meta, error);
    case "cancel":
      return cancelFailed(d, s, meta, error);
    case "input":
      return done(showError(d, s, error));
    case "callback":
      return callbackFailed(d, s, meta, error);
    case "save":
      return fail(d, s, error, "save");
    case "verify":
      return fail(d, s, error, "verify");
    default:
      return done(showError(d, setPending(s, "disconnect", false), error));
  }
}

// The sections of the settings dialog, and the session key a language change leaves so the reloaded page
// reopens at its section. i18n.js writes that key: it is a plain script and cannot import this module.
export const SETTINGS_SECTIONS = ["ai", "asr", "theme", "about"];
export const SETTINGS_SECTION_KEY = "asr-studio:settings-section";

// The launcher slice: the page-load status read, the one-shot scroll to the Google section, and the open section.
function reduceLauncher(state, action) {
  if (action.type === "provider/header-requested") {
    return { state, effects: [{ type: "fetch", key: "provider/header", url: CHATGPT.endpoints.providers, init: { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" }, ok: "provider/response", error: "provider/request-failed", meta: { op: "header" } }] };
  }
  if (action.type === "provider/dialog-opened") {
    const reopened = action.section ? [{ type: "storage", key: SETTINGS_SECTION_KEY, area: "session", op: "remove" }] : [];
    return done({ section: action.section ?? "ai", scroll: action.launcher === "antigravity" }, reopened);
  }
  if (action.type === "provider/settings-section-selected") return done({ section: action.section, scroll: false });
  if (action.type === "provider/scroll-handled") return done({ ...state, scroll: false });
  return done(state);
}

// The ASR slice: the model catalog the settings dialog shows, read each time the dialog opens.
// The newest read wins; a reply of an older read is dropped by its id.
// An install runs in a separate service: the page asks (POST), then reads the catalog every second while the
// dialog is open and a job is active. The reducer only holds what the server last said, plus the one optimistic
// step (queued, cancelling) that keeps a click from looking ignored.
const GET = { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" };
const asrFetch = (request) => ({ type: "fetch", key: "provider/asr-models", url: "/api/asr-models", init: GET, ok: "provider/asr-loaded", error: "provider/asr-failed", meta: { id: request } });
// The meeting behind the dialog reads its model list again. Closing can beat a save, so a save says so too.
const ASR_SETTINGS_CHANGED = { type: "call", key: "provider/asr-settings-changed", name: "asr.settings-changed", args: [] };
const ASR_POLL = { type: "schedule", key: "provider/asr-poll", delay: 1000, action: { type: "provider/asr-poll" } };
const JOB_ACTIVE = ["queued", "running", "cancelling"];
const jobActive = (choice) => JOB_ACTIVE.includes(choice.install?.state);
const installerUsable = (installer) => installer.available && installer.models_writable;
const asrPolling = (state) => state.open && state.installer.available && state.models.some(jobActive);
const withPoll = (state) => done(state, asrPolling(state) ? [ASR_POLL] : []);
const withInstall = (state, id, install) => ({ ...state, models: state.models.map((choice) => (choice.id === id ? { ...choice, install } : choice)) });

function asrInstallCall(state, action, method, optimistic) {
  const choice = state.models.find((item) => item.id === action.id && item.id !== "auto");
  // A second click on a job that is already going asks for nothing; Cancel is the only click that does.
  if (!choice || (method === "POST" && jobActive(choice))) return done(state);
  // Cancel waits until the server has the job: before that there is nothing to cancel and the job would still start.
  if (method === "DELETE" && (choice.install?.sending || choice.install?.state === "cancelling")) return done(state);
  const init = { method, headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" };
  // A new request id drops a catalog read that was already in flight: its answer predates this click.
  // Only the newest create or cancel of a model may answer: a late reply must not undo a later click.
  const mutation = (state.mutation ?? 0) + 1;
  const next = withInstall({ ...state, request: state.request + 1, notice: null, mutation, latest: { ...state.latest, [action.id]: mutation } }, action.id, optimistic);
  return done(next, [{ type: "fetch", key: `provider/asr-install/${method}/${action.id}`, url: `/api/asr-models/${action.id}/install`, init, ok: "provider/asr-install-sent", error: "provider/asr-install-refused", meta: { id: action.id, mutation } }]);
}

function reduceAsr(state, action) {
  switch (action.type) {
    case "provider/dialog-opened": {
      const request = state.request + 1;
      return done({ ...state, status: "loading", request, copy: null, notice: null, defaultRefusal: null, open: true }, [asrFetch(request)]);
    }
    case "provider/dialog-closed":
      return done({ ...state, open: false }, [{ type: "cancel", key: "provider/asr-poll" }, ASR_SETTINGS_CHANGED]);
    case "provider/asr-poll": {
      if (!state.open) return done(state);
      const request = state.request + 1;
      return done({ ...state, request }, [asrFetch(request)]);
    }
    case "provider/asr-loaded": {
      if (action.meta.id !== state.request) return done(state);
      const list = action.response.body?.asr_models;
      if (!Array.isArray(list)) return done({ ...state, status: "error" });
      const models = list.filter((choice) => isObject(choice) && (choice.id === "auto" || Object.hasOwn(ASR_MODEL_TEXT, choice.id)));
      const installer = action.response.body?.installer;
      const defaultModel = typeof action.response.body?.default_model === "string" ? action.response.body.default_model : null;
      const hardwareModel = typeof action.response.body?.hardware_model === "string" ? action.response.body.hardware_model : null;
      const next = { ...state, models, defaultModel, defaultDamaged: action.response.body?.default_damaged === true, hardwareModel, installer: { available: installer?.available === true, models_writable: installer?.models_writable === true },
        // A reply with no usable model must say so; an empty list would leave the section blank.
        status: models.some((choice) => choice.id !== "auto") ? "ready" : "empty" };
      return withPoll(next);
    }
    case "provider/asr-failed":
      // A failed poll keeps the rows: the next tick reads again.
      if (action.meta.id !== state.request) return done(state);
      return state.status === "ready" ? withPoll(state) : done({ ...state, status: "error" });
    // The new value shows at once. A catalog read already in flight predates it and is dropped.
    case "provider/asr-default-chosen": {
      const value = action.value === "auto" ? null : action.value;
      const defaultMutation = (state.defaultMutation ?? 0) + 1;
      const init = { method: "PUT", headers: { Accept: "application/json", "Content-Type": "application/json" }, credentials: "same-origin", cache: "no-store", body: JSON.stringify({ asr_model: value }) };
      return done({ ...state, request: state.request + 1, defaultMutation, defaultPending: value, defaultRefusal: null }, [
        { type: "fetch", key: "provider/asr-default", url: "/api/asr-models/default", init, ok: "provider/asr-default-saved", error: "provider/asr-default-refused", meta: { mutation: defaultMutation } },
      ]);
    }
    case "provider/asr-default-saved":
    case "provider/asr-default-refused": {
      if (action.meta.mutation !== state.defaultMutation) return done(state);
      const refused = action.type === "provider/asr-default-refused";
      const request = state.request + 1;
      const value = refused ? state.defaultModel : state.defaultPending;
      return done({ ...state, request, defaultModel: value, defaultPending: undefined, defaultRefusal: refused ? action.error?.body?.detail?.code ?? "request_failed" : null }, [asrFetch(request), ...(refused ? [] : [ASR_SETTINGS_CHANGED])]);
    }
    case "provider/asr-install-requested":
      return asrInstallCall(state, action, "POST", { state: "queued", sending: true });
    case "provider/asr-cancel-requested":
      return asrInstallCall(state, action, "DELETE", { state: "cancelling" });
    case "provider/asr-install-sent": {
      if (action.meta.mutation !== state.latest?.[action.meta.id]) return done(state);
      const install = isObject(action.response.body?.install) ? action.response.body.install : { state: "queued" };
      return withPoll(withInstall(state, action.meta.id, install));
    }
    // The server said no (no installer, no GPU, already prepared, nothing to cancel): say why, then read the truth.
    case "provider/asr-install-refused": {
      if (action.meta.mutation !== state.latest?.[action.meta.id]) return done(state);
      const code = action.error?.body?.detail?.code ?? "request_failed";
      const request = state.request + 1;
      return done({ ...state, request, notice: { id: action.meta.id, code } }, [asrFetch(request)]);
    }
    // The copy button: the clipboard is a call effect; the feedback line is state. A refused clipboard selects the command.
    case "provider/asr-copy-requested": {
      const command = selectAsr(state).rows.find((row) => row.id === action.id)?.command;
      if (!command) return done(state);
      return done({ ...state, copy: null }, [{ type: "call", key: "provider/asr-copy", name: "clipboard.write", args: [command], ok: "provider/asr-copy-succeeded", error: "provider/asr-copy-failed", meta: { id: action.id, request: state.request } }]);
    }
    case "provider/asr-copy-succeeded":
      return action.meta.request === state.request ? done({ ...state, copy: { id: action.meta.id, how: "copied" } }) : done(state);
    case "provider/asr-copy-failed":
      if (action.meta.request !== state.request) return done(state);
      return done({ ...state, copy: { id: action.meta.id, how: "selected" } }, [{ type: "call", key: "provider/asr-select", name: "asr.select-command", args: [action.meta.id] }]);
    default:
      return done(state);
  }
}

// One store for every provider; every reducer sees every action.
export const reduce = combine({
  chatgpt: (state, action) => reduceProvider(CHATGPT, state, action),
  antigravity: (state, action) => reduceProvider(ANTIGRAVITY, state, action),
  launcher: reduceLauncher,
  asr: reduceAsr,
});

// ---- selectors ----

export function selectLauncher(d, s) {
  const launcher = s.launcher;
  if (!launcher) return null;
  const [label, spoken] = LAUNCHER_COPY[launcher.status] || LAUNCHER_COPY.error;
  const account = launcher.status === "connected" ? t(" với {account}", { account: launcher.account || t("tài khoản") }) : "";
  return {
    status: launcher.status,
    ariaLabel: t("Kết nối {name}: {state}", { name: d.launcherName, state: [spoken, account] }),
    title: [d.launcherName, ": ", label],
  };
}

// What the settings dialog shows for the ASR catalog: the three models with their real readiness, and the default.
// The default is what "auto" resolves to; it is marked on the model whose directory it is.
const ASR_STATUS = {
  ready: t("Sẵn sàng"),
  not_prepared: t("Chưa chuẩn bị"),
  installing: t("Đang cài đặt"),
  gpu_required: t("Cần GPU NVIDIA"),
  worker_unavailable: t("Chưa kiểm tra được"),
};

const ASR_COPY = {
  copied: t("Đã sao chép lệnh."),
  selected: t("Không sao chép được. Lệnh đã được chọn; nhấn Ctrl+C."),
};

const DEFAULT_REFUSALS = {
  not_prepared: t("Model này chưa được cài, nên chưa đặt làm mặc định được."),
  gpu_required: t("Model này cần GPU NVIDIA, nên không đặt làm mặc định được trên máy này."),
  worker_unavailable: t("Worker chưa sẵn sàng nên chưa đổi được model mặc định."),
};

// The control above the list: the hardware's default, or one model that can run here now.
function defaultControl(s, auto, models, usable) {
  const chosen = s.defaultPending !== undefined ? s.defaultPending : s.defaultModel ?? null;
  const hardware = s.hardwareModel;
  const label = (row) => (row.ready ? ASR_MODEL_TEXT[row.id].label : t("{model} · chưa sẵn sàng", { model: ASR_MODEL_TEXT[row.id].label }));
  const options = [
    { value: "auto", label: hardware ? t("Tự động theo phần cứng ({model})", { model: hardware }) : t("Tự động theo phần cứng") },
    ...models.map((row) => ({ value: row.id, label: label(row), disabled: !row.ready })),
  ];
  const name = chosen && ASR_MODEL_TEXT[chosen]?.label;
  const broken = Boolean(chosen && auto && !auto.ready && s.defaultPending === undefined);
  const damaged = Boolean(s.defaultDamaged && !chosen && usable);
  return {
    value: chosen ?? "auto",
    options,
    disabled: !usable,
    refusal: s.defaultRefusal ? DEFAULT_REFUSALS[s.defaultRefusal] ?? t("Chưa đổi được model mặc định. Hãy thử lại.") : null,
    text: damaged
      ? t("Không đọc được model mặc định đã lưu, nên đang dùng theo phần cứng của máy. Hãy chọn lại.")
      : broken
      ? t("Model mặc định {model} chưa chạy được trên máy này. Lần chép lời Tự động sẽ bị từ chối cho đến khi bạn cài lại hoặc chọn mặc định khác.", { model: name })
      : chosen
        ? t("Lần chép lời Tự động dùng {model}. Mỗi lần chạy vẫn chọn được model khác.", { model: name })
        : hardware
          ? t("Mặc định theo phần cứng của máy: {model}.", { model: hardware })
          : t("Mặc định theo phần cứng của máy."),
    broken: broken || damaged,
  };
}

const INSTALL_GUIDANCE = t("Tải model công khai về máy này. Bản ghi không rời khỏi máy.");
const INSTALL_PHASES = {
  queued: t("Đang chờ cài đặt…"),
  downloading: t("Đang tải model…"),
  converting: t("Đang chuyển đổi model…"),
  verifying: t("Đang kiểm tra file model…"),
  cancelling: t("Đang hủy…"),
};
// What a failed or refused install says, in plain words and with the way out.
const INSTALL_ERRORS = {
  insufficient_disk: (space) => (space ? t("Không đủ dung lượng: cần {required}, còn {free}. Giải phóng chỗ trống rồi thử lại.", space) : t("Không đủ dung lượng. Giải phóng chỗ trống rồi thử lại.")),
  network: () => t("Mất kết nối khi tải model. Kiểm tra mạng của máy rồi thử lại."),
  disk_full: () => t("Ổ đĩa đầy giữa chừng. Giải phóng chỗ trống rồi thử lại."),
  integrity: () => t("File tải về không hợp lệ. Thử lại để tải lại."),
  target_not_empty: () => t("Thư mục model đã có file khác nên không ghi đè. Chuyển file đó đi rồi thử lại."),
  models_not_writable: () => t("Dịch vụ cài đặt không ghi được vào thư mục model. Kiểm tra quyền của thư mục."),
  interrupted: () => t("Quá trình cài đặt bị gián đoạn. Thử lại."),
};
const INSTALL_REFUSALS = {
  gpu_required: t("Model này cần GPU NVIDIA nên không cài được ở đây."),
  model_ready: t("Model này đã sẵn sàng."),
  installer_unavailable: t("Dịch vụ cài model chưa chạy. Dùng lệnh chuẩn bị model."),
  models_not_writable: t("Dịch vụ cài đặt không ghi được vào thư mục model. Kiểm tra quyền của thư mục."),
  worker_unavailable: ASR_MODEL_GUIDANCE.worker_unavailable,
  install_not_active: t("Không còn lượt cài nào để hủy."),
  request_failed: t("Không gửi được yêu cầu. Thử lại."),
};

const finite = (value) => (Number.isFinite(value) && value >= 0 ? value : null);

// The install block of one unprepared model: the offer, the running job, or the failure with its retry.
function installView(choice, notice) {
  const job = isObject(choice.install) ? choice.install : { state: "idle" };
  const required = finite(choice.required_bytes);
  const free = finite(choice.free_bytes);
  const space = required === null ? null : free === null ? { required: bytes(required) } : { required: bytes(required), free: bytes(free) };
  const spaceLine = space ? (space.free ? t("Cần {required} trống, còn {free}.", space) : t("Cần {required} trống.", space)) : null;
  const refusal = notice ? INSTALL_REFUSALS[notice.code] ?? INSTALL_REFUSALS.request_failed : null;
  if (JOB_ACTIVE.includes(job.state)) {
    const phase = job.state === "running" ? job.phase ?? "downloading" : job.state;
    const total = finite(job.bytes_total);
    const done = finite(job.bytes_done);
    const counting = phase === "downloading" && total && done !== null;
    return {
      view: { kind: "progress", phase: typeof phase === "string" && Object.hasOwn(INSTALL_PHASES, phase) ? INSTALL_PHASES[phase] : INSTALL_PHASES.downloading, cancelDisabled: job.state === "cancelling" || job.sending === true, refusal },
      progress: { ratio: counting ? Math.min(done / total, 1) : null, detail: counting ? t("{done} / {total}", { done: bytes(done), total: bytes(total) }) : null },
    };
  }
  if (job.state === "failed") {
    const message = (INSTALL_ERRORS[job.error] ?? INSTALL_ERRORS.interrupted)(finite(job.required_bytes) !== null && finite(job.free_bytes) !== null ? { required: bytes(job.required_bytes), free: bytes(job.free_bytes) } : space);
    return { view: { kind: "failed", message, refusal }, progress: null };
  }
  const roomy = required === null || free === null || free >= required;
  return { view: { kind: "offer", spaceLine, blocked: !roomy, blockedMessage: roomy ? null : INSTALL_ERRORS.insufficient_disk(space), refusal }, progress: null };
}

export function selectAsr(s) {
  const text = { loading: t("Đang tải danh sách model…"), error: t("Không tải được danh sách model. Mở lại Cài đặt để thử lại."), empty: t("Máy chủ không trả về model nào. Mở lại Cài đặt để thử lại.") }[s.status] ?? null;
  const auto = s.models.find((choice) => choice.id === "auto");
  const models = s.models.filter((choice) => choice.id !== "auto");
  if (s.status === "loading" && !models.length) return { text, defaultText: null, rows: [] };
  if (s.status === "error" || s.status === "empty") return { text, defaultText: null, rows: [] };
  const defaultName = auto?.model_name ?? null;
  const usable = installerUsable(s.installer);
  const rows = models.map((choice) => {
    const reason = choice.ready ? "ready" : choice.reason in ASR_STATUS ? choice.reason : "not_prepared";
    // Without a usable installer the page keeps what it always had: the command to copy.
    const install = reason === "not_prepared" && (usable || (s.installer.available && jobActive(choice))) ? installView(choice, s.notice?.id === choice.id ? s.notice : null) : null;
    const busy = install?.view.kind === "progress";
    const state = busy ? "installing" : reason;
    return {
      id: choice.id,
      label: ASR_MODEL_TEXT[choice.id].label,
      note: ASR_MODEL_TEXT[choice.id].note,
      isDefault: Boolean(defaultName) && choice.model_name === defaultName,
      state,
      status: ASR_STATUS[state],
      guidance: reason === "ready" ? null : install ? INSTALL_GUIDANCE : ASR_MODEL_GUIDANCE[reason] ?? null,
      command: reason === "not_prepared" && !install ? choice.prepare_command || "" : "",
      copy: s.copy?.id === choice.id ? ASR_COPY[s.copy.how] : null,
      install: install?.view ?? null,
      progress: install?.progress ?? null,
    };
  });
  // No worker means no model can be checked: say it once, above the list, instead of on every row.
  if (rows.length && rows.every((row) => row.state === "worker_unavailable")) {
    return { text: ASR_MODEL_GUIDANCE.worker_unavailable, defaultText: null, defaultControl: auto ? defaultControl(s, auto, rows.map((row) => ({ id: row.id, ready: false })), false) : null, rows: rows.map((row) => ({ ...row, guidance: null })) };
  }
  return { text, defaultText: null, defaultControl: auto ? defaultControl(s, auto, rows.map((row) => ({ id: row.id, ready: row.state === "ready" })), true) : null, rows };
}

export function selectView(d, s) {
  const connected = s.info?.status === "connected";
  const inUse = providerInUse(s);
  const mutationReason = inUse ? errorMessageForCode("provider_in_use", 409, d.id) : null;
  const model = s.selectedModel;
  const available = modelAvailable(s, model);
  const efforts = d.effort.kind === "per-model" ? [{ value: "default", label: DEFAULT_EFFORT_LABEL }, ...supportedEfforts(s, model).map((value) => ({ value, label: EFFORT_LABELS[value] }))] : null;
  const effort = d.effort.kind === "per-model" ? effortValue(d, s, model, s.selectedEffort) : s.selectedEffort;
  const view = {
    effortOptions: efforts,
    effortValue: effort,
    account: s.info ? s.info.account?.label || d.copy.noAccount : null,
    modelSectionHidden: !connected,
    signInHidden: connected || Boolean(s.login),
    registrationHidden: d.id !== "chatgpt"
      || !s.info
      || !["connected", "reconnect_required"].includes(s.info.status)
      || Boolean(s.login),
    registrationConfirming: d.id === "chatgpt" && s.recreateConfirming,
    registrationDisabled: inUse || s.pending.recreate,
    registrationLabel: d.copy.recreateConfirm,
    registrationSubmitting: d.copy.recreateSubmitting,
  };
  if (d.exclusive) {
    const anyBusy = busy(d, s, "save");
    const mutationBlocked = inUse || anyBusy;
    return {
      ...view,
      mutationReason,
      modelDisabled: !connected || mutationBlocked,
      effortDisabled: !connected || mutationBlocked,
      saveDisabled: !available || mutationBlocked || (model === s.savedModel && effort === s.savedEffort),
      verifyDisabled: !available || mutationBlocked,
      loginHidden: !s.login,
      disconnectHidden: !connected,
      disconnectDisabled: inUse || s.pending.disconnect,
      disconnectLabel: s.pending.disconnect ? d.copy.disconnecting : d.copy.disconnect,
    };
  }
  const canUse = connected && available;
  const preferenceActive = typeof s.preferences.models[d.id] === "string" && s.preferences.models[d.id] !== "";
  return {
    ...view,
    mutationReason,
    modelDisabled: !connected || inUse || s.pending.save || s.models.length === 0,
    effortDisabled: !canUse || inUse || s.pending.save || efforts.length < 2,
    saveDisabled: !canUse || inUse || s.pending.save || (model === s.savedModel && effort === s.savedEffort && preferenceActive),
    verifyDisabled: !canUse || inUse || s.pending.verify,
    pendingHidden: !s.login,
    cancelDisabled: !s.login || s.cancelling,
    disconnectHidden: !s.info || s.info.status === "disconnected",
    disconnectDisabled: inUse || s.pending.disconnect,
    disconnectLabel: s.pending.disconnect ? d.copy.disconnecting : d.copy.disconnect,
  };
}
