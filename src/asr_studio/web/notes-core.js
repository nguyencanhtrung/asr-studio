// Pure core of the notes tab: state, reducer, selectors and the pure helpers the old notes.js held.
// No DOM, fetch, timer, storage, clock, randomness or location: node --test loads this file directly.
// Contract: docs/adr/0010-frontend-reducer-core.md; state table: docs/frontend-design-v1.md §11.7.
//
// User-visible text is data: t(text, params) here only keeps the Vietnamese source string, and the view
// translates it with the real t(). The name matches the call sites in the other scripts so the i18n
// test finds every string. A param is a primitive, a t() result, or an array of those; an array is
// concatenated. A bare string in state is the server's own text.
//
// Time, random ids, stored values and the page address enter through actions (`requestId`, `stored`,
// `href`, `origin`, `hidden`, `mode`); history.js commands, the clipboard, the download and
// history.replaceState leave as `call` effects.

import { errorMessageForCode, isWorkspaceAuthenticationFailure, responseCode, responseMessage } from "./shell-core.js";

export const VIEW_MODE_KEY = "asr-studio:notes-view-mode";
export const ACTIVE_TASK_STATES = ["accepted", "running", "stopping"];
const PROVIDER_IDS = ["chatgpt", "antigravity"];
const POLL_DELAY = 1000;
const POLL_DELAY_HIDDEN = 3000;

const t = (text, params) => (params ? { text, params } : { text });

const TASK_STATE_LABELS = {
  accepted: t("Đã nhận"),
  running: t("Đang xử lý"),
  stopping: t("Đang dừng"),
  succeeded: t("Đã lưu"),
  failed: t("Thất bại"),
  canceled: t("Đã hủy"),
  interrupted: t("Bị gián đoạn"),
};
// Position of each backend stage among the steps listed in #notes-steps (shell.html).
const TASK_STEP_OF_STAGE = {
  accepted: 0,
  starting: 1,
  starting_notes: 1,
  starting_provider: 1,
  preparing_source: 1,
  generating: 2,
  generating_notes: 2,
  chunking: 2,
  merging: 2,
  repairing: 2,
  rendering: 2,
  publishing: 3,
  saving: 3,
};
export const STEP_STATE_LABELS = {
  done: t("Đã xong"),
  current: t("Đang chạy"),
  upcoming: t("Chưa tới"),
  unknown: "",
  failed: TASK_STATE_LABELS.failed,
  canceled: TASK_STATE_LABELS.canceled,
  interrupted: TASK_STATE_LABELS.interrupted,
};
const SOURCE_STATE_LABELS = {
  current: t("Nguồn hiện tại"),
  stale: t("Nguồn đã thay đổi"),
  historical: t("Nguồn lịch sử"),
};
const OUTPUT_LANGUAGE_LABELS = {
  en: "English",
  vi: t("Tiếng Việt"),
};
const PROVIDER_LABELS = { chatgpt: "ChatGPT", antigravity: "Google Antigravity" };
const NEXT_STEP = t("Hệ thống không tự đổi nhà cung cấp, tài khoản hay model. Bạn có thể chọn nhà cung cấp hoặc model khác rồi bắt đầu một lần chạy mới.");
// Each entry names the pinned provider; the operator decides what to do next.
const FAILURE_MESSAGES = {
  provider_disconnected: (name) => t("{name} chưa được kết nối. Hãy kết nối trong phần Kết nối.", { name }),
  reconnect_required: (name) => t("{name} cần được kết nối lại trước khi tạo notes.", { name }),
  connection_changed: (name) => t("Kết nối {name} đã đổi trong lúc chạy nên tác vụ dừng lại.", { name }),
  provider_quota: (name) => t("{name} đã hết hạn mức sử dụng.", { name }),
  model_unavailable: (name) => t("Model đã chọn không dùng được với {name}; gói hiện tại có thể không bao gồm model này.", { name }),
  thinking_level_unavailable: (name) => t("{name} không áp dụng được mức suy nghĩ đã chọn; tác vụ đã dừng trước khi gửi yêu cầu tiếp theo.", { name }),
  provider_unavailable: (name) => t("{name} tạm thời không khả dụng. Hãy kiểm tra kết nối Internet rồi thử lại.", { name }),
  provider_invalid_response: (name) => t("{name} dừng mà không trả về kết quả dùng được nên không có notes mới nào được lưu.", { name }),
  provider_timeout: (name) => t("{name} không phản hồi kịp thời gian.", { name }),
  provider_output_too_large: (name) => t("Phản hồi của {name} vượt quá kích thước đầu ra cho phép.", { name }),
  provider_stream_too_large: (name) => t("Phản hồi của {name} vượt quá kích thước truyền cho phép.", { name }),
  provider_request_too_large: (name) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {name}.", { name }),
  notes_request_too_large: (name) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {name}.", { name }),
  notes_context_budget_exceeded: (name) => t("Transcript vượt quá giới hạn an toàn cho yêu cầu notes của {name}.", { name }),
  notes_token_budget_exceeded: (name) => t("{name} hết hạn mức token trước khi hoàn tất notes. Không có notes mới được lưu.", { name }),
  notes_content_filtered: (name) => t("{name} dừng tạo notes vì bộ lọc nội dung. Không có notes mới được lưu.", { name }),
  notes_incomplete: (name) => t("{name} chưa hoàn tất câu trả lời notes. Không có notes mới được lưu.", { name }),
  notes_runner_protocol_error: (name) => t("Worker notes của {name} không trả về kết quả hoàn chỉnh.", { name }),
  notes_timeout: (name) => t("Tạo notes bằng {name} mất quá nhiều thời gian và đã được dừng. Hãy thử lại.", { name }),
  notes_failed: (name) => t("Tạo notes bằng {name} thất bại. Kiểm tra log của worker rồi thử lại.", { name }),
  notes_publication_failed: (name) => t("Không thể lưu notes của {name} hoàn chỉnh. Kiểm tra bộ nhớ lưu trữ rồi thử lại.", { name }),
  notes_invalid_output: (name) => t("{name} trả về notes không hợp lệ, kể cả sau một lần sửa tự động.", { name }),
  provider_in_use: (name) => t("{name} đang được một tác vụ notes khác sử dụng. Hãy đợi tác vụ hoàn tất trước khi đổi model, kiểm tra hoặc ngắt kết nối.", { name }),
  notes_model_unselected: (name) => t("Chưa chọn model cho {name}.", { name }),
  notes_preference_changed: () => t("Lựa chọn nhà cung cấp hoặc model đã đổi ở nơi khác nên yêu cầu chưa được gửi. Hãy xác nhận lại nhà cung cấp và model đang hiển thị rồi bấm tạo notes lần nữa."),
  login_in_progress: (name) => t("Phiên đăng nhập {name} đang chờ. Hãy hoàn tất hoặc hủy đăng nhập trước khi tạo notes; hệ thống chưa đổi gì.", { name }),
  provider_busy: (name) => t("{name} đang bận với một thao tác kết nối hoặc đăng nhập. Hãy hoàn tất hoặc hủy đăng nhập rồi thử lại; hệ thống chưa đổi gì.", { name }),
};

export const requestStorageKey = (meetingId) => `asr-studio:notes-request:${meetingId}`;
export const pendingRequestStorageKey = (meetingId) => `asr-studio:notes-request-v2:${meetingId}`;
export const outputLanguageStorageKey = (meetingId) => `asr-studio:notes-output-language:${meetingId}`;

function providerLabel(provider) {
  return PROVIDER_LABELS[provider] || t("nhà cung cấp");
}

// Returns "" when the code has no dedicated copy; callers then use a safe local fallback.
export function failureMessage(code, provider) {
  const build = Object.hasOwn(FAILURE_MESSAGES, code) ? FAILURE_MESSAGES[code] : null;
  if (build) return [build(providerLabel(provider)), " ", NEXT_STEP];
  const shared = errorMessageForCode(code, undefined, provider);
  return shared ? [shared, " ", NEXT_STEP] : "";
}

function formatSeconds(value) {
  if (!Number.isFinite(value) || value < 0) return "00:00:00";
  const seconds = Math.floor(value);
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

// mm:ss, or h:mm:ss from one hour on: the time a chip shows and says.
function chipLabel(seconds) {
  const total = Math.floor(seconds);
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const rest = String(total % 60).padStart(2, "0");
  return total >= 3600 ? `${Math.floor(total / 3600)}:${minutes}:${rest}` : `${minutes}:${rest}`;
}

function safeFilename(value) {
  const title = typeof value === "string" ? value : "meeting-notes";
  const cleaned = title
    .normalize("NFKC")
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 100);
  return `${cleaned || "meeting-notes"}.md`;
}

// ---- pure helpers over server payloads ----

const object = (value) => (value && typeof value === "object" ? value : null);

// A runner error {message, status?, body?} as the old NotesApiError: a failed transfer has no status.
function apiError(error, provider) {
  const status = Number.isInteger(error && error.status) ? error.status : 0;
  if (!status) return { message: t("Không kết nối được với máy chủ trên máy này."), status: 0, payload: null };
  const payload = object(error.body);
  return { code: responseCode(status, payload), message: responseMessage(status, payload, provider), status, payload };
}

const DEFINITIVE_ADMISSION_CODES = new Set([
  "meeting_task_active",
  "notes_source_locked",
  "provider_busy",
  "provider_disconnected",
  "provider_in_use",
  "reconnect_required",
  "worker_unavailable",
  "processing_unavailable",
  "system_busy",
  "notes_model_unselected",
  "notes_preference_changed",
  "login_in_progress",
]);
const isDefinitive = (error) => (error.status >= 400 && error.status < 500) || DEFINITIVE_ADMISSION_CODES.has(error.code);

function unwrappedNotesPayload(payload) {
  return payload && payload.notes && typeof payload.notes === "object" ? payload.notes : payload;
}

function normalizeCitations(citations) {
  if (!Array.isArray(citations)) return [];
  return citations
    .filter((entry) => entry
      && typeof entry.section === "string"
      && typeof entry.text === "string"
      && Array.isArray(entry.sources))
    .map((entry) => ({
      section: entry.section,
      text: entry.text,
      sources: entry.sources.filter((source) => source
        && typeof source.id === "string"
        && source.id
        && Number.isFinite(source.start_seconds)
        && source.start_seconds >= 0),
    }));
}

function normalizeRevision(revision, meetingId) {
  if (
    !revision
    || typeof revision.id !== "string"
    || !revision.id
    || revision.meeting_id !== meetingId
    || typeof revision.source_snapshot_id !== "string"
    || typeof revision.markdown !== "string"
    || !Number.isInteger(revision.edit_version)
    || !["current", "stale", "historical"].includes(revision.source_state)
  ) return null;
  return {
    ...revision,
    output_language: ["en", "vi"].includes(revision.output_language) ? revision.output_language : "en",
    citations: normalizeCitations(revision.citations),
    display_citations: normalizeCitations(revision.display_citations || revision.citations),
    // The source links of the saved Markdown that open no source; a server without the field names none.
    warnings: Array.isArray(revision.warnings)
      ? revision.warnings.filter((warning) => warning && typeof warning.href === "string")
      : [],
  };
}

const isNotesTask = (task) => Boolean(task && (task.kind === undefined || task.kind === "notes"));
const isActiveStatus = (status) => ACTIVE_TASK_STATES.includes(status);
const activeTask = (data) => Boolean(data && data.task && isActiveStatus(data.task.status));

// null when the payload is not the published notes shape.
function parseNotes(meetingId, payload) {
  const notes = unwrappedNotesPayload(payload);
  if (!notes || !Array.isArray(notes.revisions) || typeof notes.source_locked !== "boolean") return null;
  return {
    revisions: notes.revisions.map((revision) => normalizeRevision(revision, meetingId)).filter(Boolean),
    task: isNotesTask(notes.task) ? notes.task || null : null,
    sourceLocked: notes.source_locked,
  };
}

