import assert from "node:assert/strict";
import test from "node:test";

import {
  API_VERSION,
  MIME_TYPE,
  PROTOCOL,
  PROTOTYPE,
  bindingFromCapabilities,
  stagePolicyFromCapabilities,
} from "../protocol.js";
import {
  createCaptureIntent,
  createPayloadForSession,
  validateCreateResponseForSession,
  validateCreateResponse,
} from "../intent-core.js";

const limits = {
  max_chunk_bytes: 1024,
  max_recording_bytes: 8192,
  max_local_bytes: 4096,
  max_chunks: 8,
  max_duration_seconds: 60,
};

const capabilities = (overrides = {}) => ({
  protocol: PROTOCOL,
  api_version: API_VERSION,
  prototype: PROTOTYPE,
  backend_id: "backend-a",
  server_url: "https://asr.example.test/base",
  web_url: "https://asr.example.test/base",
  mime_types: [MIME_TYPE],
  limits,
  ...overrides,
});

const payload = {
  client_recording_id: "recording-a",
  capture_epoch: "epoch-a",
  mime_type: MIME_TYPE,
  title: "ASR Studio capture",
  metadata: { sources: ["tab", "microphone"] },
};

test("only a valid predecessor response without the feature permits legacy admission", () => {
  const binding = bindingFromCapabilities("https://asr.example.test/base", capabilities());
  assert.equal(binding.stagePolicy.knowledge, "absent");
  assert.equal(binding.stagePolicy.admission, "legacy");
  assert.equal(stagePolicyFromCapabilities({}).knowledge, "unknown");
  assert.equal(stagePolicyFromCapabilities(capabilities({ features: { stage_policy: { supported: false } } })).knowledge, "unsupported");
  assert.equal(stagePolicyFromCapabilities(capabilities({ features: { stage_policy: { supported: true, format_version: 1, contract_version: 1, admission: { state: "disabled", format_version: 1, reason: "execution_not_ready" } } } })).admission, "disabled");
  assert.equal(stagePolicyFromCapabilities(capabilities({ features: { stage_policy: { supported: true, format_version: 2, contract_version: 1, admission: { state: "enabled", format_version: 2, reason: null } } } })).knowledge, "unsupported");
  assert.equal(stagePolicyFromCapabilities(capabilities({ features: { stage_policy: { supported: true, format_version: 1, contract_version: 2, admission: { state: "enabled", format_version: 1, reason: null } } } })).knowledge, "unsupported");
});

test("capability knowledge is separate from endpoint binding identity", () => {
  const absent = bindingFromCapabilities("https://asr.example.test/base", capabilities());
  const disabled = bindingFromCapabilities("https://asr.example.test/base", capabilities({
    features: { stage_policy: { supported: true, format_version: 1, contract_version: 1, admission: { state: "disabled", format_version: 1, reason: "execution_not_ready" } } },
  }));
  assert.equal(absent.stagePolicy.admission, "legacy");
  assert.equal(disabled.stagePolicy.admission, "disabled");
  assert.equal(absent.serverUrl, disabled.serverUrl);
  assert.equal(absent.backendId, disabled.backendId);
});

test("the create intent persists exact JSON and adds only the capable stage field", () => {
  const absent = stagePolicyFromCapabilities(capabilities());
  const intent = createCaptureIntent({
    clientRecordingId: payload.client_recording_id,
    captureEpoch: payload.capture_epoch,
    stagePolicy: absent,
    createJson: payload,
  });
  assert.deepEqual(intent.create_json, payload);
  assert.equal(intent.format_version, "legacy");
  assert.deepEqual(createPayloadForSession({ capture: { createIntent: intent } }), payload);
  assert.equal(validateCreateResponse({}, intent), null);
  assert.deepEqual(validateCreateResponse({ stage_policy: { format_version: 0, intent: null } }, intent), {
    format_version: 0,
    intent: null,
  });

  const disabled = stagePolicyFromCapabilities(capabilities({ features: { stage_policy: { supported: true, format_version: 1, contract_version: 1, admission: { state: "disabled", format_version: 1, reason: "execution_not_ready" } } } }));
  const blocked = createCaptureIntent({
    clientRecordingId: payload.client_recording_id,
    captureEpoch: payload.capture_epoch,
    stagePolicy: disabled,
    createJson: payload,
  });
  const capablePayload = createPayloadForSession({ capture: { createIntent: blocked } });
  assert.deepEqual(capablePayload.stage_policy, { format_version: 1 });
  assert.equal(Object.hasOwn(capablePayload, "asr"), false);
  assert.deepEqual(validateCreateResponse({ stage_policy: { format_version: 1, intent: { format_version: 1 } } }, blocked), {
    format_version: 1,
    intent: { format_version: 1 },
  });
  assert.throws(() => validateCreateResponse({ stage_policy: { format_version: 0, intent: null } }, blocked), { code: "protocol_mismatch" });
});

test("draining is known and fenced without changing the capable wire", () => {
  const draining = stagePolicyFromCapabilities({
    protocol: PROTOCOL,
    api_version: API_VERSION,
    prototype: PROTOTYPE,
    features: {
      stage_policy: {
        supported: true,
        format_version: 1,
        contract_version: 1,
        admission: { state: "draining", format_version: 1, reason: "draining" },
      },
    },
  });
  assert.equal(draining.knowledge, "known");
  assert.equal(draining.admission, "draining");
  const intent = createCaptureIntent({
    clientRecordingId: payload.client_recording_id,
    captureEpoch: payload.capture_epoch,
    stagePolicy: draining,
    createJson: payload,
  });
  assert.deepEqual(createPayloadForSession({ capture: { createIntent: intent } }).stage_policy, { format_version: 1 });
});

test("old rows with an unexpected stage field are not treated as legacy", () => {
  assert.throws(() => createPayloadForSession({
    endpoint: {},
    capture: { createPayload: { ...payload, stage_policy: { format_version: 1 } } },
  }), { code: "intent_missing" });
});

test("a new row with an explicit empty intent stays fenced instead of becoming legacy", () => {
  const row = {
    endpoint: {},
    capture: { createPayload: payload, createIntent: null },
  };
  assert.throws(() => createPayloadForSession(row), { code: "intent_missing" });
  assert.throws(() => validateCreateResponseForSession({}, row), { code: "stage_policy_not_ready" });
});
