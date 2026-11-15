export const initialState = Object.freeze({
  status: null,
  statusEpoch: null,
  statusSequence: -1,
  retiredEpochs: Object.freeze([]),
  pending: Object.freeze({}),
  statusRequestId: null,
  message: "",
  error: "",
  setupRequired: false
});

function copyState(state, patch) {
  return { ...state, ...patch };
}

function isNewerStatus(state, snapshot) {
  if (!snapshot || typeof snapshot !== "object" || typeof snapshot.statusEpoch !== "string") return false;
  if (state.retiredEpochs.includes(snapshot.statusEpoch)) return false;
  if (snapshot.statusEpoch === state.statusEpoch && Number.isSafeInteger(snapshot.statusSequence) && snapshot.statusSequence < state.statusSequence) return false;
  return true;
}

export function reduce(state, action) {
  switch (action.type) {
    case "request-started":
      return copyState(state, {
        pending: { ...state.pending, [action.requestId]: action.kind },
        statusRequestId: action.kind === "status" ? action.requestId : state.statusRequestId,
        error: "",
        setupRequired: action.kind === "start" ? false : state.setupRequired
      });
    case "request-finished": {
      if (action.kind === "status" && state.statusRequestId !== action.requestId) return state;
      const pending = { ...state.pending };
      delete pending[action.requestId];
      return copyState(state, {
        pending,
        statusRequestId: action.kind === "status" ? null : state.statusRequestId,
        message: action.message || state.message,
        error: ""
      });
    }
    case "request-failed": {
      if (action.kind === "status" && state.statusRequestId !== action.requestId) return state;
      const pending = { ...state.pending };
      delete pending[action.requestId];
      return copyState(state, {
        pending,
        statusRequestId: action.kind === "status" ? null : state.statusRequestId,
        error: action.error || "Không thực hiện được yêu cầu.",
        setupRequired: Boolean(action.setupRequired) || state.setupRequired
      });
    }
    case "status-received": {
      if (action.requestId && state.statusRequestId !== action.requestId) return state;
      const snapshot = action.status;
      if (!isNewerStatus(state, snapshot)) return state;
      const retiredEpochs = state.statusEpoch && snapshot.statusEpoch !== state.statusEpoch
        ? [...state.retiredEpochs, state.statusEpoch].slice(-32)
        : state.retiredEpochs;
      return copyState(state, {
        status: snapshot,
        statusEpoch: snapshot.statusEpoch,
        statusSequence: snapshot.statusSequence,
        retiredEpochs,
        setupRequired: false,
        message: snapshot.message || state.message,
        error: snapshot.error || ""
      });
    }
    case "setup-required":
      return copyState(state, { setupRequired: true });
    case "message-received":
      return copyState(state, { message: action.message || "", error: "" });
    default:
      return state;
  }
}

export function isPending(state, kind) {
  return Object.values(state.pending).includes(kind);
}

export function isActiveCapture(capture) {
  return Boolean(capture && ["starting", "recording", "stopping"].includes(capture.status));
}

function captureSession(snapshot, capture) {
  if (!snapshot || !capture?.sessionId) return null;
  return snapshot.sessions?.find((session) => session.id === capture.sessionId) || null;
}

export function capturePresentation(snapshot) {
  const current = snapshot?.connection;
  const latest = (snapshot?.sessions || [])
    .filter((item) => item.endpoint?.serverUrl === current?.serverUrl &&
      item.endpoint?.webUrl === current?.webUrl &&
      item.endpoint?.backendId === current?.backendId && item.upload?.state !== "discarded")
    .sort((a, b) => (b.capture?.startedAt || 0) - (a.capture?.startedAt || 0))[0];
  const capture = snapshot?.capture || latest?.capture;
  const session = captureSession(snapshot, capture);
  if (!capture) {
    if (!snapshot?.connection?.paired) return { label: "Cần thiết lập", detail: "Ghép server và cấp quyền microphone để bắt đầu.", state: "warning" };
    if (snapshot.micReady !== true) return { label: "Cần microphone", detail: "Cấp quyền microphone trên trang thiết lập.", state: "warning" };
    return { label: "Sẵn sàng", detail: "Tab hiện tại sẽ là nguồn ghi.", state: "ready" };
  }
  if (capture.status === "starting") return { label: "Đang khởi động", detail: "Đang nối tab và microphone…", state: "recording" };
  if (capture.status === "recording") return { label: "Đang ghi âm", detail: "Bạn có thể đóng popup; ghi âm vẫn tiếp tục.", state: "recording" };
  if (capture.status === "stopping") return { label: "Đang lưu…", detail: "Đang lưu phần audio cuối cùng.", state: "recording" };
  if (capture.status === "interrupted") return { label: "Bị gián đoạn", detail: "Audio đã giữ lại nhưng chưa hoàn tất.", state: "warning" };
  if (capture.status === "complete") {
    if (session?.upload?.cleaned && session.upload.receipt) return { label: "Đã gửi xong", detail: "Server đã nhận đủ audio.", state: "ready" };
    if (session?.upload?.error) return { label: "Chưa gửi xong", detail: "Audio vẫn được giữ. Mở thiết lập để xem lỗi và gửi lại.", state: "warning" };
    return { label: "Đang gửi", detail: "Đang gửi bản ghi lên server.", state: "uploading" };
  }
  return { label: "Sẵn sàng", detail: "Tab hiện tại sẽ là nguồn ghi.", state: "ready" };
}