// {task} or {error}: the start and cancel answers name the task the same way.
function parseTask(payload) {
  const task = payload && payload.task && typeof payload.task === "object" ? payload.task : payload;
  if (!task || typeof task.id !== "string" || typeof task.status !== "string") {
    return { error: t("Máy chủ không trả về task notes hợp lệ.") };
  }
  if (task.kind !== "notes") return { error: t("Task được nhận không có kind notes theo API đã công bố.") };
  return { task };
}

function parseDetail(payload) {
  const note = payload && payload.note && typeof payload.note === "object" ? payload.note : payload;
  if (
    !note
    || typeof note.id !== "string"
    || typeof note.meeting_id !== "string"
    || typeof note.source_snapshot_id !== "string"
    || typeof note.markdown !== "string"
    || !Number.isInteger(note.edit_version)
    || !["current", "stale", "historical"].includes(note.source_state)
  ) return null;
  return note;
}

function normalizeCatalog(payload) {
  const models = payload && Array.isArray(payload.models) ? payload.models : [];
  return {
    models: models
      .filter((item) => item && typeof item.id === "string" && item.id)
      .map((item) => ({
        id: item.id,
        display_name: typeof item.display_name === "string" && item.display_name ? item.display_name : item.id,
        reasoning_efforts: Array.isArray(item.reasoning_efforts) ? item.reasoning_efforts : null,
      })),
  };
}

function catalogUnavailableReason(provider, status) {
  const name = providerLabel(provider);
  if (status === "disconnected") return t("{name} chưa được kết nối nên chưa tải được danh sách model.", { name });
  if (status === "reconnect_required") return t("{name} cần được kết nối lại nên chưa tải được danh sách model.", { name });
  return "";
}

const validRequestId = (value) => typeof value === "string" && /^[A-Za-z0-9._:-]{8,128}$/.test(value);

export function normalizePendingRequest(value, { legacy = false } = {}) {
  if (typeof value !== "string" || !value) return null;
  let parsed = value;
  let isJson = false;
  try {
    parsed = JSON.parse(value);
    isJson = true;
  } catch {
    // Legacy storage contains a plain request ID.
  }
  if (isJson && parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    if (!validRequestId(parsed.request_id) || !["en", "vi"].includes(parsed.output_language)) return null;
    if (parsed.provider !== undefined && !PROVIDER_IDS.includes(parsed.provider)) return null;
    // Stored requests from before provider choice were always ChatGPT requests.
    const result = {
      request_id: parsed.request_id,
      output_language: parsed.output_language,
      provider: parsed.provider || "chatgpt",
    };
    const expected = ["expected_generation_id", "expected_source_version", "expected_metadata_version"];
    const hasExpectedFields = expected.every((key) => Object.prototype.hasOwnProperty.call(parsed, key));
    const hasNoExpectedFields = expected.every((key) => !Object.prototype.hasOwnProperty.call(parsed, key));
    if (hasExpectedFields) {
      if (
        typeof parsed.expected_generation_id !== "string"
        || !parsed.expected_generation_id
        || !Number.isInteger(parsed.expected_source_version)
        || parsed.expected_source_version < 1
        || !Number.isInteger(parsed.expected_metadata_version)
        || parsed.expected_metadata_version < 1
      ) return null;
      result.expected_generation_id = parsed.expected_generation_id;
      result.expected_source_version = parsed.expected_source_version;
      result.expected_metadata_version = parsed.expected_metadata_version;
      for (const key of ["expected_model", "expected_reasoning_effort"]) {
        if (typeof parsed[key] === "string" && parsed[key]) result[key] = parsed[key];
      }
    } else if (!hasNoExpectedFields && !legacy) {
      return null;
    }
    return result;
  }
  const requestId = isJson && typeof parsed === "string" ? parsed : value;
  if (!validRequestId(requestId)) return null;
  return { request_id: requestId, output_language: "en", provider: "chatgpt" };
}

function isCompletePendingRequest(pending) {
  return Boolean(
    pending
    && typeof pending.expected_generation_id === "string"
    && Number.isInteger(pending.expected_source_version)
    && Number.isInteger(pending.expected_metadata_version),
  );
}

// ---- source links ----

// The address of a saved source: the Transcript tab, where its card sits under the player.
export function sourceHref(meetingId, snapshotId, segmentId, group = "") {
  return `/meetings/${encodeURIComponent(meetingId)}?notes_source=${encodeURIComponent(snapshotId)}&segment=${encodeURIComponent(segmentId)}${group ? `&group=${encodeURIComponent(group)}` : ""}#transcript`;
}

// A source link is spelled with the literal notes_source=; the server names the broken ones by that spelling.
export function sourceReference(rawHref, origin, expectedMeetingId = null, expectedSnapshotId = null) {
  if (typeof rawHref !== "string" || !rawHref.includes("notes_source=")) return null;
  if (!rawHref.startsWith("/") || rawHref.startsWith("//")) return null;
  let url;
  try {
    url = new URL(rawHref, origin);
  } catch {
    return null;
  }
  if (
    url.origin !== origin
    || (url.hash && url.hash !== "#transcript")
    || url.username
    || url.password
  ) return null;
  const pathMatch = url.pathname.match(/^\/meetings\/([^/]+)$/);
  if (!pathMatch) return null;
  let meetingId;
  try {
    meetingId = decodeURIComponent(pathMatch[1]);
  } catch {
    return null;
  }
  if (!meetingId || meetingId.includes("/")) return null;
  const params = [...url.searchParams.entries()];
  if (
    (params.length !== 2 && !(params.length === 3 && params[2][0] === "group" && params[2][1]))
    || params[0][0] !== "notes_source"
    || params[1][0] !== "segment"
    || !params[0][1]
    || !params[1][1]
  ) return null;
  const snapshotId = params[0][1];
  const segmentId = params[1][1];
  if (
    (expectedMeetingId && meetingId !== expectedMeetingId)
    || (expectedSnapshotId && snapshotId !== expectedSnapshotId)
  ) return null;
  return { meetingId, snapshotId, segmentId, ...(url.searchParams.has("group") ? { group: url.searchParams.get("group") } : {}), href: sourceHref(meetingId, snapshotId, segmentId, url.searchParams.get("group")) };
}

// The source named by the page address (`href` is the full URL), or null.
export function sourceFromHref(href) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  const path = url.pathname.match(/^\/meetings\/([^/]+)\/?$/);
  if (!path) return null;
  const params = [...url.searchParams.entries()];
  if (!params.some(([key]) => key === "notes_source" || key === "segment")) return null;
  let meetingId;
  try {
    meetingId = decodeURIComponent(path[1]);
  } catch {
    return null;
  }
  if (
    !meetingId
    || meetingId.includes("/")
    || (params.length !== 2 && !(params.length === 3 && params[2][0] === "group" && params[2][1]))
    || params[0][0] !== "notes_source"
    || params[1][0] !== "segment"
    || !params[0][1]
    || !params[1][1]
  ) return null;
  // The hash names a tab, not a source: #transcript (what a chip links to), #notes or none all open it.
  return {
    meetingId,
    snapshotId: params[0][1],
    segmentId: params[1][1],
    ...(url.searchParams.has("group") ? { group: url.searchParams.get("group") } : {}),
    href: sourceHref(meetingId, params[0][1], params[1][1], url.searchParams.get("group")),
  };
}

const referenceKey = (reference) => `${reference.meetingId}\u0000${reference.snapshotId}\u0000${reference.segmentId}${reference.group ? `\u0000${reference.group}` : ""}`;

// ---- Markdown shaping ----

// The citation sources that still belong to each bullet, by line index. A bullet keeps its chips only
// while its section heading and its text are exactly what the notes were made with, so an edited bullet
// shows none and never another bullet's. Each entry is used once, in order. Identical bullets cannot
// be told apart, so a text keeps its chips only while the notes hold as many bullets with it as they
// were made with: once one was edited or removed, or another was edited into a copy, none keeps a chip.
function citedBullets(lines, citations) {
  const found = new Map();
  if (!citations.length) return found;
  const keyOf = (section, text) => `${section}\u0000${text}`;
  const groups = new Map();
  for (const entry of citations) {
    const key = keyOf(entry.section, entry.text);
    if (!groups.has(key)) groups.set(key, { size: 0, queue: [] });
    const group = groups.get(key);
    group.size += 1;
    group.queue.push(entry.sources);
  }
  const bullets = [];
  const counts = new Map();
  let section = "";
  lines.forEach((line, index) => {
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      section = heading[2].trim();
      return;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (!bullet) return;
    const key = keyOf(section, bullet[1].trim());
    bullets.push({ index, key });
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  for (const { index, key } of bullets) {
    const group = groups.get(key);
    if (!group || counts.get(key) !== group.size) continue;
    found.set(index, group.queue.shift());
  }
  return found;
}

// Inline nodes of one Markdown line: text, strong, code, em, link {…ids}, invalid {text, reason}.
function inlineNodes(text, revision, origin) {
  const token = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|`([^`]+)`|\*([^*]+)\*/g;
  const unescape = (value) => value.replace(/\\([\\`*_{}\[\]<>()#!|~+\-.>])/g, "$1");
  const nodes = [];
  let previous = 0;
  let match;
  while ((match = token.exec(text))) {
    if (match.index > previous) nodes.push({ kind: "text", text: unescape(text.slice(previous, match.index)) });
    if (match[1] !== undefined) {
      const source = sourceReference(match[2], origin, revision.meeting_id, revision.source_snapshot_id);
      if (revision.warnings.some((warning) => warning.href === match[2])) {
        // Typed with a source the saved snapshot lacks: shown as text, never opened or repaired.
        nodes.push({ kind: "invalid", text: unescape(match[1]), reason: t(" (liên kết nguồn không hợp lệ)") });
      } else if (source) {
        nodes.push({
          kind: "link",
          text: unescape(match[1]),
          href: source.href,
          meetingId: source.meetingId,
          snapshotId: source.snapshotId,
          segmentId: source.segmentId,
          label: t("Mở nguồn transcript tại {time}", { time: unescape(match[1]) }),
        });
      } else {
        nodes.push({ kind: "text", text: unescape(match[0]) });
      }
    } else if (match[3] !== undefined) {
      nodes.push({ kind: "strong", text: unescape(match[3]) });
    } else if (match[4] !== undefined) {
      nodes.push({ kind: "code", text: unescape(match[4]) });
    } else {
      nodes.push({ kind: "em", text: unescape(match[5]) });
    }
    previous = token.lastIndex;
  }
  if (previous < text.length) nodes.push({ kind: "text", text: unescape(text.slice(previous)) });
  return nodes;
}

function citationChip(revision, source, group = "") {
  const count = source.members?.length || 1;
  const label = count > 1 ? `${chipLabel(source.start_seconds)}–${chipLabel(source.end_seconds)}` : chipLabel(source.start_seconds);
  return {
    href: sourceHref(revision.meeting_id, revision.source_snapshot_id, source.id, group || (source.members?.length > 1 ? source.members.map((member) => member.id).join(",") : "")),
    meetingId: revision.meeting_id,
    snapshotId: revision.source_snapshot_id,
    segmentId: source.id,
    label: count > 1 ? t("{label} · {count} đoạn", { label, count }) : label,
    aria: count > 1 ? t("Nghe lại nhóm nguồn {label}, {count} đoạn", { label, count }) : t("Nghe lại đoạn nguồn lúc {label}", { label }),
  };
}

