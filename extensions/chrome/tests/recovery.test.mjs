import assert from "node:assert/strict";
import test from "node:test";

import { API_VERSION, PROTOCOL, PROTOTYPE } from "../protocol.js";
import { pairingGuard, uploadStateForError } from "../recovery-core.js";

const binding = (overrides = {}) => ({
  serverUrl: "https://asr.example.test",
  webUrl: "https://asr.example.test",
  backendId: "backend-a",
  apiVersion: API_VERSION,
  protocol: PROTOCOL,
  prototype: PROTOTYPE,
  ...overrides
});

const retained = (overrides = {}) => ({
  endpoint: binding(),
  capture: { status: "complete" },
  ...overrides
});

test("same-binding re-pair accepts complete and interrupted retained audio", () => {
  assert.equal(pairingGuard([
    retained(),
    retained({ capture: { status: "interrupted" } })
  ], "https://asr.example.test", binding()), null);
});

test("pairing guard blocks active capture and cross-binding replacement", () => {
  assert.equal(pairingGuard([retained({ capture: { status: "recording" } })], "https://asr.example.test", binding()).code, "pair_locked");
  assert.equal(pairingGuard([retained()], "https://other.example.test", binding({ serverUrl: "https://other.example.test" })).code, "url_locked");
  assert.equal(pairingGuard([retained()], "https://asr.example.test", binding({ backendId: "backend-b" })).code, "binding_locked");
});

test("wrong-owner 404 asks for reconnection without a duplicate upload path", () => {
  assert.equal(uploadStateForError({ code: "not_found", status: 404 }, false), "needs_reconnect");
  assert.equal(uploadStateForError({ code: "auth_expired", status: 401 }, false), "auth_required");
  assert.equal(uploadStateForError({ code: "timeout" }, true), "retryable");
});
