export const PROTOCOL = "asr-extension-v1";
export const API_VERSION = 1;
export const PROTOTYPE = false;
export const EXTENSION_ID = "fgkjjpoeokhajpiagepfcpmgfapilinf";
export const MIME_TYPE = "audio/webm;codecs=opus";
export const DEFAULT_LIMITS = Object.freeze({
  max_chunk_bytes: 8 * 1024 * 1024,
  max_recording_bytes: 4 * 1024 * 1024 * 1024,
  max_local_bytes: 512 * 1024 * 1024,
  max_chunks: 65536,
  max_duration_seconds: 14400
});

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const SAFE_SEGMENT = /^[A-Za-z0-9._~-]+$/;
const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:\d{2})$/;
const STAGE_POLICY_CONTRACT_VERSION = 1;
const STAGE_POLICY_FORMAT_VERSION = 1;

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function validVersion(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function stagePolicyState({ knowledge, admission, contractVersion = null, formatVersion = null, reason = null }) {
  return Object.freeze({ knowledge, admission, contractVersion, formatVersion, reason });
}

export function unknownStagePolicy(reason = "unknown") {
  return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason });
}

// Capability format knowledge is intentionally separate from endpoint identity.
// In particular, only a successfully parsed response with no stage_policy member
// is allowed to mean "legacy". Every other uncertain result fails closed.
export function stagePolicyFromCapabilities(capabilities) {
  if (!isObject(capabilities)
    || capabilities.protocol !== PROTOCOL
    || capabilities.api_version !== API_VERSION
    || capabilities.prototype !== PROTOTYPE) {
    return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason: "malformed_capabilities" });
  }
  if (!Object.hasOwn(capabilities, "features")) {
    return stagePolicyState({ knowledge: "absent", admission: "legacy", reason: "feature_absent" });
  }
  if (!isObject(capabilities.features)) {
    return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason: "malformed_features" });
  }
  if (!Object.hasOwn(capabilities.features, "stage_policy")) {
    return stagePolicyState({ knowledge: "absent", admission: "legacy", reason: "feature_absent" });
  }
  const feature = capabilities.features.stage_policy;
  if (!isObject(feature)) {
    return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason: "malformed_stage_policy" });
  }
  if (feature.supported === false) {
    return stagePolicyState({ knowledge: "unsupported", admission: "unknown", reason: "unsupported_feature" });
  }
  if (feature.supported !== true || !validVersion(feature.contract_version) || !validVersion(feature.format_version)) {
    return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason: "malformed_stage_policy" });
  }
  if (feature.contract_version !== STAGE_POLICY_CONTRACT_VERSION || feature.format_version !== STAGE_POLICY_FORMAT_VERSION) {
    return stagePolicyState({
      knowledge: "unsupported",
      admission: "unknown",
      contractVersion: feature.contract_version,
      formatVersion: feature.format_version,
      reason: "unsupported_format"
    });
  }
  const admission = feature.admission;
  if (!isObject(admission) || !["enabled", "draining", "disabled"].includes(admission.state)
    || admission.format_version !== feature.format_version
    || admission.reason !== null && typeof admission.reason !== "string") {
    return stagePolicyState({ knowledge: "unknown", admission: "unknown", reason: "malformed_admission" });
  }
  return stagePolicyState({
    knowledge: "known",
    admission: admission.state,
    contractVersion: feature.contract_version,
    formatVersion: feature.format_version,
    reason: admission.reason
  });
}

export function isValidDeviceExpiresAt(value) {
  if (value === null) return true;
  if (typeof value !== "string" || !ISO_DATE_TIME.test(value)) return false;
  const [yearText, monthText, dayText] = value.slice(0, 10).split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const calendar = new Date(0);
  calendar.setUTCHours(0, 0, 0, 0);
  calendar.setUTCFullYear(year, month - 1, day);
  return calendar.getUTCFullYear() === year && calendar.getUTCMonth() === month - 1 &&
    calendar.getUTCDate() === day && Number.isFinite(Date.parse(value));
}

export function makeError(code, message, details = undefined) {
  const error = new Error(message);
  error.code = code;
  if (details !== undefined) error.details = details;
  return error;
}

