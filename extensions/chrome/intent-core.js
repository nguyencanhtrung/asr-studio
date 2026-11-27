// Pure durable-boundary helpers for a capture's first create request.
// Format 1 is added only after a valid capability response has pinned it;
// the three Settings booleans never cross this boundary.

import { makeError } from "./protocol.js";

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const KNOWLEDGE = new Set(["absent", "known", "unsupported", "unknown"]);

function copyJson(value) {
  try {
    const text = JSON.stringify(value);
    if (!text) return null;
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function validIdentity(value) {
  return typeof value === "string" && value.length > 0;
}

function isKnownFormat(stagePolicy) {
  return stagePolicy?.knowledge === "known" && stagePolicy.formatVersion === 1;
}

function exactCreateJson(createJson, stagePolicy) {
  const exact = copyJson(createJson);
  if (!exact) return null;
  const requestedStage = exact.stage_policy;
  if (isKnownFormat(stagePolicy)) {
    if (requestedStage !== undefined && (requestedStage === null || requestedStage.format_version !== 1
      || Object.keys(requestedStage).length !== 1)) return null;
    exact.stage_policy = { format_version: 1 };
  } else if (requestedStage !== undefined) {
    // Only a known capability may add the new field. Unknown or unsupported
    // responses must retain a blocked intent, never a guessed wire shape.
    return null;
  }
  return exact;
}

export function createCaptureIntent({ clientRecordingId, captureEpoch, stagePolicy, createJson }) {
  if (!validIdentity(clientRecordingId) || !validIdentity(captureEpoch) || !isObject(stagePolicy)
    || !KNOWLEDGE.has(stagePolicy.knowledge) || !isObject(createJson)
    || createJson.client_recording_id !== clientRecordingId || createJson.capture_epoch !== captureEpoch) {
    throw makeError("invalid_create_intent", "The durable capture intent is invalid.");
  }
  const exact = exactCreateJson(createJson, stagePolicy);
  if (!exact) throw makeError("invalid_create_intent", "The create request could not be serialized.");
  const formatVersion = stagePolicy.knowledge === "absent" && stagePolicy.admission === "legacy"
    ? "legacy"
    : stagePolicy.formatVersion;
  return {
    client_recording_id: clientRecordingId,
    capture_epoch: captureEpoch,
    capability: {
      knowledge: stagePolicy.knowledge,
      admission: stagePolicy.admission,
      contract_version: stagePolicy.contractVersion ?? null,
      format_version: stagePolicy.formatVersion ?? null,
      reason: stagePolicy.reason ?? null,
    },
    format_version: formatVersion,
    create_json: exact,
  };
}

export function createPayloadForSession(session) {
  const capture = session?.capture || {};
  const intent = capture.createIntent;
  // Rows written before this update have no intent and no stage capability
  // knowledge. Preserve their exact legacy request for recovery.
  if (!intent) {
    if (session?.endpoint?.stagePolicy === undefined && isObject(capture.createPayload)
      && !Object.hasOwn(capture.createPayload, "stage_policy")) return capture.createPayload;
    throw makeError("intent_missing", "The durable capture intent is missing.");
  }
  if (intent.capability?.knowledge === "known" && intent.format_version === 1
    && intent.capability.format_version === 1 && isObject(intent.create_json)
    && intent.create_json.stage_policy?.format_version === 1
    && Object.keys(intent.create_json.stage_policy).length === 1
    && intent.create_json.client_recording_id === intent.client_recording_id
    && intent.create_json.capture_epoch === intent.capture_epoch) {
    return intent.create_json;
  }
  if (intent.format_version !== "legacy" || intent.capability?.knowledge !== "absent") {
    throw makeError("stage_policy_not_ready", "The server has not confirmed the new capture format is ready.");
  }
  if (!isObject(intent.create_json)
    || intent.create_json.client_recording_id !== intent.client_recording_id
    || intent.create_json.capture_epoch !== intent.capture_epoch) {
    throw makeError("invalid_create_intent", "The durable capture intent does not match its recording identity.");
  }
  return intent.create_json;
}

export function validateCreateResponse(response, intent) {
  const stage = response?.stage_policy;
  if (intent?.format_version === "legacy") {
    if (stage === undefined) return null;
    if (!isObject(stage) || stage.format_version !== 0 || stage.intent !== null
      || Object.keys(stage).some((key) => !["format_version", "intent"].includes(key))) {
      throw makeError("protocol_mismatch", "The legacy create response has an invalid stage policy.");
    }
    return stage;
  }
  if (intent?.format_version === 1) {
    if (!isObject(stage) || stage.format_version !== 1 || !isObject(stage.intent)
      || stage.intent.format_version !== 1
      || Object.keys(stage).some((key) => !["format_version", "intent"].includes(key))
      || Object.keys(stage.intent).some((key) => key !== "format_version")) {
      throw makeError("protocol_mismatch", "The capable create response has an invalid stage policy.");
    }
    return stage;
  }
  throw makeError("stage_policy_not_ready", "The create response cannot be matched to its durable intent.");
}
