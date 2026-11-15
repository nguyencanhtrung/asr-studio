import { initialState, isPending, reduce, visibleSessions } from "./control-core.js";
import { MIME_TYPE, normalizeServerUrl, permissionPattern } from "./protocol.js";

let state = initialState;
let requestSequence = 0;
let renderedDevices = "";
let renderedSessions = "";

const $ = (id) => document.getElementById(id);
const elements = {
  setupPanel: $("setup-panel"),
  serverUrl: $("server-url"),
  pairCode: $("pair-code"),
  audioDevice: $("audio-device"),
  micState: $("mic-state"),
  micButton: $("mic-button"),
  pairButton: $("pair-button"),
  webRootButton: $("web-root-button"),
  setupToggle: $("setup-toggle"),
  globalMessage: $("global-message"),
  globalError: $("global-error"),
  connectionDot: $("connection-dot"),
  connectionLabel: $("connection-label"),
  deviceExpiry: $("device-expiry"),
  captureState: $("capture-state"),
  stopButton: $("stop-button"),
  tabGate: $("tab-gate"),
  micGate: $("mic-gate"),
  tabMonitor: $("tab-monitor"),
  tabMeter: $("tab-meter"),
  micMeter: $("mic-meter"),
  refreshButton: $("refresh-button"),
  sessionsList: $("sessions-list")
};

function dispatch(action) {
  state = reduce(state, action);
  render();
}

function nextRequestId(kind) {
  requestSequence += 1;
  return `${kind}:${requestSequence}`;
}

function sendRequest(kind, message, onSuccess = () => undefined) {
  const requestId = nextRequestId(kind);
  dispatch({ type: "request-started", kind, requestId });
  chrome.runtime.sendMessage({ ...message, target: "worker" })
    .then((result) => {
      if (!result?.ok) {
        dispatch({ type: "request-failed", kind, requestId, error: result?.error || { code: "client_error" } });
        return;
      }
      if (kind === "status" && result.status) dispatch({ type: "status-received", requestId, status: result.status });
      dispatch({ type: "request-finished", kind, requestId, message: result.message });
      void onSuccess(result);
    })
    .catch((error) => {
      dispatch({ type: "request-failed", kind, requestId, error: { code: "worker_unavailable" } });
    });
}

function loadStatus() {
  sendRequest("status", { type: "GET_STATUS" });
}

function setField(name, value) {
  dispatch({ type: "field-changed", name, value });
}

function setText(element, value) {
  element.textContent = t(value || "");
}

