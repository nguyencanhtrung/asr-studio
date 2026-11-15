import assert from "node:assert/strict";
import test from "node:test";

import {
  DEVICE_LIFETIMES,
  initialState,
  reduce,
  selectDevices,
} from "../../src/asr_studio/web/extension-devices-core.js";

const T0 = "2026-11-15T10:00:00Z";
const T1 = "2026-12-15T10:00:00Z";
const listReply = {
  backend_id: "backend-1",
  extension_id: "fgkjjpoeokhajpiagepfcpmgfapilinf",
  server_urls: ["https://studio.example.test"],
  devices: [
    {
      id: "record-1",
      device_id: "device-abcdefghijklmnopqrstuvwxyz",
      server_url: "https://studio.example.test",
      device_lifetime: "30d",
      device_expires_at: T1,
      created_at: T0,
      last_seen_at: null,
      revoked_at: null,
      status: "active",
      device_token: "never-render-this",
      session_token: "never-render-this-either",
    },
  ],
};

function open() {
  return reduce(initialState, { type: "provider/dialog-opened" });
}

function loaded() {
  const opened = open();
  return reduce(opened.state, {
    type: "extension/devices-loaded",
    meta: { request: opened.state.request },
    response: { body: listReply },
  });
}

test("opening Settings loads the operator device list with same-origin credentials", () => {
  const result = open();

  assert.equal(result.state.open, true);
  assert.equal(result.state.loading, true);
  assert.equal(result.state.request, 1);
  assert.deepEqual(result.effects, [{
    type: "fetch",
    key: "extension/devices/list",
    url: "/api/extension/devices",
    init: {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
    },
    ok: "extension/devices-loaded",
    error: "extension/devices-failed",
    meta: { request: 1 },
  }]);
});

test("list normalization keeps only contract fields and preserves exact endpoint data", () => {
  const result = loaded();
  const device = result.state.devices[0];
  const view = selectDevices(result.state);

  assert.equal(result.state.error, null);
  assert.equal(device.id, "record-1");
  assert.equal(device.device_id, listReply.devices[0].device_id);
  assert.equal(device.server_url, "https://studio.example.test");
  assert.equal(Object.hasOwn(device, "device_token"), false);
  assert.equal(Object.hasOwn(device, "session_token"), false);
  assert.deepEqual(view.serverUrls, ["https://studio.example.test"]);
  assert.equal(view.rows[0].device_id, device.device_id);
  assert.equal(view.rows[0].status.text, "Đang hoạt động");
});

test("a pending server record before pair may omit client id and endpoint", () => {
  const opened = open();
  const result = reduce(opened.state, {
    type: "extension/devices-loaded",
    meta: { request: opened.state.request },
    response: {
      body: {
        backend_id: "backend-1",
        extension_id: "fgkjjpoeokhajpiagepfcpmgfapilinf",
        server_urls: ["https://studio.example.test"],
        devices: [{
          id: "pending-record",
          device_id: null,
          server_url: null,
          device_lifetime: "7d",
          device_expires_at: null,
          created_at: T0,
          last_seen_at: null,
          revoked_at: null,
          status: "pending",
        }],
      },
    },
  });

  assert.equal(result.state.devices.length, 1);
  assert.equal(result.state.devices[0].device_id, null);
  const row = selectDevices(result.state).rows[0];
  assert.equal(row.recordLabel, "pending-record");
  assert.equal(row.expires.text, "Chờ client kích hoạt");
  assert.equal(row.deviceLabel.text, "Đang chờ client");
});

test("a stale list reply cannot replace the latest Settings request", () => {
  const first = open();
  const second = reduce(first.state, { type: "provider/dialog-opened" });
  const stale = reduce(second.state, {
    type: "extension/devices-loaded",
    meta: { request: first.state.request },
    response: { body: listReply },
  });

  assert.strictEqual(stale.state, second.state);
  assert.equal(stale.state.devices.length, 0);
  assert.equal(stale.effects.length, 0);
});

test("lifetime choices include never and code creation returns a separate expiry", () => {
  assert.deepEqual(DEVICE_LIFETIMES, ["7d", "30d", "90d", "never"]);
  const ready = loaded().state;
  const chosen = reduce(ready, { type: "extension/devices-lifetime-chosen", value: "never" });
  const requested = reduce(chosen.state, { type: "extension/devices-code-requested" });
  const effect = requested.effects.find((item) => item.key === "extension/devices/code");

  assert.equal(requested.state.selectedLifetime, "never");
  assert.deepEqual(JSON.parse(effect.init.body), { device_lifetime: "never" });

  const answered = reduce(requested.state, {
    type: "extension/devices-code-created",
    meta: { request: requested.state.codeRequest },
    response: { body: { code: "ABCD-EFGH", expires_at: "2026-11-15T10:10:00Z", device_lifetime: "never" } },
  });
  assert.equal(answered.state.code, "ABCD-EFGH");
  assert.equal(answered.state.codeExpiresAt, "2026-11-15T10:10:00Z");
  assert.equal(answered.state.codeLifetime, "never");
  assert.equal(selectDevices(answered.state).codeExpiry.value, "2026-11-15T10:10:00Z");
  assert.ok(answered.effects.some((item) => item.key === "extension/devices/list"));
});

test("re-pair sends only the explicitly selected server record id", () => {
  const ready = loaded().state;
  const requested = reduce(ready, {
    type: "extension/devices-repair-requested",
    deviceId: "record-1",
  });
  const effect = requested.effects.find((item) => item.key === "extension/devices/code");

  assert.deepEqual(JSON.parse(effect.init.body), {
    device_lifetime: "30d",
    device_record_id: "record-1",
  });
});

