import assert from "node:assert/strict";
import test from "node:test";

import {
  API_VERSION,
  EXTENSION_ID,
  MIME_TYPE,
  PROTOCOL,
  PROTOTYPE,
  bindingFromCapabilities,
  canonicalManifest,
  normalizeServerUrl,
  permissionPattern,
  redactedBinding,
  resolveMeetingUrl
} from "../protocol.js";

const limits = {
  max_chunk_bytes: 1024,
  max_recording_bytes: 8192,
  max_local_bytes: 4096,
  max_chunks: 8,
  max_duration_seconds: 60
};

function capabilities(overrides = {}) {
  return {
    protocol: PROTOCOL,
    api_version: API_VERSION,
    prototype: PROTOTYPE,
    backend_id: "backend-a",
    server_url: "https://asr.example.test/base",
    web_url: "https://asr.example.test/base",
    mime_types: [MIME_TYPE],
    limits,
    ...overrides
  };
}

test("production binding requires the exact API endpoint and a separate web origin", () => {
  assert.equal(PROTOCOL, "asr-extension-v1");
  assert.equal(API_VERSION, 1);
  assert.equal(PROTOTYPE, false);
  assert.equal(EXTENSION_ID, "fgkjjpoeokhajpiagepfcpmgfapilinf");

  const binding = bindingFromCapabilities("https://asr.example.test/base", capabilities());
  assert.deepEqual(
    { serverUrl: binding.serverUrl, webUrl: binding.webUrl, webOrigin: binding.webOrigin },
    {
      serverUrl: "https://asr.example.test/base",
      webUrl: "https://asr.example.test/base",
      webOrigin: "https://asr.example.test"
    }
  );
  assert.throws(
    () => bindingFromCapabilities("https://asr.example.test/base", capabilities({api_version: API_VERSION + 1})),
    { code: "api_version_mismatch" }
  );
  assert.equal(permissionPattern(binding.serverUrl), "https://asr.example.test/base/*");
  assert.throws(
    () => bindingFromCapabilities("https://asr.example.test/base", capabilities({ prototype: true })),
    { code: "protocol_mismatch" }
  );
  assert.throws(
    () => bindingFromCapabilities("https://asr.example.test/base", capabilities({ server_url: "https://other.example.test/base" })),
    { code: "backend_mismatch" }
  );
  assert.throws(
    () => bindingFromCapabilities("https://asr.example.test/base", capabilities({ web_url: "http://localhost:8877" })),
    { code: "invalid_web_url" }
  );
});

test("public connection identity does not expose stage capability knowledge", () => {
  const binding = bindingFromCapabilities("https://asr.example.test/base", capabilities({
    features: {
      stage_policy: {
        supported: true,
        contract_version: 1,
        format_version: 1,
        admission: { state: "disabled", format_version: 1, reason: "execution_not_ready" }
      }
    }
  }));
  const publicBinding = redactedBinding({ ...binding, paired: true });
  assert.equal(Object.hasOwn(publicBinding, "stagePolicy"), false);
  assert.deepEqual(Object.keys(publicBinding).sort(), [
    "apiVersion", "backendId", "deviceExpiresAt", "limits", "paired", "protocol", "prototype", "serverUrl", "webOrigin", "webUrl"
  ].sort());
});

test("endpoint normalization accepts only loopback HTTP and remote HTTPS", () => {
  assert.equal(normalizeServerUrl("http://127.0.0.1:8877/"), "http://127.0.0.1:8877");
  assert.equal(normalizeServerUrl("https://asr.example.test/base/"), "https://asr.example.test/base");
  assert.throws(() => normalizeServerUrl("http://asr.example.test"), { code: "invalid_url" });
  assert.throws(() => normalizeServerUrl("https://asr.example.test/base?token=secret"), { code: "invalid_url" });
  assert.throws(() => normalizeServerUrl("https://user:pass@asr.example.test"), { code: "invalid_url" });
});

test("meeting navigation is pinned to the capability web root", () => {
  const binding = bindingFromCapabilities("https://asr.example.test/base", capabilities());
  assert.equal(
    resolveMeetingUrl(binding, "https://asr.example.test/base/meetings/meeting-a"),
    "https://asr.example.test/base/meetings/meeting-a"
  );
  for (const value of [
    "https://asr.example.test/base/meetings/meeting-a?token=secret",
    "https://asr.example.test/base/meetings/meeting-a#token",
    "https://other.example.test/base/meetings/meeting-a",
    "https://asr.example.test/base/settings",
    "https://asr.example.test/base/meetings/meeting/a",
    "/meetings/meeting-a"
  ]) {
    assert.throws(() => resolveMeetingUrl(binding, value), { code: "invalid_result_url" });
  }
});

test("the finalize manifest is canonical and contiguous", () => {
  const digest = "a".repeat(64);
  const manifest = canonicalManifest([
    { sequence: 1, offset: 2, length: 2, sha256: digest },
    { sequence: 0, offset: 0, length: 2, sha256: digest }
  ]);
  assert.equal(manifest.text, `v1\n0:0:2:${digest}\n1:2:2:${digest}\n`);
  assert.equal(manifest.totalBytes, 4);
  assert.equal(manifest.chunkCount, 2);
});