function rawAuthorityHost(input) {
  const schemeEnd = input.indexOf("://");
  if (schemeEnd < 1) return "";
  const authorityStart = schemeEnd + 3;
  let authorityEnd = input.length;
  for (const marker of ["/", "?", "#"]) {
    const index = input.indexOf(marker, authorityStart);
    if (index >= 0) authorityEnd = Math.min(authorityEnd, index);
  }
  const authority = input.slice(authorityStart, authorityEnd);
  if (!authority || authority.includes("@")) {
    throw makeError("invalid_url", "The server authority is not supported.");
  }
  if (authority.startsWith("[")) {
    const close = authority.indexOf("]");
    if (close < 0) throw makeError("invalid_url", "The server authority is not supported.");
    const rest = authority.slice(close + 1);
    if (rest && !/^:\d+$/.test(rest)) throw makeError("invalid_url", "The server port is not supported.");
    return authority.slice(0, close + 1).toLowerCase();
  }
  const firstColon = authority.indexOf(":");
  const lastColon = authority.lastIndexOf(":");
  if (firstColon >= 0) {
    if (firstColon !== lastColon || !/^:\d+$/.test(authority.slice(firstColon))) {
      throw makeError("invalid_url", "The server authority is not supported.");
    }
    return authority.slice(0, firstColon).toLowerCase();
  }
  return authority.toLowerCase();
}

