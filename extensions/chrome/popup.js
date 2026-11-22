import { bundleFor, RELEASE_ORIGIN } from "./release.js";
import { capturePresentation, initialState, isActiveCapture, isPending, pendingRecovery, reduce } from "./popup-core.js";
import { recordedDurationLabel } from "./capture-core.js";

let state = initialState;
let requestSequence = 0;
let elapsedTimer = null;

const $ = (id) => document.getElementById(id);
const elements = {
  statusDetail: $("status-detail"),
  captureState: $("capture-state"),
  connectionDot: $("connection-dot"),
  serverUrl: $("server-url"),
  deviceExpiry: $("device-expiry"),
  message: $("popup-message"),
  error: $("popup-error"),
  errorCopy: $("error-copy"),
  setupErrorButton: $("setup-error-button"),
  startButton: $("start-button"),
  stopButton: $("stop-button"),
  elapsed: $("elapsed"),
  tabGate: $("tab-gate"),
  micGate: $("mic-gate"),
  tabLevel: $("tab-level"),
  micLevel: $("mic-level"),
  levelsLabel: $("levels-label"),
  setupHint: $("setup-hint"),
  setupHintButton: $("setup-hint-button"),
  setupOpen: $("setup-open"),
  actionPanel: $("action-panel"),
  recoveryPanel: $("recovery-panel"),
  recoveryDuration: $("recovery-duration"),
  recoveryReason: $("recovery-reason"),
  savePartialButton: $("save-partial-button"),
  cancelRemoteButton: $("cancel-remote-button"),
  sourcesPanel: $("sources-panel")
};

function dispatch(action) {
  state = reduce(state, action);
  render();
}

function setText(element, value) {
  element.textContent = t(value || "");
}

function nextRequestId(kind) {
  requestSequence += 1;
  return `${kind}:${requestSequence}`;
}

function localizedError(error) {
  if (typeof error === "string") return t(error);
  const code = error?.code;
  const known = {
    worker_unavailable: t("Extension worker không phản hồi. Hãy mở lại tiện ích và thử lại."),
    tab_missing: t("Không tìm thấy tab đang hoạt động."),
    invalid_source_tab: t("Chỉ ghi được từ tab HTTP hoặc HTTPS đang hoạt động. Không thể ghi trang Chrome hay trang của tiện ích."),
    tab_capture_denied: t("Chrome không cấp được quyền ghi tab này. Hãy quay lại tab cuộc họp rồi thử lại."),
    pair_required: t("Hãy ghép extension với server trước khi ghi."),
    setup_required: t("Hãy hoàn tất thiết lập microphone và server trước khi ghi."),
    connection_busy: t("Thiết lập kết nối khác đang chạy. Hãy thử lại sau một lát."),
    capture_busy: t("Đã có một bản ghi đang chạy."),
    capture_idle: t("Hiện không có bản ghi đang chạy."),
    microphone_denied: t("Chrome chưa cấp quyền microphone. Mở thiết lập để cấp quyền."),
    mic_required: t("Hãy cấp quyền microphone trên trang thiết lập."),
    offscreen_unavailable: t("Chrome chưa tạo được bộ ghi nền. Hãy thử lại."),
    forbidden_sender: t("Yêu cầu không đến từ giao diện extension được tin cậy."),
    api_version_mismatch: t("API không tương thích. Kiểm tra phiên bản extension và server."),
    protocol_mismatch: t("Server trả về dữ liệu không đúng hợp đồng."),
    backend_mismatch: t("Server trả về dữ liệu của backend khác."),
    invalid_web_url: t("URL web của server không hợp lệ để mở kết quả."),
    invalid_result_url: t("Link kết quả không thuộc web origin đã ghép."),
    invalid_url: t("URL server không hợp lệ."),
    network_error: t("Không kết nối được tới server."),
    timeout: t("Server phản hồi quá lâu."),
    backend_unavailable: t("Server tạm thời không sẵn sàng."),
    auth_expired: t("Phiên ghép server đã hết hạn hoặc bị thu hồi. Mở Settings → Devices trong ASR Studio và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại."),
    not_found: t("Server không tìm thấy phiên của thiết bị này. Mở Settings → Devices và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại."),
    binding_locked: t("Mã ghép thuộc server hoặc thiết bị khác. Mở Settings → Devices và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại."),
    pair_locked: t("Không thể ghép lại khi đang ghi. Hãy dừng bản ghi trước; audio đã lưu vẫn được giữ lại."),
    url_locked: t("Không thể đổi server khi còn audio đang chờ xử lý."),
    backend_error: t("Server không nhận yêu cầu này."),
    source_ended: t("Nguồn âm thanh đã dừng. Bản ghi được giữ lại."),
    source_inactive: t("Nguồn âm thanh không còn hoạt động. Bản ghi được giữ lại."),
    write_backlog: t("Âm thanh đến quá nhanh. Bản ghi được giữ lại."),
    capture_incomplete: t("Bản ghi chưa lưu đủ audio. Bản ghi được giữ lại."),
    recorder_error: t("Trình ghi âm của Chrome báo lỗi. Bản ghi được giữ lại."),
    owner_restarted: t("Tiện ích đã khởi động lại khi đang ghi. Bản ghi được giữ lại."),
    local_quota: t("Đã đạt giới hạn lưu audio trên máy. Bản ghi được giữ lại."),
    unknown: t("Ghi âm dừng ngoài dự kiến. Bản ghi được giữ lại."),
    nothing_to_save: t("Bản ghi này chưa có audio để lưu."),
    already_canceled: t("Bản ghi đã được hủy trên server."),
    already_saving: t("Phần đã ghi đang được lưu; không thể hủy nữa."),
    already_canceling: t("Phiên đang được hủy; không thể lưu phần đã ghi nữa."),
    already_saved: t("Server đã lưu bản ghi này.")
  };
  return known[code] || t("Không thực hiện được yêu cầu. Hãy thử lại.");
}