function localizedError(error) {
  if (typeof error === "string") return t(error);
  const code = error?.code;
  const known = {
    microphone_denied: t("Chrome chưa cấp quyền microphone."),
    invalid_url: t("URL server không hợp lệ."),
    protocol_mismatch: t("Server trả về dữ liệu không đúng hợp đồng."),
    backend_mismatch: t("Server trả về dữ liệu của backend khác."),
    invalid_web_url: t("URL web của server không hợp lệ để mở kết quả."),
    invalid_result_url: t("Link kết quả không thuộc web origin đã ghép."),
    host_permission: t("Chrome chưa cấp quyền cho server. Bấm Thử gửi lại để cấp quyền và tiếp tục."),
    network_error: t("Không kết nối được tới server."),
    timeout: t("Server phản hồi quá lâu."),
    auth_expired: t("Phiên ghép server đã hết hạn hoặc bị thu hồi. Hãy ghép lại."),
    forbidden: t("Server không cho phép extension này."),
    pair_locked: t("Không thể thay thiết bị khi còn audio đang chờ xử lý."),
    url_locked: t("Không thể đổi server khi còn audio đang chờ xử lý."),
    tab_capture_denied: t("Chrome không cấp được quyền ghi tab này. Hãy quay lại tab cuộc họp rồi thử lại."),
    capture_start_failed: t("Không thể bắt đầu ghi âm. Hãy thử lại."),
    source_ended: t("Nguồn âm thanh đã dừng. Bản ghi được giữ lại."),
    source_inactive: t("Nguồn âm thanh không còn hoạt động. Bản ghi được giữ lại."),
    write_backlog: t("Âm thanh đến quá nhanh. Bản ghi được giữ lại."),
    capture_incomplete: t("Bản ghi chưa lưu đủ audio. Bản ghi được giữ lại."),
    capture_missing: t("Không tìm thấy bản ghi trên máy."),
    local_chunk_missing: t("Thiếu một phần audio trên máy. Bản ghi được giữ lại."),
    server_failed: t("Server chưa xác nhận lưu đủ audio. Bản ghi được giữ lại."),
    receipt_mismatch: t("Server chưa xác nhận đúng bản ghi. Bản ghi được giữ lại."),
    media_invalid: t("Server không nhận dạng được audio của bản ghi."),
    backend_storage: t("Server đã hết chỗ lưu. Bản ghi được giữ lại."),
    byte_limit: t("Bản ghi vượt quá giới hạn của server."),
    not_found: t("Server không tìm thấy bản ghi này."),
    conflict: t("Server từ chối bản ghi do xung đột trạng thái."),
    backend_unavailable: t("Server tạm thời không sẵn sàng."),
    backend_error: t("Server không nhận yêu cầu này."),
    worker_unavailable: t("Extension worker không phản hồi. Hãy thử lại."),
    forbidden_sender: t("Yêu cầu không đến từ giao diện extension được tin cậy.")
  };
  if (known[code]) return known[code];
  if (typeof error?.message === "string" && error.message.includes("microphone")) return t("Chrome chưa cấp quyền microphone.");
  return t(error?.message && /[À-ỹ]/.test(error.message) ? error.message : "Không thực hiện được yêu cầu. Hãy thử lại.");
}

function localizedMessage(message) {
  if (!message) return "";
  if (message.startsWith("Uploaded.")) return t("Server đã nhận đủ audio; xử lý tiếp tục trong ASR Studio.");
  if (message.startsWith("The server has the complete audio")) return t("Server đã nhận đủ audio; xử lý tiếp tục trong ASR Studio.");
  return t(message);
}