function rawPathname(input) {
  const schemeEnd = input.indexOf("://");
  const pathStart = input.indexOf("/", schemeEnd + 3);
  if (pathStart < 0) return "/";
  const queryStart = input.search(/[?#]/);
  return input.slice(pathStart, queryStart >= 0 ? queryStart : input.length);
}

function normalizedBasePath(pathname) {
  const path = pathname || "/";
  if (path === "/") return "";
  if (!path.startsWith("/") || path.includes("//") || path.includes("%")) {
    throw makeError("invalid_url", "The server base path must use simple unescaped segments.");
  }
  const withoutTrailingSlash = path.endsWith("/") ? path.slice(0, -1) : path;
  const segments = withoutTrailingSlash.slice(1).split("/");
  if (segments.some((segment) => !SAFE_SEGMENT.test(segment))) {
    throw makeError("invalid_url", "The server base path contains an unsupported segment.");
  }
  return `/${segments.join("/")}`;
}

function normalizeAuthorityUrl(input, label) {
  if (typeof input !== "string" || input.trim() !== input || input.length === 0) {
    throw makeError("invalid_url", `Enter a ${label} URL without surrounding spaces.`);
  }
  if (/[\\\u0000-\u0020\u007f]/.test(input) || input.includes("@")) {
    throw makeError("invalid_url", `The ${label} URL cannot contain backslashes or user information.`);
  }
  if (input.includes("?") || input.includes("#")) {
    throw makeError("invalid_url", `The ${label} URL cannot contain a query or fragment.`);
  }

  const rawHost = rawAuthorityHost(input);
  const rawPath = rawPathname(input);
  if (rawPath !== "/") {
    const pathToCheck = rawPath.endsWith("/") ? rawPath.slice(0, -1) : rawPath;
    const segments = pathToCheck.slice(1).split("/");
    if (rawPath.includes("%") || rawPath.includes("//") || segments.some((segment) => segment === "." || segment === ".." || !SAFE_SEGMENT.test(segment))) {
      throw makeError("invalid_url", `The ${label} base path contains an unsupported segment.`);
    }
  }
  const schemeEnd = input.indexOf("://");
  const rawScheme = input.slice(0, schemeEnd).toLowerCase();
  if (rawScheme === "http" && !LOOPBACK_HOSTS.has(rawHost)) {
    throw makeError("invalid_url", "HTTP must use the literal localhost loopback host.");
  }

  let url;
  try {
    url = new URL(input);
  } catch {
    throw makeError("invalid_url", `The ${label} URL is not valid.`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw makeError("invalid_url", `The ${label} URL cannot contain credentials, a query, or a fragment.`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw makeError("invalid_url", "Only HTTP and HTTPS URLs are supported.");
  }

  const hostname = url.hostname.toLowerCase();
  if (url.protocol === "http:" && !LOOPBACK_HOSTS.has(hostname)) {
    throw makeError("invalid_url", "HTTP is limited to localhost, 127.0.0.1, or [::1].");
  }
  if (!hostname || hostname.includes("%") || hostname.endsWith(".")) {
    throw makeError("invalid_url", `The ${label} hostname is not supported.`);
  }

  const basePath = normalizedBasePath(url.pathname);
  return `${url.origin}${basePath}`;
}

export function normalizeServerUrl(input) {
  return normalizeAuthorityUrl(input, "server");
}

export function normalizeWebUrl(input) {
  return normalizeAuthorityUrl(input, "web");
}

function isLiteralLoopbackHttp(url) {
  const parsed = new URL(url);
  return parsed.protocol === "http:" && LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase());
}

function isRemoteAuthority(url) {
  const parsed = new URL(url);
  return !LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase());
}

export function permissionPattern(serverUrl) {
  const normalized = normalizeServerUrl(serverUrl);
  const url = new URL(normalized);
  const path = url.pathname.replace(/\/$/, "");
  return `${url.origin}${path || ""}/*`;
}

export function endpointUrl(serverUrl, relativePath) {
  const base = normalizeServerUrl(serverUrl);
  if (typeof relativePath !== "string" || relativePath.startsWith("/") || relativePath.includes("\\")) {
    throw makeError("invalid_endpoint", "The API path must be relative to the configured base path.");
  }
  const baseWithSlash = `${base}/`;
  const target = new URL(relativePath, baseWithSlash);
  const basePath = new URL(baseWithSlash).pathname.replace(/\/$/, "");
  if (target.origin !== new URL(base).origin || !target.pathname.startsWith(`${basePath}/`)) {
    throw makeError("invalid_endpoint", "The API path escaped the configured base path.");
  }
  return target.href;
}

export function bindingFromCapabilities(serverUrl, capabilities) {
  const base = normalizeServerUrl(serverUrl);
  if (!capabilities || capabilities.protocol !== PROTOCOL || capabilities.prototype !== PROTOTYPE) {
    throw makeError("protocol_mismatch", "The server does not advertise the production extension protocol.");
  }
  if (capabilities.api_version !== API_VERSION) {
    throw makeError("api_version_mismatch", "The extension and server need compatible API versions.");
  }
  let advertisedServer;
  let webUrl;
  try {
    advertisedServer = normalizeServerUrl(capabilities.server_url);
    webUrl = normalizeWebUrl(capabilities.web_url);
  } catch (error) {
    throw makeError("protocol_mismatch", "The server capability URLs are invalid.", error?.message);
  }
  if (advertisedServer !== base) {
    throw makeError("backend_mismatch", "The server capability belongs to another API endpoint.");
  }
  if (isRemoteAuthority(base) && isLiteralLoopbackHttp(webUrl)) {
    throw makeError("invalid_web_url", "A remote API cannot open a loopback web origin.");
  }
  if (typeof capabilities.backend_id !== "string" || capabilities.backend_id.length === 0) {
    throw makeError("protocol_mismatch", "The server did not provide a backend identity.");
  }
  if (!Array.isArray(capabilities.mime_types) || !capabilities.mime_types.includes(MIME_TYPE)) {
    throw makeError("unsupported_media", "The server does not accept the required WebM/Opus format.");
  }
  const limits = { ...DEFAULT_LIMITS, ...(capabilities.limits || {}) };
  for (const name of Object.keys(DEFAULT_LIMITS)) {
    if (!Number.isSafeInteger(limits[name]) || limits[name] <= 0) {
      throw makeError("protocol_mismatch", `The server limit ${name} is invalid.`);
    }
  }
  limits.max_chunk_bytes = Math.min(limits.max_chunk_bytes, DEFAULT_LIMITS.max_chunk_bytes);
  limits.max_recording_bytes = Math.min(limits.max_recording_bytes, DEFAULT_LIMITS.max_recording_bytes);
  limits.max_local_bytes = Math.min(limits.max_local_bytes, DEFAULT_LIMITS.max_local_bytes);
  limits.max_chunks = Math.min(limits.max_chunks, DEFAULT_LIMITS.max_chunks);
  limits.max_duration_seconds = Math.min(limits.max_duration_seconds, DEFAULT_LIMITS.max_duration_seconds);
  const stagePolicy = stagePolicyFromCapabilities(capabilities);
  return Object.freeze({
    serverUrl: base,
    webUrl,
    webOrigin: new URL(webUrl).origin,
    backendId: capabilities.backend_id,
    apiVersion: API_VERSION,
    protocol: PROTOCOL,
    prototype: PROTOTYPE,
    limits,
    mimeTypes: [MIME_TYPE],
    stagePolicy
  });
}

export function sameBinding(left, right) {
  return Boolean(left && right && left.serverUrl === right.serverUrl && left.webUrl === right.webUrl && left.backendId === right.backendId && left.apiVersion === right.apiVersion && left.protocol === right.protocol && left.prototype === right.prototype);
}

export function redactedBinding(binding) {
  if (!binding) return null;
  return {
    serverUrl: binding.serverUrl,
    webUrl: binding.webUrl,
    webOrigin: binding.webOrigin,
    backendId: binding.backendId,
    apiVersion: binding.apiVersion,
    protocol: binding.protocol,
    prototype: false,
    paired: Boolean(binding.paired),
    deviceExpiresAt: binding.deviceExpiresAt || null,
    limits: binding.limits ? { ...binding.limits } : undefined,
    ...(binding.stagePolicy ? { stagePolicy: { ...binding.stagePolicy } } : {})
  };
}

export function validateBackendIdentity(payload, binding) {
  if (!payload || payload.backend_id !== binding.backendId) {
    throw makeError("backend_mismatch", "The response belongs to a different backend.");
  }
  return payload;
}

export function validateSessionResponse(payload, binding) {
  validateBackendIdentity(payload, binding);
  if (typeof payload.id !== "string" || !payload.id || !Number.isSafeInteger(payload.epoch) || payload.epoch < 1 || !Number.isSafeInteger(payload.next_sequence) || payload.next_sequence < 0 || !Number.isSafeInteger(payload.next_offset) || payload.next_offset < 0) {
    throw makeError("protocol_mismatch", "The server session response is invalid.");
  }
  return payload;
}

export function validateChunkAck(payload, expectedSequence, expectedEpoch) {
  if (!payload || payload.sequence !== expectedSequence || payload.epoch !== expectedEpoch || payload.next_sequence !== expectedSequence + 1 || !Number.isSafeInteger(payload.next_offset)) {
    throw makeError("protocol_mismatch", "The server chunk acknowledgement is invalid.");
  }
  return payload;
}

export function canonicalManifest(metas) {
  const sorted = [...metas].sort((a, b) => a.sequence - b.sequence);
  let previousSequence = -1;
  let previousEnd = 0;
  let totalBytes = 0;
  const lines = ["v1\n"];
  for (const item of sorted) {
    if (!Number.isSafeInteger(item.sequence) || item.sequence !== previousSequence + 1 || !Number.isSafeInteger(item.offset) || item.offset !== previousEnd || !Number.isSafeInteger(item.length) || item.length <= 0 || !/^[0-9a-f]{64}$/.test(item.sha256)) {
      throw makeError("manifest_invalid", "The local chunk manifest is not contiguous.");
    }
    lines.push(`${item.sequence}:${item.offset}:${item.length}:${item.sha256}\n`);
    previousSequence = item.sequence;
    previousEnd += item.length;
    totalBytes += item.length;
  }
  return { text: lines.join(""), totalBytes, chunkCount: sorted.length };
}

export function validateReceipt(receipt, session, manifestHash) {
  if (!receipt || !/^[0-9a-f]{64}$/.test(receipt.archive_sha256 || "") || !(session.capture.captureComplete || (session.capture.interrupted && session.upload.recoveryChoice === "save")) || receipt.recording_id !== session.upload.serverSessionId || receipt.backend_id !== session.endpoint.backendId || receipt.epoch !== session.upload.serverEpoch || receipt.chunk_count !== session.capture.chunkCount || receipt.total_bytes !== session.capture.totalBytes || receipt.manifest_hash !== manifestHash) {
    throw makeError("receipt_mismatch", "The server receipt does not match the retained local capture.");
  }
  return receipt;
}

export function resolveMeetingUrl(binding, meetingUrl) {
  if (typeof meetingUrl !== "string" || meetingUrl.includes("\\")) {
    throw makeError("invalid_result_url", "The result link is invalid.");
  }
  let absolute;
  try {
    absolute = new URL(meetingUrl);
  } catch {
    throw makeError("invalid_result_url", "The result link is invalid.");
  }
  const base = normalizeWebUrl(binding.webUrl);
  const baseUrl = new URL(base);
  const basePath = baseUrl.pathname.replace(/\/$/, "");
  const expectedPrefix = `${basePath}/meetings/`;
  const meetingId = absolute.pathname.startsWith(expectedPrefix)
    ? absolute.pathname.slice(expectedPrefix.length)
    : "";
  if (absolute.origin !== baseUrl.origin || absolute.username || absolute.password || absolute.search || absolute.hash || !meetingId || meetingId.includes("/") || !SAFE_SEGMENT.test(meetingId)) {
    throw makeError("invalid_result_url", "The result link is outside the bound web origin.");
  }
  return absolute.href;
}