function localizedMessage(message) {
  if (!message) return "";
  if (message.startsWith("Saved recorded part.")) return t("Server đã lưu phần đã ghi; hãy bắt đầu chép lời trong ASR Studio.");
  if (message.startsWith("Uploaded.")) return t("Server đã nhận đủ audio; xử lý tiếp tục trong ASR Studio.");
  if (message.startsWith("The server has the complete audio")) return t("Server đã nhận đủ audio; xử lý tiếp tục trong ASR Studio.");
  return "";
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

function formatElapsed(startedAt) {
  if (!Number.isFinite(startedAt)) return "00:00";
  const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60).toString().padStart(2, "0");
  const seconds = (elapsed % 60).toString().padStart(2, "0");
  return hours > 0 ? `${hours}:${minutes}:${seconds}` : `${minutes}:${seconds}`;
}

function sendRequest(kind, message, onSuccess = () => undefined) {
  const requestId = nextRequestId(kind);
  dispatch({ type: "request-started", kind, requestId });
  return chrome.runtime.sendMessage({ ...message, target: "worker" })
    .then((result) => {
      if (!result?.ok) {
        const setupRequired = Boolean(result?.setupRequired) || ["pair_required", "mic_required", "setup_required"].includes(result?.error?.code);
        dispatch({
          type: "request-failed",
          kind,
          requestId,
          setupRequired,
          error: result?.error || { code: "client_error" }
        });
        if (setupRequired) dispatch({ type: "setup-required" });
        return result;
      }
      if (kind === "status" && result.status) dispatch({ type: "status-received", requestId, status: result.status });
      dispatch({ type: "request-finished", kind, requestId, message: result.message });
      void onSuccess(result);
      return result;
    })
    .catch(() => {
      const error = { code: "worker_unavailable" };
      dispatch({ type: "request-failed", kind, requestId, error });
      return { ok: false, error };
    });
}

function loadStatus() {
  return sendRequest("status", { type: "GET_STATUS" });
}

function openSetup() {
  void sendRequest("setup", { type: "OPEN_SETUP" });
}

function renderLevels(capture) {
  const levels = capture?.levels;
  const hasLevels = Number.isFinite(levels?.tab) || Number.isFinite(levels?.microphone);
  elements.levelsLabel.hidden = !hasLevels;
  for (const [source, meter] of [["tab", elements.tabLevel], ["microphone", elements.micLevel]]) {
    const value = Number.isFinite(levels?.[source]) ? Math.max(0, Math.min(1, levels[source])) : 0;
    meter.hidden = !hasLevels;
    meter.value = value;
  }
}

function renderSourceButton(button, source, capture) {
  const enabled = capture?.gates?.[source] !== false;
  const canChange = capture && ["starting", "recording"].includes(capture.status);
  const pending = isPending(state, `gate-${source}`);
  button.disabled = !canChange || pending;
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", `${source === "tab" ? t("Âm thanh tab") : "Microphone"}: ${enabled ? t("đang bật") : t("đang tắt")}`);
  button.textContent = enabled ? t("Bật") : t("Tắt");
}

