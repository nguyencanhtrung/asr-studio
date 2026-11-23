// Pure core for the operator's extension device settings. No DOM, network, timer,
// storage or clock: the view supplies effects to the existing store/runner.
// Contract: docs/adr/0010-frontend-reducer-core.md and #217 contract.md.

const t = (text, params) => (params ? { text, params } : { text });

export const DEVICE_LIFETIMES = ["7d", "30d", "90d", "never"];
const DEVICE_STATUSES = ["pending", "active", "expired", "revoked"];

const LIFETIME_TEXT = {
  "7d": t("7 ngày"),
  "30d": t("30 ngày"),
  "90d": t("90 ngày"),
  never: t("Không hết hạn"),
};

const STATUS_TEXT = {
  pending: t("Đang chờ ghép"),
  active: t("Đang hoạt động"),
  expired: t("Đã hết hạn"),
  revoked: t("Đã thu hồi"),
};

const ERROR_TEXT = {
  pending_limit: t("Có quá nhiều thiết bị đang chờ ghép. Hãy ghép hoặc thu hồi một thiết bị trước khi tạo mã mới."),
  authentication_required: t("Phiên workspace không còn hợp lệ. Hãy mở lại workspace từ link trong terminal."),
  device_not_found: t("Không tìm thấy thiết bị đã chọn. Hãy tải lại danh sách rồi thử lại."),
  device_record_not_found: t("Không tìm thấy thiết bị đã chọn. Hãy tải lại danh sách rồi thử lại."),
  device_expired: t("Thiết bị đã hết hạn. Hãy tạo mã ghép mới."),
  device_revoked: t("Thiết bị đã bị thu hồi. Hãy tạo mã ghép mới nếu bạn muốn ghép lại."),
  invalid_device_lifetime: t("Thời hạn thiết bị không hợp lệ. Chọn lại rồi thử lại."),
  invalid_request: t("Yêu cầu quản lý thiết bị không hợp lệ. Kiểm tra lựa chọn rồi thử lại."),
  pairing_code_expired: t("Mã ghép đã hết hạn. Hãy tạo mã mới."),
  pairing_code_invalid: t("Mã ghép không hợp lệ. Hãy tạo mã mới."),
  pairing_code_used: t("Mã ghép đã được dùng. Hãy tạo mã mới."),
  revoke_not_confirmed: t("Máy chủ chưa xác nhận đã thu hồi thiết bị. Hãy tải lại rồi thử lại."),
  same_origin: t("Yêu cầu bị từ chối. Hãy tải lại workspace rồi thử lại."),
};

const GENERIC_ERROR = t("Máy chủ chưa thể hoàn tất yêu cầu thiết bị. Hãy thử lại sau.");
const NETWORK_ERROR = t("Không kết nối được với máy chủ. Hãy kiểm tra kết nối rồi thử lại.");

export const initialState = {
  open: false,
  loading: false,
  request: 0,
  devices: [],
  showRevoked: false,
  backendId: null,
  extensionId: null,
  serverUrls: [],
  selectedLifetime: "30d",
  code: null,
  codeExpiresAt: null,
  codeLifetime: null,
  codeRequest: 0,
  pairingFor: null,
  pendingCode: false,
  pendingRevoke: null,
  revokeTarget: null,
  revokeRequest: 0,
  confirmingRevoke: null,
  error: null,
  notice: null,
  copy: null,
  endpointCopy: null,
  endpointCopyRequest: 0,
  ui: null,
};

const done = (state, effects = []) => ({ state, effects });
const same = (state) => done(state);
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const validLifetime = (value) => DEVICE_LIFETIMES.includes(value);
const validTimestamp = (value) => typeof value === "string" && value.length > 0 && Number.isFinite(Date.parse(value));

function compareDevices(left, right) {
  const leftCreated = left.created_at ? Date.parse(left.created_at) : Number.NEGATIVE_INFINITY;
  const rightCreated = right.created_at ? Date.parse(right.created_at) : Number.NEGATIVE_INFINITY;
  if (leftCreated !== rightCreated) return rightCreated - leftCreated;
  if (left.id === right.id) return 0;
  return left.id > right.id ? -1 : 1;
}