test("revoke changes status only after the persisted revoke response", () => {
  const ready = loaded().state;
  const confirmed = reduce(ready, {
    type: "extension/devices-revoke-requested",
    deviceId: "record-1",
  });
  const requested = reduce(confirmed.state, {
    type: "extension/devices-revoke-confirmed",
    deviceId: "record-1",
  });
  const failed = reduce(requested.state, {
    type: "extension/devices-revoke-failed",
    meta: { deviceId: "record-1", request: requested.state.revokeRequest },
    error: { status: 404, body: { error: { code: "device_not_found", message: "secret" } } },
  });

  const revokeEffect = requested.effects.find((item) => item.key === "extension/devices/revoke");
  assert.equal(revokeEffect.url, "/api/extension/devices/record-1/revoke");
  assert.equal(revokeEffect.init.method, "POST");
  assert.equal(revokeEffect.init.headers.Accept, "application/json");
  assert.equal(revokeEffect.init.credentials, "same-origin");
  assert.equal(revokeEffect.init.cache, "no-store");
  assert.equal(failed.state.devices[0].status, "active");
  assert.match(failed.state.error.text.text, /không tìm thấy/i);
  assert.doesNotMatch(failed.state.error.text.text, /secret/);

  const unconfirmed = reduce(requested.state, {
    type: "extension/devices-revoked",
    meta: { deviceId: "record-1", request: requested.state.revokeRequest },
    response: { body: { id: "record-1", revoked: false } },
  });
  assert.equal(unconfirmed.state.devices[0].status, "active");
  assert.equal(unconfirmed.state.error.code, "REVOKE_NOT_CONFIRMED");
  assert.equal(unconfirmed.state.ui, "focus-revoke");

  const succeeded = reduce(requested.state, {
    type: "extension/devices-revoked",
    meta: { deviceId: "record-1", request: requested.state.revokeRequest },
    response: { body: { id: "record-1", revoked: true } },
  });
  assert.equal(succeeded.state.devices[0].status, "revoked");
  assert.equal(succeeded.state.devices[0].revoked_at, null);
  assert.equal(succeeded.state.pendingRevoke, null);
  assert.equal(succeeded.state.revokeTarget, null);
  assert.ok(succeeded.effects.some((item) => item.key === "extension/devices/list"));
});

test("copy effect receives only the one-time code and never an expiry or device credential", () => {
  const ready = loaded().state;
  const requested = reduce(ready, { type: "extension/devices-code-requested" });
  const answered = reduce(requested.state, {
    type: "extension/devices-code-created",
    meta: { request: requested.state.codeRequest },
    response: { body: { code: "ONLY-CODE", expires_at: "2026-11-15T10:10:00Z", device_lifetime: "30d" } },
  });
  const copied = reduce(answered.state, { type: "extension/devices-copy-requested" });
  const effect = copied.effects.find((item) => item.name === "extension.devices.write");

  assert.deepEqual(effect.args, ["ONLY-CODE"]);
  assert.equal(JSON.stringify(effect.args).includes("10:10"), false);
  assert.equal(JSON.stringify(effect.args).includes("token"), false);
});


test("pending-device limit explains recovery without exposing server error text", () => {
  const requested = reduce(open().state, { type: "extension/devices-code-requested" });
  const failed = reduce(requested.state, {
    type: "extension/devices-code-failed",
    meta: { request: requested.state.codeRequest },
    error: { status: 429, body: { error: { code: "pending_limit", message: "private debug details" } } },
  });
  assert.equal(failed.state.pendingCode, false);
  assert.equal(failed.state.ui, "focus-retry");
  assert.match(failed.state.error.text.text, /ghép hoặc thu hồi/);
  assert.doesNotMatch(failed.state.error.text.text, /private debug details/);
});

test("a failed re-pair keeps its selected row as an actionable focus target", () => {
  const ready = loaded().state;
  const requested = reduce(ready, {
    type: "extension/devices-repair-requested",
    deviceId: "record-1",
  });
  const failed = reduce(requested.state, {
    type: "extension/devices-code-failed",
    meta: { request: requested.state.codeRequest },
    error: { status: 503, body: { error: { code: "service_unavailable", message: "private" } } },
  });

  assert.equal(failed.state.pendingCode, false);
  assert.equal(failed.state.pairingFor, "record-1");
  assert.equal(failed.state.ui, "focus-retry");
  assert.equal(selectDevices(failed.state).rows[0].canRepair, true);
});

test("a delayed pairing answer after dialog close is ignored", () => {
  const opened = open();
  const requested = reduce(opened.state, { type: "extension/devices-code-requested" });
  const closed = reduce(requested.state, { type: "provider/dialog-closed" });
  const late = reduce(closed.state, {
    type: "extension/devices-code-failed",
    meta: { request: requested.state.codeRequest },
    error: { status: 429, body: { error: { code: "pending_limit", message: "private" } } },
  });

  assert.strictEqual(late.state, closed.state);
  assert.deepEqual(late.effects, []);
});

test("a delayed revoke answer after dialog close is ignored", () => {
  const ready = loaded().state;
  const confirmed = reduce(ready, {
    type: "extension/devices-revoke-requested",
    deviceId: "record-1",
  });
  const requested = reduce(confirmed.state, {
    type: "extension/devices-revoke-confirmed",
    deviceId: "record-1",
  });
  const closed = reduce(requested.state, { type: "provider/dialog-closed" });
  const late = reduce(closed.state, {
    type: "extension/devices-revoked",
    meta: { deviceId: "record-1", request: requested.state.revokeRequest },
    response: { body: { id: "record-1", revoked: true } },
  });

  assert.strictEqual(late.state, closed.state);
  assert.deepEqual(late.effects, []);
});
