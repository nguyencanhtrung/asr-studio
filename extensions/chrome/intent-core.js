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

function replayableCreatePayload(session) {
  // Only pre-intent rows are legacy rows. A new row with a null or corrupt
  // intent has no proven first body and must fail closed after createAttempted.
  if (!isLegacyCaptureRow(session)) return null;
  const payload = session?.capture?.createPayload;
  const clientRecordingId = session?.clientRecordingId;
  const captureEpoch = session?.capture?.captureEpoch;
  if (session?.upload?.createAttempted !== true || !isObject(payload) || !validIdentity(clientRecordingId)
    || payload.client_recording_id !== clientRecordingId) return null;
  if (captureEpoch !== undefined && (!validIdentity(captureEpoch) || payload.capture_epoch !== captureEpoch)) return null;
  return payload;
}

function validStoredIntent(intent, session) {
  const clientRecordingId = session?.clientRecordingId;
  const captureEpoch = session?.capture?.captureEpoch;
  const createJson = intent?.create_json;
  if (!isObject(intent) || !validIdentity(clientRecordingId) || !validIdentity(captureEpoch)
    || intent.client_recording_id !== clientRecordingId || intent.capture_epoch !== captureEpoch
    || !isObject(createJson) || createJson.client_recording_id !== clientRecordingId
    || createJson.capture_epoch !== captureEpoch || !isObject(intent.capability)) return false;
  if (intent.format_version === "legacy") {
    return intent.capability.knowledge === "absent"
      && intent.capability.contract_version === null
      && intent.capability.format_version === null
      && !Object.hasOwn(createJson, "stage_policy");
  }
  return intent.format_version === 1
    && intent.capability.knowledge === "known"
    && intent.capability.contract_version === 1
    && intent.capability.format_version === 1
    && isObject(createJson.stage_policy)
    && createJson.stage_policy.format_version === 1
    && Object.keys(createJson.stage_policy).length === 1;
}

function replayIntentForSession(session) {
  const payload = replayableCreatePayload(session);
  if (!payload) throw makeError("stage_policy_not_ready", "The create response cannot be matched to its durable intent.");
  const stage = payload.stage_policy;
  if (stage === undefined) return { format_version: "legacy" };
  if (isObject(stage) && stage.format_version === 1 && Object.keys(stage).length === 1) {
    return { format_version: 1 };
  }
  throw makeError("protocol_mismatch", "The stored create request has an invalid stage policy.");
}

function replayOrThrow(session, code, message) {
  const payload = replayableCreatePayload(session);
  if (payload) return payload;
  throw makeError(code, message);
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

// Rows written before durable create intents have no createIntent key and no
// endpoint stage-policy binding. Their payload is the only wire contract that
// can be replayed. A present null or malformed intent is a new-row failure,
// not evidence of a legacy row.
export function isLegacyCaptureRow(session) {
  const capture = session?.capture;
  const endpoint = session?.endpoint;
  return isObject(capture)
    && isObject(endpoint)
    && !Object.hasOwn(capture, "createIntent")
    && isObject(capture.createPayload)
    && !Object.hasOwn(capture.createPayload, "stage_policy")
    && !Object.hasOwn(endpoint, "stagePolicy");
}

export function createCaptureIntentForSession(session, stagePolicy) {
  const payload = session?.capture?.createPayload;
  return createCaptureIntent({
    clientRecordingId: session?.clientRecordingId,
    captureEpoch: session?.capture?.captureEpoch,
    stagePolicy,
    createJson: payload,
  });
}

export function createPayloadForSession(session) {
  const capture = session?.capture || {};
  if (isLegacyCaptureRow(session)) return capture.createPayload;
  const intent = capture.createIntent;
  if (!intent) return replayOrThrow(session, "intent_missing", "The durable capture intent is missing.");
  if (!validStoredIntent(intent, session)) {
    throw makeError("invalid_create_intent", "The durable capture intent does not match its recording identity or format.");
  }
  if (intent.format_version === 1) {
    return intent.create_json;
  }
  return intent.create_json;
}

export function validateCreateResponseForSession(response, session) {
  if (isLegacyCaptureRow(session)) return validateCreateResponse(response, { format_version: "legacy" });
  if (session?.upload?.createAttempted === true && !session?.capture?.createIntent) {
    return validateCreateResponse(response, replayIntentForSession(session));
  }
  const intent = session?.capture?.createIntent;
  if (!validStoredIntent(intent, session)) {
    throw makeError("invalid_create_intent", "The durable capture intent does not match its recording identity or format.");
  }
  return validateCreateResponse(response, intent);
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