const requestInit = (method, body) => ({
  method,
  headers: body === undefined
    ? { Accept: "application/json" }
    : { Accept: "application/json", "Content-Type": "application/json" },
  credentials: "same-origin",
  cache: "no-store",
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

const listEffect = (request) => ({
  type: "fetch",
  key: "extension/devices/list",
  url: "/api/extension/devices",
  init: requestInit("GET"),
  ok: "extension/devices-loaded",
  error: "extension/devices-failed",
  meta: { request },
});

const cancelEffects = [
  { type: "cancel", key: "extension/devices/list" },
  { type: "cancel", key: "extension/devices/code" },
  { type: "cancel", key: "extension/devices/revoke" },
  { type: "cancel", key: "extension/devices/copy" },
  { type: "cancel", key: "extension/devices/select-code" },
  { type: "cancel", key: "extension/devices/endpoint-copy" },
  { type: "cancel", key: "extension/devices/select-endpoint" },
];

function safeDevice(value) {
  if (!isObject(value) || typeof value.id !== "string" || !value.id) return null;
  const status = DEVICE_STATUSES.includes(value.status) ? value.status : "pending";
  return {
    id: value.id,
    device_id: typeof value.device_id === "string" && value.device_id ? value.device_id : null,
    server_url: typeof value.server_url === "string" && value.server_url ? value.server_url : null,
    device_lifetime: validLifetime(value.device_lifetime) ? value.device_lifetime : null,
    device_expires_at: value.device_expires_at === null || validTimestamp(value.device_expires_at) ? value.device_expires_at : null,
    created_at: value.created_at === null || validTimestamp(value.created_at) ? value.created_at : null,
    last_seen_at: value.last_seen_at === null || validTimestamp(value.last_seen_at) ? value.last_seen_at : null,
    revoked_at: value.revoked_at === null || validTimestamp(value.revoked_at) ? value.revoked_at : null,
    status,
  };
}

function normalizeList(body) {
  if (!isObject(body) || !Array.isArray(body.devices)) return { error: t("Máy chủ trả về danh sách thiết bị không hợp lệ.") };
  const devices = body.devices.map(safeDevice);
  if (devices.some((device) => device === null)) return { error: t("Máy chủ trả về danh sách thiết bị không hợp lệ.") };
  devices.sort(compareDevices);
  return {
    backendId: typeof body.backend_id === "string" ? body.backend_id : null,
    extensionId: typeof body.extension_id === "string" ? body.extension_id : null,
    serverUrls: Array.isArray(body.server_urls) ? body.server_urls.filter((url) => typeof url === "string" && url) : [],
    devices,
  };
}

function errorText(error) {
  if (!error || error.status === undefined) return NETWORK_ERROR;
  const payload = isObject(error.body) ? error.body : null;
  const detail = payload && (isObject(payload.error) ? payload.error : isObject(payload.detail) ? payload.detail : null);
  const code = typeof detail?.code === "string" && detail.code ? detail.code : `HTTP_${error.status}`;
  if (ERROR_TEXT[code]) return ERROR_TEXT[code];
  if (error.status === 401) return ERROR_TEXT.authentication_required;
  if (error.status === 404) return ERROR_TEXT.device_not_found;
  if (error.status === 409) return t("Trạng thái thiết bị đã thay đổi. Hãy tải lại danh sách rồi thử lại.");
  if (error.status >= 500) return GENERIC_ERROR;
  return GENERIC_ERROR;
}

function errorState(error) {
  return { code: isObject(error?.body?.error) && typeof error.body.error.code === "string" ? error.body.error.code : "DEVICE_REQUEST_FAILED", text: errorText(error) };
}

function load(state, { clearCode = false } = {}) {
  const request = state.request + 1;
  return done({
    ...state,
    open: true,
    loading: true,
    request,
    error: null,
    notice: null,
    showRevoked: false,
    ...(clearCode
      ? { code: null, codeExpiresAt: null, codeLifetime: null, pairingFor: null, copy: null, endpointCopy: null }
      : {}),
    confirmingRevoke: null,
  }, [listEffect(request)]);
}

function codeEffect(state, deviceId = null) {
  const request = state.codeRequest + 1;
  const body = { device_lifetime: state.selectedLifetime };
  if (deviceId) body.device_record_id = deviceId;
  return done({
    ...state,
    code: null,
    codeExpiresAt: null,
    codeLifetime: null,
    codeRequest: request,
    pairingFor: deviceId,
    pendingCode: true,
    error: null,
    notice: null,
    copy: null,
    ui: null,
  }, [{
    type: "fetch",
    key: "extension/devices/code",
    url: "/api/extension/pairing-codes",
    init: requestInit("POST", body),
    ok: "extension/devices-code-created",
    error: "extension/devices-code-failed",
    meta: { request },
  }]);
}

function normalizeCode(body) {
  if (!isObject(body) || typeof body.code !== "string" || !body.code || !validTimestamp(body.expires_at) || !validLifetime(body.device_lifetime)) {
    return { error: t("Máy chủ trả về mã ghép không hợp lệ.") };
  }
  return { code: body.code, expiresAt: body.expires_at, lifetime: body.device_lifetime };
}

function revokeEffect(state, deviceId) {
  const request = state.revokeRequest + 1;
  return done({
    ...state,
    pendingRevoke: deviceId,
    revokeTarget: deviceId,
    revokeRequest: request,
    confirmingRevoke: null,
    error: null,
    notice: null,
    ui: null,
  }, [{
    type: "fetch",
    key: "extension/devices/revoke",
    url: `/api/extension/devices/${encodeURIComponent(deviceId)}/revoke`,
    init: requestInit("POST"),
    ok: "extension/devices-revoked",
    error: "extension/devices-revoke-failed",
    meta: { deviceId, request },
  }]);
}

function selectDevice(state, id) {
  return state.devices.find((device) => device.id === id) || null;
}

function copyEffect(state) {
  if (!state.code) return [];
  return [{
    type: "call",
    key: "extension/devices/copy",
    name: "extension.devices.write",
    args: [state.code],
    ok: "extension/devices-copy-succeeded",
    error: "extension/devices-copy-failed",
    meta: { request: state.codeRequest },
  }];
}

function endpointIndex(state, action) {
  if (Number.isInteger(action.index)) return action.index;
  if (typeof action.url === "string") return state.serverUrls.indexOf(action.url);
  return -1;
}

function endpointCopyEffect(state, index) {
  const url = state.serverUrls[index];
  if (typeof url !== "string" || !url) return same(state);
  if (state.endpointCopy?.state === "pending" && state.endpointCopy.index === index) return same(state);
  const request = state.endpointCopyRequest + 1;
  return done({
    ...state,
    endpointCopy: { index, url, state: "pending", text: null },
    endpointCopyRequest: request,
    ui: null,
  }, [{
    type: "call",
    key: "extension/devices/endpoint-copy",
    name: "extension.devices.write",
    args: [url],
    ok: "extension/devices-endpoint-copy-succeeded",
    error: "extension/devices-endpoint-copy-failed",
    meta: { index, url, request },
  }]);
}

export function reduce(state, action) {
  switch (action.type) {
    case "provider/dialog-opened":
      return load(state, { clearCode: true });
    case "provider/dialog-closed":
      return done({
        ...state,
        open: false,
        loading: false,
        code: null,
        codeExpiresAt: null,
        codeLifetime: null,
        pairingFor: null,
        pendingCode: false,
        pendingRevoke: null,
        revokeTarget: null,
        confirmingRevoke: null,
        showRevoked: false,
        error: null,
        notice: null,
        copy: null,
        endpointCopy: null,
        ui: null,
      }, cancelEffects);
    case "extension/devices-lifetime-chosen":
      return validLifetime(action.value) ? done({ ...state, selectedLifetime: action.value, error: null }) : same(state);
    case "extension/devices-revoked-visibility-changed":
      return state.open && typeof action.value === "boolean" ? done({ ...state, showRevoked: action.value }) : same(state);
    case "extension/devices-code-requested":
      if (!state.open || state.pendingCode || !validLifetime(state.selectedLifetime)) return same(state);
      return codeEffect(state);
    case "extension/devices-repair-requested":
      if (!state.open || state.pendingCode || !selectDevice(state, action.deviceId)) return same(state);
      return codeEffect(state, action.deviceId);
    case "extension/devices-loaded": {
      if (!state.open || action.meta?.request !== state.request) return same(state);
      const normalized = normalizeList(action.response?.body);
      if (normalized.error) return done({ ...state, loading: false, error: { code: "INVALID_DEVICE_LIST", text: normalized.error } });
      return done({
        ...state,
        loading: false,
        error: null,
        backendId: normalized.backendId,
        extensionId: normalized.extensionId,
        serverUrls: normalized.serverUrls,
        devices: normalized.devices,
        endpointCopy: null,
      });
    }
    case "extension/devices-failed":
      if (!state.open || action.meta?.request !== state.request) return same(state);
      return done({ ...state, loading: false, error: errorState(action.error) });
    case "extension/devices-code-created": {
      if (!state.open || !state.pendingCode || action.meta?.request !== state.codeRequest) return same(state);
      const normalized = normalizeCode(action.response?.body);
      if (normalized.error) return done({ ...state, pendingCode: false, error: { code: "INVALID_PAIRING_CODE", text: normalized.error }, ui: "focus-retry" });
      const request = state.request + 1;
      return done({
        ...state,
        pendingCode: false,
        loading: true,
        request,
        error: null,
        code: normalized.code,
        codeExpiresAt: normalized.expiresAt,
        codeLifetime: normalized.lifetime,
        notice: t("Mã ghép đã tạo. Mã chỉ hiển thị trong Settings và dùng một lần."),
        copy: null,
        ui: "focus-code",
      }, [listEffect(request)]);
    }
    case "extension/devices-code-failed":
      if (!state.open || !state.pendingCode || action.meta?.request !== state.codeRequest) return same(state);
      return done({ ...state, pendingCode: false, error: errorState(action.error), notice: null, ui: "focus-retry" });
    case "extension/devices-copy-requested":
      return state.code ? done({ ...state, copy: null }, copyEffect(state)) : same(state);
    case "extension/devices-copy-succeeded":
      return state.open && action.meta?.request === state.codeRequest ? done({ ...state, copy: { state: "copied", text: t("Đã sao chép mã ghép.") } }) : same(state);
    case "extension/devices-copy-failed":
      if (!state.open || action.meta?.request !== state.codeRequest) return same(state);
      return done({ ...state, copy: { state: "selected", text: t("Không sao chép được. Mã đã được chọn; nhấn Ctrl+C.") }, ui: "select-code" }, [{ type: "call", key: "extension/devices/select-code", name: "extension.devices.select-code" }]);
    case "extension/devices-endpoint-copy-requested":
      if (!state.open) return same(state);
      return endpointCopyEffect(state, endpointIndex(state, action));
    case "extension/devices-endpoint-copy-succeeded":
      if (!state.open || !state.endpointCopy
        || action.meta?.request !== state.endpointCopyRequest
        || action.meta?.index !== state.endpointCopy.index
        || action.meta?.url !== state.endpointCopy.url) return same(state);
      return done({ ...state, endpointCopy: { ...state.endpointCopy, state: "copied", text: t("Đã sao chép endpoint.") }, ui: null });
    case "extension/devices-endpoint-copy-failed":
      if (!state.open || !state.endpointCopy
        || action.meta?.request !== state.endpointCopyRequest
        || action.meta?.index !== state.endpointCopy.index
        || action.meta?.url !== state.endpointCopy.url) return same(state);
      return done({
        ...state,
        endpointCopy: { ...state.endpointCopy, state: "selected", text: t("Không sao chép được. Chọn endpoint rồi nhấn Ctrl+C.") },
      }, [{
        type: "call",
        key: "extension/devices/select-endpoint",
        name: "extension.devices.select-endpoint",
        args: [state.endpointCopy.index],
      }]);
    case "extension/devices-revoke-requested":
      if (!state.open || state.pendingRevoke || !selectDevice(state, action.deviceId)) return same(state);
      return done({ ...state, confirmingRevoke: action.deviceId, error: null, notice: null, ui: null });
    case "extension/devices-revoke-cancelled":
      return action.deviceId === state.confirmingRevoke ? done({ ...state, confirmingRevoke: null, revokeTarget: null }) : same(state);
    case "extension/devices-revoke-confirmed":
      if (action.deviceId !== state.confirmingRevoke || state.pendingRevoke || !selectDevice(state, action.deviceId)) return same(state);
      return revokeEffect(state, action.deviceId);
    case "extension/devices-revoked": {
      const body = action.response?.body;
      if (!state.open || !state.pendingRevoke || action.meta?.request !== state.revokeRequest || action.meta?.deviceId !== state.pendingRevoke) return same(state);
      if (!isObject(body) || body.id !== state.pendingRevoke || body.revoked !== true) {
        return done({ ...state, pendingRevoke: null, error: { code: "REVOKE_NOT_CONFIRMED", text: ERROR_TEXT.revoke_not_confirmed }, ui: "focus-revoke" });
      }
      const deviceId = state.pendingRevoke;
      const request = state.request + 1;
      return done({
        ...state,
        loading: true,
        request,
        pendingRevoke: null,
        revokeTarget: null,
        confirmingRevoke: null,
        error: null,
        notice: t("Thiết bị đã được thu hồi. Danh sách đang được cập nhật."),
        ui: "focus-revoke-status",
        devices: state.devices.map((device) => device.id === deviceId ? { ...device, status: "revoked" } : device),
      }, [listEffect(request)]);
    }
    case "extension/devices-revoke-failed":
      if (!state.open || !state.pendingRevoke || action.meta?.request !== state.revokeRequest || action.meta?.deviceId !== state.pendingRevoke) return same(state);
      return done({ ...state, pendingRevoke: null, confirmingRevoke: null, error: errorState(action.error), ui: "focus-revoke" });
    case "extension/devices-ui-handled":
      return action.ui === state.ui ? done({ ...state, ui: null }) : same(state);
    default:
      return same(state);
  }
}

function readableId(value) {
  if (!value) return null;
  if (value.length <= 18) return value;
  return `${value.slice(0, 10)}…${value.slice(-6)}`;
}

function expiryText(device) {
  if (device.device_lifetime === "never") return LIFETIME_TEXT.never;
  if (device.device_expires_at) return { fmt: "timestamp", value: device.device_expires_at };
  if (device.status === "pending") return t("Chờ client kích hoạt");
  return t("Chưa rõ");
}

export function selectDevices(state) {
  const visibleDevices = state.devices.filter((device) => state.showRevoked || device.status !== "revoked");
  const revokedCount = state.devices.reduce((count, device) => count + (device.status === "revoked" ? 1 : 0), 0);
  return {
    loading: state.loading,
    error: state.error,
    notice: state.notice,
    copy: state.copy,
    endpointCopy: state.endpointCopy,
    ui: state.ui,
    showRevoked: state.showRevoked,
    revokedCount,
    selectedLifetime: state.selectedLifetime,
    lifetimeOptions: DEVICE_LIFETIMES.map((value) => ({ value, label: LIFETIME_TEXT[value] })),
    pendingCode: state.pendingCode,
    codeRequest: state.codeRequest,
    code: state.code,
    codeExpiry: state.codeExpiresAt ? { fmt: "timestamp", value: state.codeExpiresAt } : null,
    codeLifetime: state.codeLifetime ? LIFETIME_TEXT[state.codeLifetime] : null,
    pairingFor: state.pairingFor,
    pendingRevoke: state.pendingRevoke,
    revokeRequest: state.revokeRequest,
    revokeTarget: state.revokeTarget,
    serverUrls: state.serverUrls,
    empty: !state.loading && visibleDevices.length === 0,
    emptyText: state.devices.length > 0 && visibleDevices.length === 0
      ? t("Không có thiết bị nào đang hiển thị. Bật “Hiện thiết bị đã thu hồi” để xem lại.")
      : t("Chưa có thiết bị nào được ghép."),
    rows: visibleDevices.map((device) => ({
      id: device.id,
      device_id: device.device_id,
      recordLabel: device.id,
      deviceLabel: readableId(device.device_id) || t("Đang chờ client"),
      deviceAriaLabel: device.device_id ? t("Mã thiết bị đầy đủ: {id}", { id: device.device_id }) : t("Client chưa cung cấp device ID"),
      server_url: device.server_url,
      lifetime: device.device_lifetime ? LIFETIME_TEXT[device.device_lifetime] : t("Chưa rõ"),
      expires: expiryText(device),
      lastSeen: device.last_seen_at ? { fmt: "timestamp", value: device.last_seen_at } : t("Chưa ghi nhận"),
      status: STATUS_TEXT[device.status],
      statusValue: device.status,
      canRevoke: device.status !== "revoked" && state.pendingRevoke !== device.id,
      revokePending: state.pendingRevoke === device.id,
      confirming: state.confirmingRevoke === device.id,
      canRepair: !state.pendingCode,
    })),
  };
}