function render() {
  const snapshot = state.status;
  $("release-panel").hidden = !snapshot || (!snapshot.updateSafe && !snapshot.checksEnabled);
  $("release-checks").checked = Boolean(snapshot?.checksEnabled);
  $("release-checks").disabled = isPending(state, "release-checks");
  const bundle = bundleFor(snapshot?.availableUpdate);
  $("release-links").hidden = !bundle;
  if (bundle) {
    $("release-zip").textContent = `ZIP ${snapshot.availableUpdate}`;
    $("release-zip").href = bundle;
    $("release-checksum").href = bundle + ".sha256";
  }
  const capture = snapshot?.capture || null;
  const presentation = capturePresentation(snapshot);
  const connection = snapshot?.connection;
  const active = isActiveCapture(capture);
  const stopping = capture?.status === "stopping";

  setText(elements.statusDetail, presentation.detail);
  setText(elements.captureState, presentation.label);
  elements.captureState.dataset.state = presentation.state;
  elements.connectionDot.classList.toggle("is-ready", Boolean(connection?.paired));
  setText(elements.serverUrl, connection?.serverUrl || t("Chưa ghép server"));
  setText(elements.deviceExpiry, connection?.paired ? formatDeviceExpiry(connection.deviceExpiresAt) : "");
  setText(elements.message, localizedMessage(state.message));

  const error = snapshot?.compatibilityError || state.error;
  elements.error.hidden = !error;
  setText(elements.errorCopy, localizedError(error));
  elements.setupErrorButton.hidden = !state.setupRequired;

  elements.startButton.hidden = active;
  elements.stopButton.hidden = !active;
  elements.startButton.disabled = isPending(state, "start");
  elements.stopButton.disabled = stopping || isPending(state, "stop");
  setText(elements.stopButton, stopping ? t("Đang lưu…") : t("Dừng và lưu"));
  setText(elements.elapsed, active ? formatElapsed(capture.startedAt) : "00:00");

  const recovery = active ? null : pendingRecovery(snapshot);
  elements.recoveryPanel.hidden = !recovery;
  if (recovery) {
    setText(elements.recoveryDuration, t("Khoảng {duration}", { duration: recordedDurationLabel(recovery.capture.chunkCount) }));
    const reason = localizedError({ code: recovery.capture.interruptionCode || "unknown" });
    setText(elements.recoveryReason, recovery.capture.chunkCount > 0 ? reason : `${reason} ${localizedError({ code: "nothing_to_save" })}`);
    const choosing = isPending(state, "save-partial") || isPending(state, "cancel-remote");
    elements.savePartialButton.disabled = choosing || !(recovery.capture.chunkCount > 0);
    elements.cancelRemoteButton.disabled = choosing;
  }

  renderSourceButton(elements.tabGate, "tab", capture);
  renderSourceButton(elements.micGate, "microphone", capture);
  renderLevels(capture);

  const needsSetup = state.setupRequired || !connection?.paired || snapshot?.micReady !== true;
  elements.setupHint.hidden = !needsSetup;
  elements.actionPanel.hidden = needsSetup && !active;
  elements.sourcesPanel.hidden = needsSetup && !active;

  if (active && !elapsedTimer) {
    elapsedTimer = window.setInterval(() => {
      if (!isActiveCapture(state.status?.capture)) {
        window.clearInterval(elapsedTimer);
        elapsedTimer = null;
        return;
      }
      setText(elements.elapsed, formatElapsed(state.status.capture.startedAt));
    }, 1000);
  } else if (!active && elapsedTimer) {
    window.clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function toggleGate(source) {
  const capture = state.status?.capture;
  if (!capture || !["starting", "recording"].includes(capture.status)) return;
  const enabled = capture.gates?.[source] === false;
  void sendRequest(`gate-${source}`, { type: "SET_GATE", source, enabled }, loadStatus);
}

$("release-checks").addEventListener("change", async (event) => {
  const enabled = event.target.checked;
  $("release-permission").hidden = true;
  if (enabled && !await chrome.permissions.request({ origins: [RELEASE_ORIGIN] }).catch(() => false)) {
    $("release-permission").hidden = false;
    render();
    return;
  }
  await sendRequest("release-checks", { type: "SET_UPDATE_CHECKS", enabled }, loadStatus);
});

elements.startButton.addEventListener("click", () => void sendRequest("start", { type: "START_CAPTURE" }, loadStatus));
elements.stopButton.addEventListener("click", () => void sendRequest("stop", { type: "STOP_CAPTURE" }, loadStatus));
elements.savePartialButton.addEventListener("click", () => {
  const recovery = pendingRecovery(state.status);
  if (recovery) void sendRequest("save-partial", { type: "SAVE_INTERRUPTED", sessionId: recovery.id }, loadStatus);
});
elements.cancelRemoteButton.addEventListener("click", () => {
  const recovery = pendingRecovery(state.status);
  // Cancel keeps local audio and server staging, so the popup asks no confirmation.
  if (recovery) void sendRequest("cancel-remote", { type: "CANCEL_INTERRUPTED", sessionId: recovery.id }, loadStatus);
});
elements.tabGate.addEventListener("click", () => toggleGate("tab"));
elements.micGate.addEventListener("click", () => toggleGate("microphone"));
elements.setupOpen.addEventListener("click", openSetup);
elements.setupHintButton.addEventListener("click", openSetup);
elements.setupErrorButton.addEventListener("click", openSetup);

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.target !== "extension-ui" || sender?.id !== chrome.runtime.id || sender?.url !== chrome.runtime.getURL("sw.js")) return;
  if (message.type === "STATUS_UPDATE") dispatch({ type: "status-received", status: message.status });
  if (message.type === "NOTICE") dispatch({ type: "message-received", message: message.message });
});

onLanguageChange(() => {
  render();
});
render();
loadStatus();