function formatDeviceExpiry(value) {
  if (value === null) return t("Thiết bị tin cậy không hết hạn");
  if (typeof value !== "string") return "";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "";
  return t("Thiết bị tin cậy đến {date}", { date: new Intl.DateTimeFormat(getLocale(), {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(timestamp)) });
}

function bytesLabel(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function renderDevices() {
  const key = JSON.stringify([state.devices, state.selectedDeviceId, state.mic]);
  if (key === renderedDevices) return;
  renderedDevices = key;
  const select = elements.audioDevice;
  select.replaceChildren();
  if (!state.devices.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = state.mic === "ready" ? t("Microphone mặc định") : t("Cấp quyền để xem microphone");
    select.append(option);
    select.disabled = state.mic !== "ready";
    return;
  }
  for (const device of state.devices) {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = device.label || "Microphone";
    option.selected = device.deviceId === state.selectedDeviceId;
    select.append(option);
  }
  select.disabled = false;
}

function statusText(capture) {
  if (!capture) return t("Chưa sẵn sàng");
  if (capture.status === "starting") return t("Đang khởi động");
  if (capture.status === "recording") return t("Đang ghi âm");
  if (capture.status === "stopping") return t("Đang lưu…");
  if (capture.status === "complete") return t("Đã ghi");
  if (capture.status === "interrupted") return t("Bị gián đoạn");
  return capture.status;
}

function uploadText(upload, capture) {
  if (upload?.cleaned && upload.receipt) return t("đã nhận đủ");
  if (capture?.status === "interrupted") return t("giữ trên máy");
  if (upload?.state === "waiting_for_stop") return t("chờ dừng ghi");
  if (upload?.state === "failed") return t("gửi thất bại");
  if (upload?.state === "finalizing" || upload?.state === "finalize_pending") return t("chờ xác nhận");
  if (upload?.state === "retryable") return t("sẽ thử lại");
  if (upload?.state === "needs_reconnect" || upload?.state === "auth_required") return t("cần kết nối lại");
  if (upload?.state === "server_failed" || upload?.state === "receipt_mismatch") return t("lỗi xác nhận");
  return upload?.state === "receiving" ? t("đang gửi") : t("chưa gửi");
}

function renderSessionRow(session) {
  const row = document.createElement("article");
  row.className = "session-row";
  row.dataset.sessionId = session.id;
  if (session.upload.state === "uploaded") row.classList.add("is-ready");
  if (session.capture.status === "interrupted" || session.upload.error) row.classList.add("is-error");

  const copy = document.createElement("div");
  const title = document.createElement("p");
  title.className = "session-title";
  title.textContent = session.capture.status === "interrupted" ? t("Bản ghi bị gián đoạn được giữ lại") : t("Bản ghi cục bộ");
  const meta = document.createElement("p");
  meta.className = "session-meta";
  meta.textContent = t("{status} · {size} · {chunks} · {upload}", {
    status: statusText(session.capture), size: bytesLabel(session.capture.totalBytes),
    chunks: session.capture.chunkCount === 1 ? t("1 phần audio") : t("{count} phần audio", { count: new Intl.NumberFormat(getLocale()).format(session.capture.chunkCount) }),
    upload: uploadText(session.upload, session.capture)
  });
  const recordedAt = document.createElement("time");
  recordedAt.className = "session-date";
  if (Number.isFinite(session.capture.startedAt) && session.capture.startedAt > 0) {
    recordedAt.dateTime = new Date(session.capture.startedAt).toISOString();
    recordedAt.textContent = new Intl.DateTimeFormat(getLocale(), { dateStyle: "medium", timeStyle: "short" }).format(new Date(session.capture.startedAt));
  }
  copy.append(title, recordedAt, meta);
  if (session.upload.cleaned && session.upload.receipt) {
    title.textContent = t("Đã lưu đủ audio trên server");
    const done = document.createElement("p");
    done.textContent = t("Có thể đóng tiện ích; kết quả tiếp tục được xử lý trong ASR Studio.");
    copy.append(done);
  }
  if (session.upload.error) {
    const error = document.createElement("p");
    error.className = "error";
    error.textContent = localizedError(session.upload.error);
    copy.append(error);
  }
  if (session.capture.error && !session.upload.cleaned) {
    const error = document.createElement("p");
    error.className = "session-meta";
    error.textContent = t("Âm thanh vẫn được giữ trên máy. Hãy thử lại hoặc bỏ bản ghi.");
    copy.append(error);
  }

  const actions = document.createElement("div");
  actions.className = "session-actions";
  if (!session.upload.cleaned && session.capture.captureComplete) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.dataset.action = "retry";
    retry.className = "quiet-button";
    retry.textContent = t("Thử gửi lại");
    retry.addEventListener("click", async () => {
      if (session.upload.error?.code === "host_permission") {
        const granted = await chrome.permissions.request({ origins: [permissionPattern(session.endpoint.serverUrl)] });
        if (!granted) return;
      }
      sendRequest("retry", { type: "RETRY_UPLOAD", sessionId: session.id }, loadStatus);
    });
    actions.append(retry);
  }
  if (session.upload.meetingUrl) {
    const result = document.createElement("button");
    result.type = "button";
    result.dataset.action = "result";
    result.className = "primary-button";
    result.textContent = t("Mở ASR Studio");
    result.addEventListener("click", () => sendRequest("result", { type: "OPEN_RESULT", sessionId: session.id }));
    actions.append(result);
  }
  if (!session.upload.cleaned && !session.upload.receipt) {
    const discard = document.createElement("button");
    discard.type = "button";
    discard.dataset.action = "discard";
    discard.className = "quiet-button";
    discard.textContent = t("Bỏ audio cục bộ");
    discard.addEventListener("click", () => {
      if (window.confirm(t("Bỏ audio và xóa bản ghi này khỏi lịch sử trên máy? Dữ liệu server không bị xóa."))) {
        sendRequest("discard", { type: "DISCARD_CAPTURE", sessionId: session.id }, loadStatus);
      }
    });
    actions.append(discard);
  }
  row.append(copy, actions);
  return row;
}

function render() {
  elements.setupPanel.hidden = !state.setupOpen;
  elements.serverUrl.value = state.serverUrl;
  elements.pairCode.value = state.pairCode;
  renderDevices();

  const connection = state.connection;
  elements.connectionDot.classList.toggle("is-ready", Boolean(connection?.paired));
  setText(elements.connectionLabel, connection?.serverUrl || t("Chưa ghép server"));
  setText(elements.deviceExpiry, connection?.paired ? formatDeviceExpiry(connection.deviceExpiresAt) : "");
  setText(elements.micState, state.mic === "ready" ? t("Đã cấp quyền. Audio kiểm tra đã dừng.") : state.mic === "checking" ? t("Đang chờ Chrome hỏi quyền…") : state.mic === "error" ? t("Cấp quyền microphone không thành công.") : t("Chưa kiểm tra quyền microphone."));
  elements.micButton.disabled = state.mic === "checking" || isPending(state, "mic");
  elements.pairButton.disabled = isPending(state, "pair") || state.mic !== "ready";
  elements.setupToggle.textContent = state.setupOpen ? t("Ẩn thiết lập") : t("Mở thiết lập");

  const capture = state.capture;
  setText(elements.captureState, statusText(capture));
  elements.captureState.dataset.state = capture?.status === "recording" || capture?.status === "starting" || capture?.status === "stopping"
    ? "recording"
    : capture?.status === "complete" ? "uploading" : capture?.status === "interrupted" ? "warning" : capture ? "ready" : "";
  elements.stopButton.disabled = !capture || !["starting", "recording", "stopping"].includes(capture.status);
  elements.stopButton.textContent = capture?.status === "stopping" ? t("Đang lưu…") : t("Dừng và lưu");
  const levels = capture?.levels || { tab: 0, microphone: 0 };
  elements.tabMeter.value = Math.max(0, Math.min(1, levels.tab || 0));
  elements.micMeter.value = Math.max(0, Math.min(1, levels.microphone || 0));
  const gates = capture?.gates || { tab: true, microphone: true };
  elements.tabGate.textContent = gates.tab ? t("Bật") : t("Tắt");
  elements.micGate.textContent = gates.microphone ? t("Bật") : t("Tắt");
  elements.tabGate.setAttribute("aria-pressed", String(gates.tab));
  elements.micGate.setAttribute("aria-pressed", String(gates.microphone));
  elements.tabMonitor.textContent = capture?.tabMonitor === false ? t("Bật âm thanh tab đang phát") : t("Tắt âm thanh tab đang phát");

  const sessions = visibleSessions(state.sessions);
  const sessionsKey = JSON.stringify(sessions);
  if (sessionsKey !== renderedSessions) {
    renderedSessions = sessionsKey;
    const focused = document.activeElement;
    const focusSession = focused?.closest(".session-row")?.dataset.sessionId;
    const focusAction = focused?.dataset.action;
    const focusIndex = [...elements.sessionsList.children].findIndex((item) => item.dataset.sessionId === focusSession);
    elements.sessionsList.replaceChildren();
    if (!sessions.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = t("Chưa có bản ghi cục bộ. Hoàn tất thiết lập rồi bắt đầu từ tab cuộc họp.");
      elements.sessionsList.append(empty);
    } else {
      for (const session of sessions) elements.sessionsList.append(renderSessionRow(session));
    }
    if (focusSession) {
      const row = [...elements.sessionsList.children].find((item) => item.dataset.sessionId === focusSession);
      const button = [...(row?.querySelectorAll("button") || [])].find((button) => button.dataset.action === focusAction);
      (button || elements.sessionsList.children[Math.min(focusIndex, elements.sessionsList.children.length - 1)]?.querySelector("button") || elements.refreshButton).focus();
    }
  }
  setText(elements.globalMessage, localizedMessage(state.message));
  setText(elements.globalError, state.error ? localizedError(state.error) : "");
}

async function checkMicrophone() {
  dispatch({ type: "mic-check-started" });
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Trang này không hỗ trợ microphone.");
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    for (const track of stream.getTracks()) track.stop();
    const devices = (await navigator.mediaDevices.enumerateDevices())
      .filter((device) => device.kind === "audioinput")
      .map((device) => ({ deviceId: device.deviceId, label: device.label || "Microphone" }));
    const selectedDeviceId = state.selectedDeviceId || devices[0]?.deviceId || "";
    dispatch({ type: "field-changed", name: "selectedDeviceId", value: selectedDeviceId });
    dispatch({ type: "mic-check-finished", ok: true, devices });
    sendRequest("mic", { type: "SET_MIC_SETUP", selectedDeviceId }, loadStatus);
  } catch (error) {
    dispatch({ type: "mic-check-finished", ok: false, error: { code: "microphone_denied" } });
  }
}