// Blocks of the preview: {kind: heading, level, nodes} | {kind: list, items: [{nodes, chips}]} | {kind: paragraph, lines: [nodes]}.
function markdownBlocks(markdown, revision, mode, origin) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  // The panel's own heading is an h3, so the shallowest heading in the notes becomes an h4.
  const shallowest = Math.min(
    ...lines.map((line) => line.match(/^(#{1,6})\s+(.+)$/)).filter(Boolean).map((match) => match[1].length),
  );
  // Chips belong to Xem; the Markdown, Viết, the copy and the download never carry a reference.
  const cited = mode === "view" ? citedBullets(lines, revision.display_citations || revision.citations || []) : new Map();
  const blocks = [];
  let list = null;
  let paragraph = null;
  for (const [index, line] of lines.entries()) {
    if (!line.trim()) {
      paragraph = null;
      list = null;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      paragraph = null;
      list = null;
      blocks.push({ kind: "heading", level: Math.min(6, heading[1].length - shallowest + 4), nodes: inlineNodes(heading[2], revision, origin) });
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      paragraph = null;
      if (!list) {
        list = { kind: "list", items: [] };
        blocks.push(list);
      }
      list.items.push({
        nodes: inlineNodes(bullet[1], revision, origin),
        chips: (cited.get(index) || []).map((source) => citationChip(revision, source)),
      });
      continue;
    }
    list = null;
    if (!paragraph) {
      paragraph = { kind: "paragraph", lines: [] };
      blocks.push(paragraph);
    }
    paragraph.lines.push(inlineNodes(line, revision, origin));
  }
  return blocks;
}

// ---- state ----

const EMPTY_NOTES = Object.freeze({
  revisions: [],
  task: null,
  sourceLocked: false,
  sourceLockKnown: false,
  known: false, // the server has answered a notes read (or failed one) since the meeting opened
  selectedNoteId: null,
  starting: false,
  canceling: false,
  saving: {}, // revision id -> true while its PATCH runs
  readError: "",
  runError: "",
  runErrorUncertain: false,
  actionMessage: "",
});

export const initialState = {
  mode: "view",
  hidden: false, // the page is in the background: polls slow down
  origin: "",
  view: "",
  meetingId: null,
  meeting: null,
  transcribing: false,
  speakerNames: [], // history's default names for the current generation, sent with each context
  // Provider choice for the next run; catalogs[provider] is null until a catalog loads.
  choice: {
    loaded: false,
    loadedFor: "",
    loading: false,
    loadId: 0,
    load: { phase: "idle", providers: null, preferences: null, waiting: 0, meetingId: "" },
    localProvider: null,
    preferences: { provider: null, models: {}, reasoning_effort: "default", reasoning_efforts: {} },
    status: { chatgpt: "disconnected", antigravity: "disconnected" },
    catalogs: { chatgpt: null, antigravity: null },
    catalogErrors: { chatgpt: "", antigravity: "" },
    saving: false,
    saveId: 0,
    message: "",
    open: false, // "Đổi…" is showing the selects and the description of what is sent
    focus: 0, // one-shot focus request for the model select
  },
  notes: {}, // meeting id -> EMPTY_NOTES shape
  drafts: {}, // revision id -> { value, baselineMarkdown, expectedEditVersion, conflict }
  outputLanguages: {},
  pending: {}, // meeting id -> the request waiting for its answer
  // The step each notes task was last seen running, by task id: the backend overwrites
  // the stage when a run fails, is canceled or is interrupted.
  taskSteps: {},
  snapshots: {}, // saved transcript snapshots by id
  readId: 0, // id of the latest notes read
  authoritativeId: 0, // counts reads of the saved copy for copy and download
  poll: { token: 0, scheduled: false },
  source: { id: 0, key: "", current: null, visible: false, status: "", focus: 0 },
};

const entryOf = (state, meetingId) => state.notes[meetingId] ?? EMPTY_NOTES;
const currentEntry = (state) => (state.meetingId ? entryOf(state, state.meetingId) : null);
const patchNotes = (state, meetingId, patch) => ({
  ...state,
  notes: { ...state.notes, [meetingId]: { ...entryOf(state, meetingId), ...patch } },
});
const patchChoice = (state, patch) => ({ ...state, choice: { ...state.choice, ...patch } });
const patchSource = (state, patch) => ({ ...state, source: { ...state.source, ...patch } });
const pendingOf = (state, meetingId = state.meetingId) => (meetingId ? state.pending[meetingId] ?? null : null);
const languageOf = (state, meetingId) => state.outputLanguages[meetingId] ?? "en";

const HOST = "notes/host";
const call = (name, args, extra) => ({ type: "call", key: extra ? extra.key : HOST, name, args, ...extra });
const storeBoth = (key, value) => ["local", "session"].map((area) => ({ type: "storage", key, area, op: "write", value }));
const removeBoth = (key) => ["local", "session"].map((area) => ({ type: "storage", key, area, op: "remove" }));

function request(key, url, method, body, op, meta) {
  const init = {
    method,
    headers: method === "GET" ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
  };
  if (body !== undefined) init.body = body;
  return { type: "fetch", key, url, init, ok: "notes/response", error: "notes/request-failed", meta: { op, ...meta } };
}

const notesEndpoint = (meetingId) => `/api/meetings/${encodeURIComponent(meetingId)}/notes`;
const noteEndpoint = (noteId) => `/api/notes/${encodeURIComponent(noteId)}`;

// A transition is { state, effects }; `then` runs the next step on the state the last one left.
const out = (state, effects = []) => ({ state, effects });
function then(result, step) {
  const next = step(result.state);
  return { state: next.state, effects: [...result.effects, ...(next.effects ?? [])] };
}

// ---- derived values ----

export function currentGeneration(meeting) {
  const generation = meeting && meeting.transcription && meeting.transcription.generation;
  return generation && generation.status === "complete" ? generation : null;
}

function transcriptionBusy(state, meeting) {
  const task = meeting && meeting.transcription && meeting.transcription.task;
  return state.transcribing || Boolean(task && isActiveStatus(task.status));
}

function hasRequestVersions(meeting, generation) {
  return Boolean(
    generation
    && Number.isInteger(generation.source_version)
    && Number.isInteger(meeting && meeting.metadata_version),
  );
}

const savedModel = (state, provider) => {
  const model = state.choice.preferences.models[provider];
  return typeof model === "string" ? model : "";
};
const catalogModels = (state, provider) => {
  const catalog = state.choice.catalogs[provider];
  return catalog ? catalog.models : null;
};
export function modelName(state, provider, modelId) {
  const models = catalogModels(state, provider);
  const model = models && models.find((item) => item.id === modelId);
  return model ? model.display_name : modelId;
}
const effortLabel = (effort) => (effort === "medium" ? "Medium" : "High");

function effortFor(state, provider, modelId) {
  if (provider === "antigravity") {
    const saved = state.choice.preferences.reasoning_efforts.antigravity;
    return saved === "medium" ? "medium" : "high";
  }
  if (provider !== "chatgpt") return "default";
  const effort = state.choice.preferences.reasoning_effort;
  const models = catalogModels(state, provider);
  const model = models && models.find((item) => item.id === modelId);
  if (model && Array.isArray(model.reasoning_efforts) && !model.reasoning_efforts.includes(effort)) {
    return "default";
  }
  return typeof effort === "string" ? effort : "default";
}

// The provider for the next run: a pending retry keeps its provider, then the operator's choice,
// the last saved provider, the only connected provider, and finally ChatGPT.
export function selectedProvider(state, pending) {
  const choice = state.choice;
  if (pending && PROVIDER_IDS.includes(pending.provider)) return pending.provider;
  if (PROVIDER_IDS.includes(choice.localProvider)) return choice.localProvider;
  if (PROVIDER_IDS.includes(choice.preferences.provider)) return choice.preferences.provider;
  const connected = PROVIDER_IDS.filter((id) => choice.status[id] === "connected");
  return connected.length === 1 ? connected[0] : "chatgpt";
}

// "ok" when the saved model is usable, otherwise why the operator must act first.
function modelReadiness(state, provider) {
  const model = savedModel(state, provider);
  const models = catalogModels(state, provider);
  if (models === null) return { ready: true, model, missing: false };
  if (!model) return { ready: false, model, missing: false };
  return { ready: models.some((item) => item.id === model), model, missing: true };
}

// A new run needs the saved choice loaded for this meeting and a model to show for the provider.
function newRunChoiceReady(state, meeting) {
  const choice = state.choice;
  if (!choice.loaded || choice.loadedFor !== meeting.id) return false;
  const readiness = modelReadiness(state, selectedProvider(state, null));
  return readiness.ready && Boolean(readiness.model);
}

function canStartNotes(state) {
  const meeting = state.meeting;
  const data = currentEntry(state);
  const generation = currentGeneration(meeting);
  if (
    !meeting
    || !data
    || !generation
    || transcriptionBusy(state, meeting)
    || !hasRequestVersions(meeting, generation)
    || !data.sourceLockKnown
    || data.sourceLocked
    || activeTask(data)
    || data.starting
    || state.choice.saving
  ) return false;
  const pending = pendingOf(state, meeting.id);
  return Boolean(
    (!state.choice.loading || isCompletePendingRequest(pending))
    && (pending || newRunChoiceReady(state, meeting)),
  );
}

// The chosen provider is not connected or has no usable model: Connections is the next step, not a run.
// A run in progress keeps its own button; the choice only matters again once it ends.
function needsConnection(state, data, provider, readiness, pending) {
  const choice = state.choice;
  return choice.loaded
    && !pending
    && !activeTask(data)
    && !(data && data.starting)
    && choice.status[provider] !== "connected";
}

function needsModelChoice(state, data, provider, readiness, pending) {
  const choice = state.choice;
  const models = catalogModels(state, provider);
  return choice.loaded
    && !pending
    && !activeTask(data)
    && !(data && data.starting)
    && choice.status[provider] === "connected"
    && models !== null
    && (!readiness.ready || !readiness.model);
}

function selectedRevision(state) {
  const data = currentEntry(state);
  return (data && data.revisions.find((item) => item.id === data.selectedNoteId)) || null;
}

const draftIsDirty = (draft) => Boolean(draft && draft.value !== draft.baselineMarkdown);

// Dirty drafts make the page ask before it unloads (history.js adds its own transcript drafts).
export function hasUnsavedDraft(state) {
  return Object.values(state.drafts).some(draftIsDirty);
}

// What history.js shows about the notes: count, task status, and whether the server has answered.
function selectSummary(state) {
  const meeting = state.meeting;
  if (!meeting) return null;
  const data = currentEntry(state);
  return [meeting.id, data ? data.revisions.length : 0, (data && data.task && data.task.status) || undefined, Boolean(data && data.known)];
}

// ---- drafts and per-action settling ----

function reconcileDraft(draft, revision) {
  if (!draft) {
    return { value: revision.markdown, baselineMarkdown: revision.markdown, expectedEditVersion: revision.edit_version, conflict: null };
  }
  if (draft.value === draft.baselineMarkdown) {
    if (draft.value === revision.markdown && draft.expectedEditVersion === revision.edit_version && draft.conflict === null) return draft;
    return { value: revision.markdown, baselineMarkdown: revision.markdown, expectedEditVersion: revision.edit_version, conflict: null };
  }
  if (revision.edit_version !== draft.expectedEditVersion) {
    const conflict = draft.conflict;
    if (conflict && conflict.markdown === revision.markdown && conflict.editVersion === revision.edit_version) return draft;
    return { ...draft, conflict: { markdown: revision.markdown, editVersion: revision.edit_version } };
  }
  return draft;
}

// What the old view did while rendering: the selected revision gets its draft made or reconciled with the
// saved text, and a running task's step is remembered. Idempotent, so it runs after every action.
function settle(state) {
  const id = state.meetingId;
  const data = id && state.notes[id];
  if (!data) return state;
  let next = state;
  const revision = data.revisions.find((item) => item.id === data.selectedNoteId);
  if (revision) {
    const draft = reconcileDraft(state.drafts[revision.id], revision);
    if (draft !== state.drafts[revision.id]) next = { ...next, drafts: { ...next.drafts, [revision.id]: draft } };
  }
  const task = data.task;
  if (task && isActiveStatus(task.status) && Object.hasOwn(TASK_STEP_OF_STAGE, task.stage)) {
    const step = TASK_STEP_OF_STAGE[task.stage];
    if (next.taskSteps[task.id] !== step) next = { ...next, taskSteps: { ...next.taskSteps, [task.id]: step } };
  }
  return next;
}

function replaceRevision(data, updated) {
  const normalized = normalizeRevision(updated, updated && updated.meeting_id);
  if (!normalized) return data.revisions;
  const index = data.revisions.findIndex((item) => item.id === normalized.id);
  if (index < 0) return [normalized, ...data.revisions];
  return data.revisions.map((item, at) => (at === index ? normalized : item));
}

const patchDraft = (state, revisionId, patch) => ({
  ...state,
  drafts: { ...state.drafts, [revisionId]: { ...state.drafts[revisionId], ...patch } },
});

// ---- polling and reading ----

function readNotes(state, meetingId, poll = null, broadcast = false) {
  const id = state.readId + 1;
  return out({ ...state, readId: id }, [request(`notes/read/${id}`, notesEndpoint(meetingId), "GET", undefined, "read", { id, meetingId, poll, broadcast })]);
}

function stopPolling(state) {
  return out({ ...state, poll: { token: state.poll.token + 1, scheduled: false } }, [{ type: "cancel", key: "notes/poll" }]);
}

function schedulePoll(state, meetingId, delay = state.hidden ? POLL_DELAY_HIDDEN : POLL_DELAY, token = state.poll.token) {
  return out({ ...state, poll: { ...state.poll, scheduled: true } }, [
    { type: "schedule", key: "notes/poll", delay, action: { type: "notes/poll-due", meetingId, token } },
  ]);
}

function startPolling(state, meetingId, task) {
  if (!task || !isActiveStatus(task.status)) return state.meetingId === meetingId ? stopPolling(state) : out(state);
  if (meetingId !== state.meetingId) return out(state);
  return state.poll.scheduled ? out(state) : schedulePoll(state, meetingId);
}

function clearPending(state, meetingId) {
  const pending = { ...state.pending };
  delete pending[meetingId];
  return out({ ...state, pending }, [...removeBoth(pendingRequestStorageKey(meetingId)), ...removeBoth(requestStorageKey(meetingId))]);
}

function savePending(state, meetingId, pending) {
  return out({ ...state, pending: { ...state.pending, [meetingId]: pending } }, storeBoth(pendingRequestStorageKey(meetingId), JSON.stringify(pending)));
}

const lockEffect = (meetingId, locked, known) => call("history.setNotesSourceLock", [meetingId, locked, known]);
const meetingChangeEffect = (meetingId, reason) => call("meeting.change", [meetingId, reason]);

function revisionsChanged(before, parsed) {
  if (before.revisions.length !== parsed.revisions.length) return true;
  return before.revisions.some((revision, index) => {
    const next = parsed.revisions[index];
    return !next
      || revision.id !== next.id
      || revision.edit_version !== next.edit_version
      || revision.markdown !== next.markdown
      || revision.source_state !== next.source_state;
  });
}

function notesChanged(before, parsed) {
  return before.sourceLockKnown !== true
    || before.sourceLocked !== parsed.sourceLocked
    || revisionsChanged(before, parsed);
}

function installNotes(state, meetingId, parsed, broadcast = false) {
  const before = entryOf(state, meetingId);
  const wasActive = activeTask(before);
  let result = out(patchNotes(state, meetingId, { revisions: parsed.revisions, task: parsed.task }));
  // A finished run frees its provider, so catalogs that were blocked can load again.
  if (wasActive && !activeTask(entryOf(result.state, meetingId))) {
    result = then(result, (s) => loadChoice(patchChoice(s, { message: "" }), meetingId));
  }
  if (!result.state.choice.loaded && !result.state.choice.loading) result = then(result, (s) => loadChoice(s, meetingId));
  result = then(result, (s) => {
    const data = entryOf(s, meetingId);
    return out(patchNotes(s, meetingId, {
      sourceLocked: parsed.sourceLocked,
      sourceLockKnown: true,
      known: true,
      readError: "",
      selectedNoteId: !data.selectedNoteId && parsed.revisions.length ? parsed.revisions[0].id : data.selectedNoteId,
    }));
  });
  const pending = pendingOf(result.state, meetingId);
  const task = entryOf(result.state, meetingId).task;
  if (task && pending && (task.request_id === pending.request_id || isActiveStatus(task.status))) {
    result = then(result, (s) => clearPending(s, meetingId));
  }
  result.effects.push(lockEffect(meetingId, parsed.sourceLocked, true));
  if (broadcast && notesChanged(before, parsed)) result.effects.push(meetingChangeEffect(meetingId, "notes"));
  return then(result, (s) => startPolling(s, meetingId, entryOf(s, meetingId).task));
}

function readFailed(state, meetingId, poll, message) {
  const data = entryOf(state, meetingId);
  const effects = data.sourceLockKnown ? [] : [lockEffect(meetingId, false, false)];
  const result = out(patchNotes(state, meetingId, { known: true, readError: message }), effects);
  return poll === null ? result : then(result, (s) => schedulePoll(s, meetingId, POLL_DELAY_HIDDEN, poll));
}

// ---- provider choice ----

function loadChoice(state, meetingId) {
  const id = state.choice.loadId + 1;
  return out(
    patchChoice(state, {
      loadId: id,
      loading: true, // a run cannot start while the saved choice and catalogs are still loading
      loaded: false,
      load: { phase: "base", providers: null, preferences: null, waiting: 0, meetingId },
    }),
    [
      request(`notes/choice/providers/${id}`, "/api/providers", "GET", undefined, "choice-providers", { id }),
      request(`notes/choice/preferences/${id}`, "/api/notes-preferences", "GET", undefined, "choice-preferences", { id }),
    ],
  );
}

function choiceFailed(state) {
  return out(
    patchChoice(state, {
      loaded: false,
      loading: false,
      load: { ...state.choice.load, phase: "idle" },
      message: t("Chưa đọc được lựa chọn nhà cung cấp và model đã lưu. Chưa thể tạo notes mới cho tới khi tải lại được."),
    }),
  );
}

function applyChoiceBase(state) {
  const { providers, preferences, meetingId } = state.choice.load;
  const list = providers && Array.isArray(providers.providers) ? providers.providers : [];
  const status = {};
  for (const id of PROVIDER_IDS) {
    const found = list.find((item) => item && item.id === id);
    status[id] = found && typeof found.status === "string" ? found.status : "disconnected";
  }
  const catalogs = { ...state.choice.catalogs };
  const catalogErrors = { ...state.choice.catalogErrors };
  const effects = [];
  for (const id of PROVIDER_IDS) {
    if (status[id] !== "connected") {
      catalogs[id] = null;
      catalogErrors[id] = catalogUnavailableReason(id, status[id]);
    } else {
      effects.push(request(`notes/choice/catalog/${id}/${state.choice.loadId}`, `/api/providers/${id}/models`, "GET", undefined, "choice-catalog", { id: state.choice.loadId, provider: id }));
    }
  }
  return out(
    patchChoice(state, {
      status,
      catalogs,
      catalogErrors,
      preferences: {
        provider: preferences && PROVIDER_IDS.includes(preferences.provider) ? preferences.provider : null,
        models: preferences && preferences.models && typeof preferences.models === "object" ? preferences.models : {},
        reasoning_effort: preferences && typeof preferences.reasoning_effort === "string" ? preferences.reasoning_effort : "default",
        reasoning_efforts: preferences && preferences.reasoning_efforts && typeof preferences.reasoning_efforts === "object"
          ? preferences.reasoning_efforts : {},
      },
      loaded: true,
      loadedFor: meetingId,
      loading: effects.length > 0,
      load: { ...state.choice.load, phase: effects.length ? "catalogs" : "idle", waiting: effects.length },
    }),
    effects,
  );
}

function choiceResponse(state, action, failed) {
  const { meta } = action;
  const load = state.choice.load;
  if (meta.id !== state.choice.loadId) return out(state);
  if (meta.op === "choice-catalog") {
    if (load.phase !== "catalogs") return out(state);
    const catalogs = { ...state.choice.catalogs };
    const catalogErrors = { ...state.choice.catalogErrors };
    if (failed) {
      catalogs[meta.provider] = null;
      const detail = object(apiError(action.error).payload)?.detail;
      catalogErrors[meta.provider] = detail && detail.code === "provider_in_use"
        ? t("{provider} đang chạy notes nên chưa tải lại được danh sách model. Model đã lưu vẫn được giữ.", { provider: providerLabel(meta.provider) })
        : t("Chưa tải được danh sách model của {provider}. Model đã lưu vẫn được giữ.", { provider: providerLabel(meta.provider) });
    } else {
      catalogs[meta.provider] = normalizeCatalog(object(action.response.body));
      catalogErrors[meta.provider] = "";
    }
    const waiting = load.waiting - 1;
    return out(patchChoice(state, {
      catalogs,
      catalogErrors,
      loading: waiting > 0,
      load: { ...load, waiting, phase: waiting > 0 ? "catalogs" : "idle" },
    }));
  }
  if (load.phase !== "base") return out(state);
  if (failed) return choiceFailed(state);
  const field = meta.op === "choice-providers" ? "providers" : "preferences";
  const filled = patchChoice(state, { load: { ...load, [field]: object(action.response.body) ?? false } });
  const next = filled.choice.load;
  return next.providers !== null && next.preferences !== null
    ? applyChoiceBase(patchChoice(filled, { load: { ...next, providers: next.providers || null, preferences: next.preferences || null } }))
    : out(filled);
}

function savePreference(state, provider, model) {
  const id = state.choice.saveId + 1;
  // The selects show the new choice at once; a run cannot start until it is saved.
  const next = patchChoice(state, { localProvider: provider, saving: true, saveId: id });
  const body = { provider, model, reasoning_effort: effortFor(next, provider, model) };
  return out(next, [request(`notes/choice/save/${id}`, "/api/notes-preferences", "PUT", JSON.stringify(body), "choice-save", { id, provider, model, meetingId: state.meetingId })]);
}

function preferenceSaved(state, action) {
  const { id, provider, model, meetingId } = action.meta;
  if (id !== state.choice.saveId) return out(state);
  const choice = state.choice;
  const saved = object(action.response.body);
  const preferences = {
    provider: PROVIDER_IDS.includes(saved && saved.provider) ? saved.provider : provider,
    models: saved && saved.models && typeof saved.models === "object" ? saved.models : { [provider]: model },
    reasoning_effort: provider === "chatgpt" && saved && typeof saved.reasoning_effort === "string"
      ? saved.reasoning_effort : choice.preferences.reasoning_effort,
    reasoning_efforts: saved && saved.reasoning_efforts && typeof saved.reasoning_efforts === "object"
      ? saved.reasoning_efforts : choice.preferences.reasoning_efforts,
  };
  const named = patchChoice(state, { preferences });
  const next = [providerLabel(provider), " · ", modelName(named, provider, model)];
  const data = meetingId ? entryOf(state, meetingId) : null;
  const message = data && activeTask(data)
    ? t("Đã lưu lựa chọn {next}. Lựa chọn này chỉ áp dụng cho lần chạy tiếp theo; tác vụ đang chạy giữ {provider} · {model}.", {
      next,
      provider: providerLabel(data.task.provider),
      model: data.task.model || t("model đã ghim"),
    })
    : t("Đã lưu lựa chọn {next} cho lần chạy tiếp theo.", { next });
  return out(patchChoice(named, { message, localProvider: null, saving: false }));
}

function preferenceFailed(state, action) {
  if (action.meta.id !== state.choice.saveId) return out(state);
  const error = apiError(action.error, action.meta.provider);
  return out(patchChoice(state, {
    message: t("Chưa lưu được lựa chọn: {error} Lựa chọn đã lưu trước đó vẫn được giữ.", { error: error.message || t("lỗi không xác định") }),
    localProvider: null,
    saving: false,
  }));
}

// ---- starting and canceling a run ----

function requestPayload(state, pending, meeting, generation) {
  if (isCompletePendingRequest(pending)) return out(state, []);
  const complete = {
    request_id: pending.request_id,
    output_language: pending.output_language,
    provider: pending.provider,
    expected_generation_id: generation.id,
    expected_source_version: generation.source_version,
    expected_metadata_version: meeting.metadata_version,
  };
  // The confirmed choice is stored with the request so an HTTP retry resends the same body.
  const confirmed = savedModel(state, pending.provider);
  if (state.choice.loaded && confirmed) {
    complete.expected_model = confirmed;
    if (pending.provider === "chatgpt" || pending.provider === "antigravity") {
      complete.expected_reasoning_effort = effortFor(state, pending.provider, confirmed);
    }
  }
  return savePending(state, meeting.id, complete);
}

function startNotes(state, requestId) {
  const meeting = state.meeting;
  const meetingId = state.meetingId;
  const generation = currentGeneration(meeting);
  if (!canStartNotes(state) || !generation || !meetingId) return out(state);
  let result = out(state);
  if (!pendingOf(state, meetingId)) {
    result = savePending(state, meetingId, {
      request_id: requestId,
      output_language: languageOf(state, meetingId),
      provider: selectedProvider(state, null),
    });
  }
  const pending = pendingOf(result.state, meetingId);
  let body = pending;
  result = then(result, (s) => {
    const payload = requestPayload(s, pending, meeting, generation);
    body = payload.state.pending[meetingId];
    return payload;
  });
  // The run's progress, errors and result all live in the notes tab, wherever the run was started from.
  result.effects.push(call("history.selectTab", ["notes"]));
  result = then(result, (s) => out(patchNotes(s, meetingId, { starting: true, runError: "", runErrorUncertain: false, actionMessage: "" })));
  result.effects.push(request(`notes/start/${meetingId}`, `/api/meetings/${encodeURIComponent(meetingId)}/notes-runs`, "POST", JSON.stringify(body), "start", { meetingId, provider: body.provider }));
  return result;
}

function startAccepted(state, action) {
  const { meetingId } = action.meta;
  const parsed = parseTask(object(action.response.body));
  if (parsed.error) return startRejected(state, action.meta, { message: parsed.error, status: 0, payload: null });
  const { task } = parsed;
  const locked = isActiveStatus(task.status);
  let result = clearPending(state, meetingId);
  result = then(result, (s) => out(patchNotes(s, meetingId, {
    task,
    sourceLocked: locked,
    sourceLockKnown: true,
    actionMessage: t("Yêu cầu notes đã được worker xác nhận."),
    starting: false,
  })));
  result.effects.push(lockEffect(meetingId, locked, true));
  result.effects.push(meetingChangeEffect(meetingId, "notes"));
  return then(result, (s) => (locked ? startPolling(s, meetingId, task) : readNotes(s, meetingId)));
}

function startRejected(state, meta, error) {
  const { meetingId } = meta;
  const definitive = isDefinitive(error);
  let result = definitive ? clearPending(state, meetingId) : out(state);
  const mapped = definitive ? failureMessage(error.code, meta.provider) : "";
  result = then(result, (s) => out(patchNotes(s, meetingId, {
    runError: mapped || errorMessageForCode(error.code, error.status, meta.provider) || error.message || t("Không thể xác nhận yêu cầu notes."),
    runErrorUncertain: !definitive,
    actionMessage: "",
  })));
  if (mapped) result = then(result, (s) => loadChoice(s, meetingId));
  if (definitive && error.status === 409) {
    result.effects.push(call("history.refreshMeeting", [meetingId]));
    result = then(result, (s) => readNotes(s, meetingId));
  }
  return then(result, (s) => out(patchNotes(s, meetingId, { starting: false })));
}

function cancelNotes(state) {
  const meetingId = state.meetingId;
  const data = currentEntry(state);
  const task = data && data.task;
  if (!meetingId || !task || task.kind !== "notes" || !isActiveStatus(task.status) || data.canceling) return out(state);
  return out(patchNotes(state, meetingId, { canceling: true }), [
    request(`notes/cancel/${task.id}`, `/api/tasks/${encodeURIComponent(task.id)}/cancel`, "POST", "{}", "cancel", { meetingId }),
  ]);
}

function cancelAnswered(state, action) {
  const { meetingId } = action.meta;
  const parsed = parseTask(object(action.response.body));
  if (parsed.error) return cancelFailed(state, action.meta, { message: parsed.error });
  const { task } = parsed;
  const next = patchNotes(state, meetingId, { task, runError: "", canceling: false });
  const result = isActiveStatus(task.status) ? startPolling(next, meetingId, task) : readNotes(next, meetingId);
  result.effects.push(meetingChangeEffect(meetingId, "notes"));
  return result;
}

function cancelFailed(state, meta, error) {
  return out(patchNotes(state, meta.meetingId, {
    runError: error.message || t("Không thể gửi yêu cầu hủy. Trạng thái worker chưa được xác nhận là đã đổi."),
    runErrorUncertain: false,
    canceling: false,
  }));
}

// ---- saving a revision and the saved copy ----

function saveSelectedNote(state) {
  const meetingId = state.meetingId;
  const data = currentEntry(state);
  const revision = data && data.revisions.find((item) => item.id === data.selectedNoteId);
  if (!meetingId || !revision) return out(state);
  const draft = state.drafts[revision.id];
  if (!draftIsDirty(draft) || data.saving[revision.id]) return out(state);
  const next = patchNotes(state, meetingId, { saving: { ...data.saving, [revision.id]: true }, actionMessage: "" });
  return out(next, [
    request(`notes/save/${revision.id}`, noteEndpoint(revision.id), "PATCH", JSON.stringify({ expected_edit_version: draft.expectedEditVersion, markdown: draft.value }), "save", { meetingId, revisionId: revision.id }),
  ]);
}

function doneSaving(state, meetingId, revisionId, patch) {
  const saving = { ...entryOf(state, meetingId).saving };
  delete saving[revisionId];
  return patchNotes(state, meetingId, { ...patch, saving });
}

function saveAnswered(state, action) {
  const { meetingId, revisionId } = action.meta;
  const saved = parseDetail(object(action.response.body));
  if (!saved || saved.id !== revisionId || saved.meeting_id !== meetingId) {
    const message = saved ? t("Máy chủ lưu nhầm revision notes.") : t("Máy chủ không trả về revision notes hợp lệ.");
    return out(doneSaving(state, meetingId, revisionId, { actionMessage: message }));
  }
  const data = entryOf(state, meetingId);
  let next = patchNotes(state, meetingId, { revisions: replaceRevision(data, saved) });
  next = patchDraft(next, revisionId, { value: saved.markdown, baselineMarkdown: saved.markdown, expectedEditVersion: saved.edit_version, conflict: null });
  return out(doneSaving(next, meetingId, revisionId, { runError: "", actionMessage: t("Markdown đã được lưu.") }), [meetingChangeEffect(meetingId, "notes")]);
}

function saveFailed(state, action) {
  const { meetingId, revisionId } = action.meta;
  const error = apiError(action.error);
  if (error.status !== 409) {
    return out(doneSaving(state, meetingId, revisionId, { actionMessage: error.message || t("Không thể lưu Markdown.") }));
  }
  // The conflict alert waits for the latest text (conflictLatest) so it shows the right version once.
  return out(state, [request(`notes/save-latest/${revisionId}`, noteEndpoint(revisionId), "GET", undefined, "save-latest", { meetingId, revisionId, conflictMessage: error.message })]);
}

function conflictLatest(state, action, failed) {
  const { meetingId, revisionId, conflictMessage } = action.meta;
  const latest = failed ? null : parseDetail(object(action.response.body));
  if (!latest || latest.id !== revisionId || latest.meeting_id !== meetingId) {
    // No latest text: the alert still shows, with the version the save expected.
    const placeholder = patchDraft(state, revisionId, { conflict: { markdown: "", editVersion: state.drafts[revisionId].expectedEditVersion } });
    return out(doneSaving(placeholder, meetingId, revisionId, {
      actionMessage: t("{error} Bản nháp được giữ nguyên; chưa tải được phiên bản mới nhất.", { error: conflictMessage }),
    }));
  }
  let next = patchNotes(state, meetingId, { revisions: replaceRevision(entryOf(state, meetingId), latest) });
  next = patchDraft(next, revisionId, { conflict: { markdown: latest.markdown, editVersion: latest.edit_version } });
  return out(doneSaving(next, meetingId, revisionId, { actionMessage: t("Bản đã lưu đổi ở nơi khác. Bản nháp được giữ nguyên.") }));
}

function keepDraftAndSaveLatest(state) {
  const revision = selectedRevision(state);
  const draft = revision && state.drafts[revision.id];
  if (!revision || !draft || !draft.conflict) return out(state);
  const next = patchDraft(state, revision.id, {
    expectedEditVersion: draft.conflict.editVersion,
    baselineMarkdown: draft.conflict.markdown,
    conflict: null,
  });
  return saveSelectedNote(next);
}

function useLatestSavedNote(state) {
  const revision = selectedRevision(state);
  const draft = revision && state.drafts[revision.id];
  if (!revision || !draft || !draft.conflict) return out(state);
  const next = patchDraft(state, revision.id, {
    value: draft.conflict.markdown,
    baselineMarkdown: draft.conflict.markdown,
    expectedEditVersion: draft.conflict.editVersion,
    conflict: null,
  });
  return out(patchNotes(next, state.meetingId, { actionMessage: t("Đã tải bản Markdown mới nhất đã lưu.") }));
}

// Copy and download use the saved text read just now, never the draft.
function readSavedMarkdown(state, action) {
  const meetingId = state.meetingId;
  const data = currentEntry(state);
  const revision = data && data.revisions.find((item) => item.id === data.selectedNoteId);
  if (!meetingId || !revision) return out(state);
  const draft = state.drafts[revision.id];
  if (draftIsDirty(draft) || draft.conflict || data.saving[revision.id]) {
    return out(patchNotes(state, meetingId, { actionMessage: t("Lưu hoặc giải quyết bản nháp trước khi sao chép hay tải xuống.") }));
  }
  return out({ ...patchNotes(state, meetingId, { actionMessage: t("Đang đọc phiên bản đã lưu mới nhất…") }), authoritativeId: state.authoritativeId + 1 }, [
    request(`notes/authoritative/${state.authoritativeId + 1}`, noteEndpoint(revision.id), "GET", undefined, "authoritative", {
      meetingId,
      noteId: revision.id,
      action,
      currentValue: draft.value,
      expectedVersion: draft.expectedEditVersion,
    }),
  ]);
}

function savedMarkdownRead(state, action, failed) {
  const { meetingId, noteId, currentValue, expectedVersion } = action.meta;
  const message = (actionMessage) => out(patchNotes(state, meetingId, { actionMessage }));
  if (failed) return message(apiError(action.error).message || t("Không thể đọc bản notes đã lưu mới nhất."));
  const latest = parseDetail(object(action.response.body));
  if (!latest) return message(t("Máy chủ không trả về revision notes hợp lệ."));
  if (latest.id !== noteId || latest.meeting_id !== meetingId) return message(t("Revision notes mới nhất không khớp cuộc họp."));
  let next = patchNotes(state, meetingId, { revisions: replaceRevision(entryOf(state, meetingId), latest) });
  const draft = state.drafts[noteId];
  if (draft.value !== currentValue || draft.expectedEditVersion !== expectedVersion) {
    next = patchDraft(next, noteId, { conflict: { markdown: latest.markdown, editVersion: latest.edit_version } });
    return out(patchNotes(next, meetingId, { actionMessage: t("Bản nháp thay đổi trong lúc đồng bộ. Bản nháp được giữ; chưa sao chép hoặc tải xuống.") }));
  }
  if (latest.edit_version !== expectedVersion || latest.markdown !== currentValue) {
    next = patchDraft(next, noteId, { value: latest.markdown, baselineMarkdown: latest.markdown, expectedEditVersion: latest.edit_version, conflict: null });
  }
  if (state.meetingId !== meetingId || entryOf(state, meetingId).selectedNoteId !== noteId) {
    return out(patchNotes(next, meetingId, { actionMessage: t("Phiên bản notes đã đổi trong lúc đồng bộ. Hãy chọn lại để tiếp tục.") }));
  }
  if (action.meta.action === "copy") {
    return out(patchNotes(next, meetingId, { actionMessage: "" }), [
      call("copy", [latest.markdown], { key: "notes/copy", ok: "notes/copied", error: "notes/copy-failed", meta: { editVersion: latest.edit_version } }),
    ]);
  }
  return out(
    patchNotes(next, meetingId, { actionMessage: t("Đã tải bản đã lưu · edit {version}.", { version: latest.edit_version }) }),
    [call("download", [safeFilename(state.meeting && state.meeting.title), latest.markdown])],
  );
}

// ---- saved transcript source ----

function sourceFailed(state, message) {
  return out(patchSource(state, {
    current: null,
    visible: true,
    status: message || t("Không thể mở nguồn transcript đã lưu."),
    focus: state.source.focus + 1,
  }));
}

function validSnapshot(snapshot, meetingId, snapshotId) {
  return Boolean(snapshot && snapshot.id === snapshotId && snapshot.meeting_id === meetingId && Array.isArray(snapshot.segments));
}

function speakerForSnapshot(snapshot, segment) {
  if (typeof segment.speaker_name === "string" && segment.speaker_name) return segment.speaker_name;
  const speakerId = segment.speaker_id;
  const speaker = Array.isArray(snapshot.speakers)
    ? snapshot.speakers.find((item) => item.speaker_id === speakerId)
    : null;
  return speaker && speaker.display_name ? speaker.display_name : speakerId || t("Chưa xác định");
}

const dateLabel = (date) => date || t("Chưa đặt ngày");

// How the current meeting departs from the saved source, in words; [] while they agree or before the meeting loads.
function sourceDifference(meeting, source) {
  if (!meeting) return [];
  const notes = [];
  const generation = currentGeneration(meeting);
  if (generation && generation.id !== source.generationId) {
    notes.push(t("Transcript hiện tại là một lần chép lời khác với bản chụp này, nên đoạn được đánh dấu chỉ khớp theo mốc thời gian."));
  } else if (generation) {
    const current = generation.segments.find((item) => item.id === source.segmentId);
    if (current && current.text !== source.text) notes.push(t("Văn bản của đoạn này đã được sửa trong transcript hiện tại."));
    if (current && speakerForSnapshot(generation, current) !== source.speaker) {
      notes.push(t("Người nói của đoạn này đã đổi trong transcript hiện tại."));
    }
  }
  if ((meeting.meeting_date || null) !== source.meetingDate) {
    notes.push(t("Ngày họp hiện tại ({current}) khác ngày họp trong bản chụp ({snapshot}).", {
      current: dateLabel(meeting.meeting_date || null),
      snapshot: dateLabel(source.meetingDate),
    }));
  }
  return notes.flatMap((note, index) => (index ? [" ", note] : [note]));
}

function showSource(state, reference, fromClick) {
  const effects = fromClick ? [call("history.showNotesSourceTab", [reference])] : [];
  const id = state.source.id + 1;
  const next = patchSource(state, { id, key: referenceKey(reference), status: t("Đang tải bản chụp transcript đã lưu…") });
  const snapshot = state.snapshots[reference.snapshotId];
  if (!snapshot) {
    effects.push(request(`notes/source-snapshot/${id}`, `/api/notes-sources/${encodeURIComponent(reference.snapshotId)}`, "GET", undefined, "source-snapshot", { id, reference }));
    return out(next, effects);
  }
  return then(out(next, effects), (s) => openSnapshot(s, reference, snapshot, id));
}

// Past the fetch: the snapshot is valid, so show its segment and send the player there.
function openSnapshot(state, reference, snapshot, id) {
  if (id !== state.source.id || state.meetingId !== reference.meetingId) return out(state);
  const segment = snapshot.segments.find((item) => item.id === reference.segmentId);
  if (
    !segment
    || typeof segment.text !== "string"
    || !Number.isFinite(Number(segment.start_seconds))
    || Number(segment.start_seconds) < 0
  ) return sourceFailed(state, t("Không tìm thấy đoạn trong bản chụp transcript đã lưu."));
  const source = {
    group: reference.group || "",
    snapshotId: snapshot.id,
    meetingId: reference.meetingId,
    generationId: snapshot.generation_id,
    segmentId: segment.id,
    startSeconds: Number(segment.start_seconds),
    text: segment.text,
    speaker: speakerForSnapshot(snapshot, segment),
    meetingDate: typeof snapshot.meeting_date === "string" && snapshot.meeting_date ? snapshot.meeting_date : null,
  };
  return out(
    patchSource(state, { current: source, visible: true, status: t("Đang đưa trình phát bản ghi tới mốc nguồn…"), focus: state.source.focus + 1 }),
    // mark and seek wait one tick so the render that shows the source card lands first
    [{ type: "schedule", key: "notes/source-shown", delay: 0, action: { type: "notes/source-shown", id } }],
  );
}

function snapshotAnswered(state, action) {
  const { id, reference } = action.meta;
  const snapshot = object(action.response.body);
  if (!validSnapshot(snapshot, reference.meetingId, reference.snapshotId)) {
    return id === state.source.id
      ? sourceFailed(state, t("Bản chụp nguồn không khớp cuộc họp hoặc mã nguồn đã chọn."))
      : out(state);
  }
  const cached = { ...state, snapshots: { ...state.snapshots, [reference.snapshotId]: snapshot } };
  return openSnapshot(cached, reference, snapshot, id);
}

function closeSource(state, href) {
  const next = patchSource(state, { id: state.source.id + 1, key: "", current: null, visible: false });
  const effects = [call("history.markNotesSource", [null])];
  if (href !== null) {
    const reference = sourceFromHref(href);
    if (reference) {
      const url = new URL(href);
      if (url.search) effects.push(call("replaceUrl", [`${url.pathname}${url.hash}`]));
    }
  }
  return out(next, effects);
}

// One flow for a chip click, a pasted link, Back and Forward: open the source the page address names.
function handleLocation(state, href) {
  const reference = sourceFromHref(href);
  if (!reference || reference.meetingId !== state.meetingId || state.view !== "meeting") {
    return !reference && state.source.key ? closeSource(state, null) : out(state);
  }
  if (state.source.key === referenceKey(reference) && (state.source.current || state.source.status)) return out(state);
  return showSource(state, reference, false);
}

// ---- meeting context ----

function adoptStored(state, meetingId, stored) {
  let result = out(state);
  if (!pendingOf(state, meetingId)) {
    const found = normalizePendingRequest(stored.pending);
    if (found) result = out({ ...state, pending: { ...state.pending, [meetingId]: found } });
    else {
      const legacy = normalizePendingRequest(stored.legacy, { legacy: true });
      if (legacy) result = savePending(state, meetingId, legacy);
    }
  }
  return then(result, (s) => {
    if (Object.hasOwn(s.outputLanguages, meetingId)) return out(s);
    const pending = pendingOf(s, meetingId);
    const language = pending ? pending.output_language : ["en", "vi"].includes(stored.language) ? stored.language : "en";
    return out({ ...s, outputLanguages: { ...s.outputLanguages, [meetingId]: language } });
  });
}

function meetingContext(state, action) {
  const detail = action.detail || {};
  const previousMeeting = state.meeting;
  const meeting = detail.view === "meeting" ? detail.meeting : null;
  let next = {
    ...state,
    view: detail.view || "",
    meeting,
    transcribing: Boolean(detail.transcriptionRequestInFlight),
    speakerNames: Array.isArray(action.speakerNames) ? action.speakerNames : [],
  };
  const meetingId = meeting && meeting.id;
  if (!meetingId) return stopPolling({ ...next, meetingId: null });
  const changed = state.meetingId !== meetingId;
  next = { ...next, meetingId };
  let result = out(next);
  if (changed) {
    result = then(result, stopPolling);
    result = then(result, (s) => out(patchChoice(s, { open: false })));
    result = then(result, (s) => out(patchNotes(s, meetingId, { sourceLockKnown: false, known: false })));
  }
  result = then(result, (s) => {
    const data = entryOf(s, meetingId);
    const lockKnown = (changed || previousMeeting !== meeting || !data.sourceLockKnown) && typeof meeting.notes_source_locked === "boolean";
    return out(lockKnown ? patchNotes(s, meetingId, { sourceLocked: meeting.notes_source_locked, sourceLockKnown: true }) : patchNotes(s, meetingId, {}));
  });
  const data = entryOf(result.state, meetingId);
  result.effects.push(lockEffect(meetingId, data.sourceLocked, data.sourceLockKnown));
  result = then(result, (s) => adoptStored(s, meetingId, action.stored || {}));
  // Several render paths can publish the same installed meeting. Read once per new
  // authoritative context; repeated paints still update local state and lock effects.
  if (changed || previousMeeting !== meeting) result = then(result, (s) => readNotes(s, meetingId));
  if (changed || !result.state.choice.loaded) result = then(result, (s) => loadChoice(s, meetingId));
  return then(result, (s) => handleLocation(s, action.href));
}

// ---- the reducer ----

function response(state, action) {
  const { meta } = action;
  switch (meta.op) {
    case "read": {
      if (meta.id !== state.readId || meta.meetingId !== state.meetingId || (meta.poll !== null && meta.poll !== state.poll.token)) return out(state);
      const parsed = parseNotes(meta.meetingId, object(action.response.body));
      return parsed
        ? installNotes(state, meta.meetingId, parsed, Boolean(meta.broadcast))
        : readFailed(state, meta.meetingId, meta.poll, t("Máy chủ không trả về trạng thái notes theo API đã công bố."));
    }
    case "choice-providers":
    case "choice-preferences":
    case "choice-catalog":
      return choiceResponse(state, action, false);
    case "choice-save":
      return preferenceSaved(state, action);
    case "start":
      return startAccepted(state, action);
    case "cancel":
      return cancelAnswered(state, action);
    case "save":
      return saveAnswered(state, action);
    case "save-latest":
      return conflictLatest(state, action, false);
    case "authoritative":
      return savedMarkdownRead(state, action, false);
    case "source-snapshot":
      return snapshotAnswered(state, action);
    default:
      return out(state);
  }
}

function requestFailed(state, action) {
  const { meta } = action;
  if (isWorkspaceAuthenticationFailure(action.error?.status, action.error?.body)) {
    const meetingId = state.meetingId || meta.meetingId;
    const next = meetingId ? patchNotes(state, meetingId, {
      runError: t("Phiên làm việc đã hết. Đang chuyển đến trang đăng nhập."),
      runErrorUncertain: false,
    }) : state;
    const stopped = stopPolling(next);
    return out(stopped.state, [
      ...stopped.effects,
      { type: "navigate", key: "notes/session-ended", url: "/access" },
    ]);
  }
  const task = meta.op === "cancel" ? entryOf(state, meta.meetingId)?.task : null;
  const error = apiError(action.error, meta.provider || task?.provider);
  switch (meta.op) {
    case "read":
      if (meta.id !== state.readId || meta.meetingId !== state.meetingId || (meta.poll !== null && meta.poll !== state.poll.token)) return out(state);
      return readFailed(state, meta.meetingId, meta.poll, error.message || t("Không thể đọc notes đã lưu."));
    case "choice-providers":
    case "choice-preferences":
    case "choice-catalog":
      return choiceResponse(state, action, true);
    case "choice-save":
      return preferenceFailed(state, action);
    case "start":
      return startRejected(state, meta, error);
    case "cancel":
      return cancelFailed(state, meta, error);
    case "save":
      return saveFailed(state, action);
    case "save-latest":
      return conflictLatest(state, action, true);
    case "authoritative":
      return savedMarkdownRead(state, action, true);
    case "source-snapshot":
      return action.meta.id === state.source.id ? sourceFailed(state, error.message) : out(state);
    default:
      return out(state);
  }
}

function transition(state, action) {
  switch (action.type) {
    case "notes/booted":
      return out({ ...state, mode: action.mode === "write" ? "write" : "view", hidden: Boolean(action.hidden), origin: action.origin });
    case "notes/visibility-changed": {
      const next = { ...state, hidden: Boolean(action.hidden) };
      return state.meetingId && activeTask(currentEntry(next))
        ? schedulePoll(next, state.meetingId, next.hidden ? POLL_DELAY_HIDDEN : POLL_DELAY)
        : out(next);
    }
    case "notes/context":
      return meetingContext(state, action);
    case "notes/location-changed":
      return handleLocation(state, action.href);
    case "notes/response":
      return response(state, action);
    case "notes/request-failed":
      return requestFailed(state, action);
    case "notes/poll-due": {
      const next = { ...state, poll: { ...state.poll, scheduled: false } };
      if (action.token !== next.poll.token || action.meetingId !== next.meetingId || !activeTask(entryOf(next, action.meetingId))) return out(next);
      return readNotes(next, action.meetingId, action.token, true);
    }
    case "notes/meeting-change-received":
      return action.meetingId === state.meetingId ? readNotes(state, state.meetingId) : out(state);
    case "notes/start-clicked":
      return startNotes(state, action.requestId);
    case "notes/cancel-clicked":
      return cancelNotes(state);
    case "notes/change-toggled":
      return out(patchChoice(state, { open: !state.choice.open }));
    case "notes/configure-clicked":
      return out(patchChoice(state, { open: true, focus: state.choice.focus + 1 }));
    case "notes/choice-focus-handled":
      return out(patchChoice(state, { focus: 0 }));
    case "notes/choice-reload-clicked":
    case "notes/provider-choice-changed":
      return state.meetingId ? loadChoice(state, state.meetingId) : out(state);
    case "notes/name-speakers-clicked":
      return out(state, [call("history.focusSpeakerCatalog", [])]);
    case "notes/open-transcript-clicked":
      return out(state, [call("history.selectTab", ["transcript", { focus: true }])]);
    case "notes/language-chosen": {
      const meetingId = state.meetingId;
      if (!meetingId || !["en", "vi"].includes(action.language) || pendingOf(state, meetingId)) return out(state);
      return out(
        { ...state, outputLanguages: { ...state.outputLanguages, [meetingId]: action.language } },
        storeBoth(outputLanguageStorageKey(meetingId), action.language),
      );
    }
    case "notes/provider-picked": {
      if (!PROVIDER_IDS.includes(action.provider)) return out(state);
      const cleared = patchChoice(state, { message: "" });
      // No saved model for this provider: the operator must pick one before anything is saved.
      return savedModel(cleared, action.provider)
        ? savePreference(cleared, action.provider, savedModel(cleared, action.provider))
        : out(patchChoice(cleared, { localProvider: action.provider }));
    }
    case "notes/model-picked":
      return action.model ? savePreference(state, selectedProvider(state, null), action.model) : out(state);
    case "notes/revision-picked": {
      if (!state.meetingId) return out(state);
      return out(patchNotes(state, state.meetingId, { selectedNoteId: action.id || null, actionMessage: "" }));
    }
    case "notes/draft-edited": {
      const revision = selectedRevision(state);
      if (!revision) return out(state);
      const draft = state.drafts[revision.id];
      const conflict = draft.expectedEditVersion === revision.edit_version
        ? null
        : { markdown: revision.markdown, editVersion: revision.edit_version };
      return out(patchNotes(patchDraft(state, revision.id, { value: action.value, conflict }), state.meetingId, { actionMessage: "" }));
    }
    case "notes/mode-chosen": {
      if (action.mode === state.mode || !["view", "write"].includes(action.mode)) return out(state);
      return out({ ...state, mode: action.mode }, [{ type: "storage", key: VIEW_MODE_KEY, area: "local", op: "write", value: action.mode }]);
    }
    case "notes/save-clicked":
      return saveSelectedNote(state);
    case "notes/keep-draft-clicked":
      return keepDraftAndSaveLatest(state);
    case "notes/use-latest-clicked":
      return useLatestSavedNote(state);
    case "notes/copy-clicked":
      return readSavedMarkdown(state, "copy");
    case "notes/export-clicked":
      return readSavedMarkdown(state, "export");
    case "notes/copied":
    case "notes/copy-failed": {
      if (!state.meetingId) return out(state);
      const message = action.type === "notes/copied"
        ? t("Đã sao chép bản đã lưu · edit {version}.", { version: action.meta.editVersion })
        : t("Không thể truy cập clipboard. Trình duyệt chưa sao chép nội dung.");
      return out(patchNotes(state, state.meetingId, { actionMessage: message }));
    }
    case "notes/source-opened":
      return action.reference && action.reference.meetingId === state.meetingId
        ? showSource(state, action.reference, Boolean(action.fromClick))
        : out(state);
    case "notes/source-closed":
      return closeSource(state, action.href);
    case "notes/source-shown": {
      const source = state.source.current;
      if (action.id !== state.source.id || !source) return out(state);
      return out(state, [
        call("history.markNotesSource", [source.segmentId, source.startSeconds]),
        call("history.seekNotesSource", [source], { key: "notes/source-open-seek", ok: "notes/source-sought", error: "notes/source-failed", meta: { id: action.id } }),
      ]);
    }
    case "notes/source-sought": {
      if (action.meta.id !== state.source.id) return out(state);
      if (!action.value) {
        return out(patchSource(state, { status: t("Đã mở nguồn bất biến nhưng chưa thể tua bản ghi. Dùng nút tua để thử lại.") }));
      }
      return out(state, [call("history.playRecording", [], { key: "notes/source-play", ok: "notes/source-played", error: "notes/source-failed", meta: { id: action.meta.id } })]);
    }
    case "notes/source-played": {
      if (action.meta.id !== state.source.id || !state.source.current) return out(state);
      const time = formatSeconds(state.source.current.startSeconds);
      return out(patchSource(state, {
        status: action.value
          ? t("Đã tua bản ghi cuộc họp tới {time} và đang phát.", { time })
          : t("Đã tua bản ghi cuộc họp tới {time}. Bấm phát trên trình phát để nghe.", { time }),
      }));
    }
    case "notes/source-failed":
      return action.meta.id === state.source.id ? sourceFailed(state, action.error && action.error.message) : out(state);
    case "notes/source-seek-clicked": {
      const source = state.source.current;
      if (!source) return out(state);
      return out(state, [call("history.seekNotesSource", [source], { key: "notes/source-seek", ok: "notes/source-seek-answered", meta: { source } })]);
    }
    case "notes/source-seek-answered": {
      const { source } = action.meta;
      const sought = Boolean(action.value);
      return out(
        patchSource(state, {
          status: sought
            ? t("Đã tua bản ghi cuộc họp tới {time}.", { time: formatSeconds(source.startSeconds) })
            : t("Chưa thể tua bản ghi tới mốc này. Hãy kiểm tra audio đã tải xong."),
        }),
        sought ? [call("history.markNotesSource", [source.segmentId, source.startSeconds])] : [],
      );
    }
    default:
      return out(state);
  }
}

const sameSummary = (a, b) => (a === null || b === null ? a === b : a.every((value, index) => value === b[index]));

export function reduce(state, action) {
  const result = transition(state, action);
  let next = settle(result.state);
  const effects = result.effects;
  const summary = selectSummary(next);
  if (summary && (action.type === "notes/context" || !sameSummary(selectSummary(state), summary))) {
    effects.push(call("history.setNotesSummary", summary));
  }
  if (next === state && !effects.length) return { state };
  return { state: next, effects };
}

// ---- view model ----

function stepView(task, reached, index) {
  const ended = ["failed", "canceled", "interrupted"].includes(task.status);
  if (task.status === "succeeded") return "done";
  if (reached === undefined) return "unknown";
  if (index < reached) return "done";
  if (index > reached) return "upcoming";
  return ended ? task.status : "current";
}

function taskView(state, data) {
  const task = data.task;
  const provider = task.provider;
  const providerId = PROVIDER_IDS.includes(provider) ? provider : "";
  const name = providerLabel(provider);
  const active = isActiveStatus(task.status);
  const ended = ["failed", "canceled", "interrupted"].includes(task.status);
  const reached = state.taskSteps[task.id];
  const effort = providerId === "antigravity"
    ? t("mức suy nghĩ {effort}", { effort: effortLabel(task.reasoning_effort === "medium" ? "medium" : "high") })
    : (providerId === "chatgpt" && task.reasoning_effort && task.reasoning_effort !== "default" ? `effort ${task.reasoning_effort}` : "");
  let status;
  if (task.status === "accepted") status = t("Tác vụ đã được nhận. Worker sẽ bắt đầu xử lý notes.");
  else if (task.status === "running") status = t("{taskProviderName} đang tạo notes. Tác vụ tiếp tục ở worker nếu bạn mở cuộc họp khác.", { taskProviderName: name });
  else if (task.status === "stopping") status = t("Đã gửi yêu cầu hủy. Tác vụ vẫn giữ slot cho đến khi worker xác nhận dừng.");
  else if (task.status === "succeeded") status = t("Notes đã được lưu. Chọn revision bên dưới để xem hoặc sửa.");
  else if (task.status === "failed") {
    status = failureMessage(task.error_code, provider)
      || errorMessageForCode(task.error_code, undefined, provider)
      || t("Tác vụ notes bằng {provider} thất bại. {next}", { provider: name, next: NEXT_STEP });
  } else if (task.status === "canceled") status = t("Worker đã xác nhận hủy tác vụ notes.");
  else if (task.status === "interrupted") status = t("Worker đã dừng giữa tác vụ notes. Không tự chạy lại.");
  else status = t("Đang đọc trạng thái tác vụ notes.");
  const taskFailure = errorMessageForCode(task.error_code, undefined, provider);
  if (taskFailure && !["failed", "interrupted"].includes(task.status)) status = [status, " ", taskFailure];
  const stopping = task.status === "stopping" || data.canceling;
  const progress = task.progress && typeof task.progress === "object" ? task.progress : null;
  const progressCurrent = Number.isSafeInteger(progress?.current) && progress.current >= 0 ? progress.current : null;
  const progressTotal = Number.isSafeInteger(progress?.total) && progress.total >= progressCurrent ? progress.total : null;
  let progressText = "";
  if (progress && task.stage === "preparing_source") progressText = t("Đang chuẩn bị transcript cho notes.");
  else if (progress && task.stage === "generating") progressText = progressTotal === null
    ? t("Đang tạo notes.")
    : t("Đang tạo notes · đã xong {current}/{total} lượt.", { current: progressCurrent ?? 0, total: progressTotal });
  else if (progress && task.stage === "chunking") progressText = progressTotal === null
    ? t("Đang xử lý các phần transcript · đã xong {current}.", { current: progressCurrent ?? 0 })
    : t("Đang xử lý transcript · đã xong {current}/{total} phần.", { current: progressCurrent ?? 0, total: progressTotal });
  else if (progress && task.stage === "merging") progressText = t("Đang hợp nhất notes · đã xong {current} lượt.", { current: progressCurrent ?? 0 });
  else if (progress && task.stage === "repairing") progressText = progressTotal === null
    ? t("Đang sửa phản hồi notes · lượt {current}.", { current: progressCurrent ?? 0 })
    : t("Đang sửa phản hồi notes · đã xong {current}/{total} lượt.", { current: progressCurrent ?? 0, total: progressTotal });
  else if (progress && task.stage === "rendering") progressText = t("Đang hoàn thiện notes.");
  const workerChecking = active && task.worker_state === "checking-worker";
  if (workerChecking) {
    status = task.status === "accepted"
      ? t("Đang kiểm tra worker trước khi bắt đầu xử lý notes.")
      : t("Đang kiểm tra worker vì heartbeat tác vụ notes đã cũ.");
  }
  return {
    status,
    elapsed: t("Đã chạy {n} giây", { n: Math.max(0, Number(task.elapsed_seconds) || 0).toFixed(1) }),
    providerModel: [name, task.model, effort].filter(Boolean),
    progress: workerChecking ? "" : progressText,
    steps: [0, 1, 2, 3].map((index) => stepView(task, reached, index)),
    // A run first seen after it ended has no known step, so say only how it ended.
    end: { hidden: !ended || reached !== undefined, state: ended && reached === undefined ? task.status : "", label: ended && reached === undefined ? TASK_STATE_LABELS[task.status] : "" },
    cancelHidden: !active,
    cancelDisabled: !active || stopping,
    cancelLabel: stopping ? t("Đang dừng…") : t("Hủy tạo notes"),
  };
}

function choiceView(state, data, pending) {
  const choice = state.choice;
  const provider = selectedProvider(state, pending);
  const readiness = modelReadiness(state, provider);
  const models = catalogModels(state, provider);
  const locked = Boolean(pending) || Boolean(data && data.starting) || choice.saving;
  const items = [];
  if (models === null) items.push({ value: readiness.model, text: readiness.model || t("Chưa có model") });
  else {
    if (!readiness.ready) items.push({ value: "", text: readiness.model ? t("Chọn model khác") : t("Chọn model") });
    for (const item of models) items.push({ value: item.id, text: item.display_name });
  }
  const name = providerLabel(provider);
  let effort = provider === "chatgpt" && choice.preferences.reasoning_effort !== "default"
    ? ` · effort ${choice.preferences.reasoning_effort}` : "";
  if (provider === "antigravity") effort = t(" · mức suy nghĩ {effort}", { effort: effortLabel(effortFor(state, "antigravity")) });
  let plan;
  if (!choice.loaded && !choice.loading && !pending) {
    plan = t("Chưa đọc được lựa chọn đã lưu nên chưa thể tạo notes mới. Hãy bấm “Tải lại lựa chọn”.");
  } else if (readiness.ready && readiness.model && models !== null) {
    plan = t("Lần chạy tiếp theo dùng {name} · {model}{effort}.", { name, model: modelName(state, provider, readiness.model), effort });
  } else if (readiness.missing) {
    plan = t("Model đã lưu “{model}” không còn trong danh sách của {name}. Hãy chọn model khác; hệ thống không tự chọn thay.", { model: readiness.model, name });
  } else if (models !== null) {
    plan = t("Chưa chọn model cho {name}. Hãy chọn một model trước khi tạo notes.", { name });
  } else {
    plan = t("Lần chạy tiếp theo dùng {name}{model}. {error}", {
      name,
      model: readiness.model ? [" · ", readiness.model, effort] : "",
      error: choice.catalogErrors[provider],
    });
  }
  // The plan line belongs to the open choice, except when the action cannot simply run and it says why.
  const configure = needsModelChoice(state, data, provider, readiness, pending);
  const catalogBlocked = Boolean(choice.catalogErrors[provider]);
  const stuck = needsConnection(state, data, provider, readiness, pending)
    || configure
    || catalogBlocked
    || (!choice.loaded && !choice.loading && !pending);
  return {
    provider,
    readiness,
    providerDisabled: locked,
    modelKey: `${provider}\u0000${readiness.model}\u0000${models ? models.map((item) => item.id).join("\u0001") : "-"}`,
    modelItems: items,
    modelValue: readiness.ready || models === null ? readiness.model : "",
    modelDisabled: locked || models === null || models.length === 0,
    plan,
    planHidden: !stuck && !choice.open,
    reloadHidden: choice.loaded || !state.meetingId,
    reloadDisabled: choice.loading,
    message: choice.message,
    configure,
    focus: choice.focus,
    policy: t("Ngày họp, lời thoại, mốc thời gian và tên người nói được gửi tới {name} bằng model đã chọn. Với nguồn dài, các bản tóm tắt trung gian cũng được gửi để tổng hợp notes. File audio/video, tên cuộc họp và thông tin tài khoản không được gửi.", { name }),
    hidden: !choice.open,
    expanded: choice.open,
  };
}

function revisionProvenance(revision) {
  const provider = typeof revision.provider === "string" && revision.provider.trim() ? providerLabel(revision.provider.trim()) : "";
  return [provider, revision.model, revision.reasoning_effort].filter((value) => typeof value === "string" && value.trim());
}

function revisionOptionLabel(revision, index, count) {
  const provenance = revisionProvenance(revision);
  const label = t("Bản {number} · {source}", { number: count - index, source: SOURCE_STATE_LABELS[revision.source_state] || t("Nguồn chưa xác định") });
  return provenance.length ? [label, ` · ${provenance.join(" / ")}`] : label;
}

function savedWorkView(state, data) {
  const revision = selectedRevision(state);
  const optionsKey = data
    ? data.revisions.map((item) => `${item.id}\u0001${item.provider || ""}\u0001${item.model || ""}`).join("\u0000")
    : "";
  const options = [];
  if (data && data.selectedNoteId && !data.revisions.some((item) => item.id === data.selectedNoteId)) {
    options.push({ value: data.selectedNoteId, text: t("Phiên bản đã chọn không còn trong danh sách") });
  }
  if (data) data.revisions.forEach((item, index) => options.push({ value: item.id, text: revisionOptionLabel(item, index, data.revisions.length) }));
  const view = {
    hidden: !revision,
    mode: state.mode,
    selectorKey: optionsKey,
    options,
    selected: data && data.selectedNoteId ? data.selectedNoteId : "",
    revision,
  };
  if (!revision) {
    return {
      ...view,
      meta: data && data.selectedNoteId ? t("Phiên bản đã chọn chưa có trong danh sách notes mới nhất. Bản nháp được giữ trong phiên này.") : "",
      warning: null,
    };
  }
  const draft = state.drafts[revision.id];
  const dirty = draftIsDirty(draft);
  const busy = Boolean(data.saving[revision.id]);
  const provenance = revisionProvenance(revision).join(" · ");
  return {
    ...view,
    draftValue: draft.value,
    textareaDisabled: busy,
    meta: [
      SOURCE_STATE_LABELS[revision.source_state] || t("Nguồn chưa xác định"),
      t("Ngôn ngữ: {language}", { language: OUTPUT_LANGUAGE_LABELS[revision.output_language] || OUTPUT_LANGUAGE_LABELS.en }),
      provenance,
      `Edit ${revision.edit_version}`,
    ].filter(Boolean).flatMap((part, index) => (index ? [" · ", part] : [part])),
    warning: revision.warnings.length ? { count: revision.warnings.length, first: revision.warnings[0].href } : null,
    blocks: markdownBlocks(draft.value, revision, state.mode, state.origin),
    saveDisabled: !dirty || busy,
    copyDisabled: dirty || Boolean(draft.conflict) || busy,
    draftStatus: dirty
      ? t("Có thay đổi chưa lưu. Sao chép và tải xuống chỉ dùng Markdown đã lưu.")
      : t("Đang xem đúng Markdown đã lưu cho revision này."),
    dirty,
    viewDraftHidden: !dirty || state.mode !== "view",
    writeLabel: dirty ? t("Viết, có thay đổi chưa lưu") : t("Viết"),
    conflict: draft.conflict
      ? { message: t("Bản đã lưu đã đổi thành edit {version}. Bản nháp của bạn vẫn được giữ; chọn cách xử lý trước khi lưu.", { version: draft.conflict.editVersion }), disabled: busy }
      : null,
  };
}

function sourceView(state) {
  const { current, visible, status, focus } = state.source;
  return {
    members: current ? (selectedRevision(state)?.display_citations || [])
      .flatMap((entry) => entry.sources)
      .filter((group) => selectedRevision(state).source_snapshot_id === current.snapshotId && group.members?.length > 1 && group.members.map((member) => member.id).join(",") === current.group && group.members.some((member) => member.id === current.segmentId))
      .slice(0, 1).flatMap((group) => group.members.length > 1 ? group.members.map((member) => citationChip(selectedRevision(state), member, current.group)) : []) : [],
    hidden: !visible,
    context: current ? t("Đang xem bản chụp transcript bất biến. Nội dung này không đổi khi transcript hiện tại được sửa.") : "",
    time: current ? formatSeconds(current.startSeconds) : "",
    speaker: current ? current.speaker : "",
    date: current ? dateLabel(current.meetingDate) : "",
    text: current ? current.text : "",
    difference: current ? sourceDifference(state.meeting, current) : [],
    status,
    seekDisabled: !current || !Number.isFinite(current.startSeconds) || current.startSeconds < 0,
    focus,
  };
}

// Everything the view draws, derived from the state. The speaker reminder and the copy status are
// the view's: an empty action message leaves the old text.
export function selectView(state) {
  const meeting = state.meeting;
  const data = currentEntry(state);
  const pending = state.meetingId ? pendingOf(state) : null;
  const generation = currentGeneration(meeting);
  const needed = Boolean(meeting) && !generation;
  const wait = transcriptionBusy(state, meeting);
  const waitMessage = t("Đang xử lý transcript. Chờ tác vụ kết thúc để tạo notes.");
  const activeOutputLanguage = activeTask(data) ? (data.task.output_language === "vi" ? "vi" : "en") : null;
  const outputLanguage = pending
    ? pending.output_language
    : activeOutputLanguage || (state.meetingId ? languageOf(state, state.meetingId) : "en");
  const choice = choiceView(state, data, pending);
  const { provider, readiness } = choice;
  const connect = needsConnection(state, data, provider, readiness, pending);
  const configure = choice.configure;
  const revisions = data ? data.revisions : [];
  let runLabel;
  if (connect) runLabel = t("Kết nối {provider} để tạo notes", { provider: providerLabel(provider) });
  else if (configure) runLabel = t("Chọn hoặc đổi model để tạo notes.");
  else if (data && data.starting) runLabel = t("Đang gửi yêu cầu…");
  else if (pending) runLabel = t("Xác minh yêu cầu notes");
  else {
    const label = [revisions.length ? t("Tạo lại notes") : t("Tạo notes"), OUTPUT_LANGUAGE_LABELS[outputLanguage] || OUTPUT_LANGUAGE_LABELS.en];
    if (state.choice.loaded && !state.choice.loading) {
      const model = readiness.model ? modelName(state, provider, readiness.model) : "";
      label.push([providerLabel(provider), model].filter(Boolean).join(" "));
    }
    runLabel = label.flatMap((part, index) => (index ? [" · ", part] : [part]));
  }
  const task = data && data.task ? taskView(state, data) : null;
  let status;
  if (task) status = task.status;
  else if (data && data.readError) status = t("Chưa đọc được trạng thái notes đã lưu.");
  else if (!meeting) status = t("Mở một cuộc họp để xem hoặc tạo notes.");
  else if (!generation) status = t("Cần transcript hoàn chỉnh trước khi tạo notes.");
  else status = t("Transcript hoàn chỉnh đã sẵn sàng. Notes dùng nhà cung cấp và model đang chọn.");
  if (wait) status = waitMessage;
  else if (data && data.runErrorUncertain) {
    status = t("{error} Yêu cầu chưa được tự gửi lại. Bấm “Xác minh yêu cầu notes” để gửi lại cùng request ID.", { error: data.runError });
  }
  const failedTranscription = meeting && meeting.transcription && meeting.transcription.task;
  const savedWork = savedWorkView(state, data);
  return {
    needed,
    emptyCopy: needed
      ? (wait
        ? t("Đang chép lời. Notes tạo được sau khi transcript hoàn tất.")
        : failedTranscription && ["failed", "canceled", "interrupted"].includes(failedTranscription.status)
          ? t("Lần chép lời gần nhất chưa hoàn tất. Mở Transcript & bản ghi để xem chi tiết và chạy lại.")
          : t("Notes được tạo từ transcript hoàn chỉnh. Chép lời bản ghi trước."))
      : "",
    outputLanguage,
    languageDisabled: Boolean(wait || (data && data.starting) || activeTask(data) || pending),
    choice,
    run: {
      action: connect ? "connect" : configure ? "configure" : "start",
      describedBy: connect || configure ? "notes-run-plan" : "notes-status",
      disabled: !connect && !configure && !canStartNotes(state),
      title: !connect && !configure && wait ? waitMessage : "",
      label: runLabel,
      inToolbar: Boolean(selectedRevision(state)),
    },
    reminder: { names: generation ? state.speakerNames : [], blocked: activeTask(data) || Boolean(data && data.sourceLocked) },
    status,
    task,
    error: data ? data.runError || data.readError : "",
    savedWork,
    actionMessage: data ? data.actionMessage : "",
    source: sourceView(state),
  };
}

// Speaker names the run will copy: the system's own, listed above the run button.
export function speakerReminder(names) {
  return names.length ? t("Notes sẽ dùng tên người nói hiện tại: {names}.", { names: names.join(", ") }) : "";
}

export function warningLine(warning) {
  return t("Markdown đã lưu có {count} liên kết nguồn không hợp lệ. Nội dung được giữ nguyên; Xem chỉ hiện chúng dưới dạng chữ và không mở nguồn nào. Liên kết đầu tiên: ", { count: warning.count });
}
