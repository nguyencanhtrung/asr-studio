export const initialState = Object.freeze({
  ready: false,
  setupOpen: false,
  serverUrl: "",
  serverUrlEdited: false,
  pairCode: "",
  selectedDeviceId: "",
  devices: [],
  mic: "unknown",
  connection: null,
  capture: null,
  sessions: [],
  message: "",
  error: "",
  pending: Object.freeze({}),
  statusRequestId: null,
  statusEpoch: null,
  statusSequence: 0,
  retiredEpochs: Object.freeze([])
});

function copyState(state, patch) {
  return { ...state, ...patch };
}



export function reduce(state, action) {
  switch (action.type) {
    case "field-changed":
      return copyState(state, { [action.name]: action.value, ...(action.name === "serverUrl" ? { serverUrlEdited: true } : {}), error: "" });
    case "mic-check-started":
      return copyState(state, { mic: "checking", error: "", message: "Đang chờ Chrome hỏi quyền microphone…" });
    case "mic-check-finished":
      return copyState(state, { mic: action.ok ? "ready" : "error", devices: action.devices || [], error: action.error || "", message: action.ok ? "Đã cấp quyền microphone." : "" });
    case "request-started":
      return copyState(state, {
        pending: { ...state.pending, [action.requestId]: action.kind },
        statusRequestId: action.kind === "status" ? action.requestId : state.statusRequestId,
        error: ""
      });
    case "request-finished": {
      if (action.kind === "status" && state.statusRequestId !== action.requestId) return state;
      const pending = { ...state.pending };
      delete pending[action.requestId];
      return copyState(state, { pending, statusRequestId: action.kind === "status" ? null : state.statusRequestId, message: action.message || state.message, error: "" });
    }
    case "request-failed": {
      if (action.kind === "status" && state.statusRequestId !== action.requestId) return state;
      const pending = { ...state.pending };
      delete pending[action.requestId];
      return copyState(state, { pending, statusRequestId: action.kind === "status" ? null : state.statusRequestId, error: action.error || "Không thực hiện được yêu cầu." });
    }
    case "status-received": {
      if (action.requestId && state.statusRequestId !== action.requestId) return state;
      const snapshot = action.status;
      if (snapshot.statusEpoch === state.statusEpoch && snapshot.statusSequence < state.statusSequence) return state;
      if (state.retiredEpochs.includes(snapshot.statusEpoch)) return state;
      const retiredEpochs = state.statusEpoch && snapshot.statusEpoch !== state.statusEpoch
        ? [...state.retiredEpochs, state.statusEpoch].slice(-32) : state.retiredEpochs;
      return copyState(state, {
        ready: true,
        connection: action.status.connection,
        serverUrl: state.serverUrlEdited ? state.serverUrl : action.status.connection?.serverUrl || "",
        mic: action.status.micReady === true ? "ready" : state.mic,
        capture: action.status.capture,
        sessions: action.status.sessions || state.sessions,
        statusEpoch: snapshot.statusEpoch,
        statusSequence: snapshot.statusSequence,
        retiredEpochs,
        setupOpen: action.status.connection?.paired ? state.setupOpen : true,
        message: action.status.message || state.message,
        error: action.status.error || ""
      });
    }
    case "setup-opened":
      return copyState(state, { setupOpen: true, error: "" });
    case "setup-closed":
      return copyState(state, { setupOpen: false, error: "" });
    case "reset-pairing":
      return copyState(state, { connection: null, setupOpen: true, message: "Đã xóa ghép nối. Hãy ghép lại với server đã chọn." });
    default:
      return state;
  }
}

export function isPending(state, kind) {
  return Object.values(state.pending).includes(kind);
}

export function visibleSessions(sessions) {
  return sessions
    .filter((session) => session.capture?.status !== "discarded" && session.upload?.state !== "discarded")
    .slice()
    .sort((a, b) => (b.capture?.startedAt || 0) - (a.capture?.startedAt || 0) || a.id.localeCompare(b.id));
}