async function pair() {
  try {
    if (state.mic !== "ready") throw new Error("Hãy cấp quyền microphone trước khi ghép.");
    const serverUrl = normalizeServerUrl(state.serverUrl);
    const granted = await chrome.permissions.request({ origins: [permissionPattern(serverUrl)] });
    if (!granted) throw new Error("Chrome chưa cấp quyền cho server đã chọn.");
    sendRequest("pair", { type: "PAIR", serverUrl, code: state.pairCode }, () => {
      dispatch({ type: "field-changed", name: "pairCode", value: "" });
      dispatch({ type: "setup-closed" });
      loadStatus();
    });
  } catch (error) {
    dispatch({ type: "request-failed", kind: "pair", requestId: "none", error: { code: error?.code, message: error?.message } });
  }
}

function toggleGate(source) {
  const enabled = !(state.capture?.gates?.[source] ?? true);
  sendRequest("gate", { type: "SET_GATE", source, enabled }, loadStatus);
}

function toggleMonitor() {
  const enabled = state.capture?.tabMonitor === false;
  sendRequest("monitor", { type: "SET_TAB_MONITOR", enabled }, loadStatus);
}

elements.serverUrl.addEventListener("input", (event) => setField("serverUrl", event.target.value));
elements.serverUrl.addEventListener("change", () => sendRequest("server-url", { type: "SET_SERVER_URL", serverUrl: state.serverUrl }, loadStatus));
elements.pairCode.addEventListener("input", (event) => setField("pairCode", event.target.value));
elements.audioDevice.addEventListener("change", (event) => {
  const selectedDeviceId = event.target.value;
  setField("selectedDeviceId", selectedDeviceId);
  sendRequest("mic-device", { type: "SET_MIC_SETUP", selectedDeviceId }, loadStatus);
});
elements.micButton.addEventListener("click", () => void checkMicrophone());
elements.pairButton.addEventListener("click", () => void pair());
elements.setupToggle.addEventListener("click", () => dispatch({ type: state.setupOpen ? "setup-closed" : "setup-opened" }));
elements.webRootButton?.addEventListener("click", () => sendRequest("web-root", { type: "OPEN_WEB_ROOT" }));
elements.stopButton.addEventListener("click", () => sendRequest("stop", { type: "STOP_CAPTURE" }, loadStatus));
elements.tabGate.addEventListener("click", () => toggleGate("tab"));
elements.micGate.addEventListener("click", () => toggleGate("microphone"));
elements.tabMonitor.addEventListener("click", toggleMonitor);
elements.refreshButton.addEventListener("click", loadStatus);
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "y") {
    event.preventDefault();
    sendRequest("diagnostic", { type: "EXECUTE_ACTION" }, loadStatus);
  }
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.target !== "extension-ui" || sender?.id !== chrome.runtime.id || sender?.url !== chrome.runtime.getURL("sw.js")) return;
  if (message?.type === "STATUS_UPDATE") dispatch({ type: "status-received", status: message.status });
  if (message?.type === "NOTICE") dispatch({ type: "request-finished", kind: "notice", requestId: "notice", message: message.message });
});

if (new URLSearchParams(window.location.search).get("setup") === "1") dispatch({ type: "setup-opened" });
else dispatch({ type: "setup-closed" });
onLanguageChange(() => {
  renderedDevices = "";
  renderedSessions = "";
  render();
});
render();
loadStatus();

void MIME_TYPE;
