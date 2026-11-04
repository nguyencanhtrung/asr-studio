import assert from "node:assert/strict";
import test from "node:test";

import { createRunner, createStore } from "../../src/asr_studio/web/store.js";
import { ANTIGRAVITY, CHATGPT, SETTINGS_SECTION_KEY, initialState, reduce, selectAsr, selectLauncher, selectView } from "../../src/asr_studio/web/provider-core.js";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const T0 = Date.parse("2026-01-01T00:00:00Z");
const LATER = new Date(T0 + 60_000).toISOString();
const SOON = new Date(T0 + 600_000).toISOString();

const LIST = {
  providers: [
    { id: "chatgpt", status: "connected", account: { label: "a@example.com" }, in_use: false, in_use_reason: null },
    { id: "antigravity", status: "connected", account: { label: "g@example.com" }, in_use: false, in_use_reason: null },
  ],
};
const LIST_IN_USE = {
  providers: [
    { id: "chatgpt", status: "connected", account: { label: "a@example.com" }, in_use: true, in_use_reason: "notes" },
    { id: "antigravity", status: "connected", account: { label: "g@example.com" }, in_use: false, in_use_reason: null },
  ],
};
const LIST_OUT = { providers: [{ id: "chatgpt", status: "disconnected" }, { id: "antigravity", status: "disconnected" }] };
const MODELS = {
  chatgpt: { models: [{ id: "m1", display_name: "M1", reasoning_efforts: ["low", "high"] }, { id: "m2", display_name: "M2" }], selected_model: "m1" },
  antigravity: { models: [{ id: "g1", display_name: "G1" }, { id: "g2", display_name: "G2" }], selected_model: "g1", reasoning_effort: "medium" },
};
const PREFS = { provider: "chatgpt", models: { chatgpt: "m1" }, reasoning_effort: "low" };

// Per provider: the second model to pick and the endpoint of its save request.
const CASES = [
  { d: CHATGPT, other: "m2", save: ["PUT", "/api/notes-preferences"], login: "popup" },
  { d: ANTIGRAVITY, other: "g2", save: ["PUT", "/api/providers/antigravity/preference"], login: "code" },
];

// Real store and runner over fakes: requests wait in `calls` until a test answers them; timers wait in `timers`.
function setup() {
  const calls = [];
  const fetch = (url, init) =>
    new Promise((resolve, reject) => {
      calls.push({ url, init, resolve, reject, method: init.method, body: init.body && JSON.parse(init.body), done: false });
    });
  const clock = { now: T0 };
  const timers = [];
  const written = [];
  const events = [];
  const clipboard = { refuse: false, written: [], selected: [] };
  const opened = { blocked: false, popup: null, replaced: [] };
  const open = () => {
    if (opened.blocked) return null;
    opened.popup = {
      closed: false,
      opener: "set",
      document: { title: "", body: { textContent: "" } },
      location: { replace: (url) => opened.replaced.push(url) },
      close() {
        this.closed = true;
      },
    };
    return opened.popup;
  };
  const run = createRunner({
    fetch,
    setTimeout: (callback, delay) => timers.push({ callback, delay, live: true }) - 1,
    clearTimeout: (id) => {
      if (timers[id]) timers[id].live = false;
    },
    XMLHttpRequest: class {},
    storage: { session: () => ({ setItem: (k, v) => written.push(["write", k, v]), removeItem: (k) => written.push(["remove", k]) }) },
    navigate: () => {},
    open,
    calls: {
      "provider.choice-changed": () => events.push("provider.choice-changed"),
      "clipboard.write": async (text) => {
        if (clipboard.refuse) throw new Error("denied");
        clipboard.written.push(text);
      },
      "asr.select-command": (id) => clipboard.selected.push(id),
    },
    now: () => clock.now,
  });
  const store = createStore({ reduce, initialState, run });
  const answer = async (match, status, body) => {
    const call = calls.find((c) => !c.done && match(c));
    assert.ok(call, "no pending call");
    call.done = true;
    call.resolve(reply(status, body));
    await tick();
  };
  const fail = async (match) => {
    const call = calls.find((c) => !c.done && match(c));
    assert.ok(call, "no pending call");
    call.done = true;
    call.reject(new TypeError("offline"));
    await tick();
  };
  const is = (method, path) => (c) => c.method === method && c.url.includes(path);
  const pending = (match) => calls.filter((c) => !c.done && match(c));
  const fire = (delay) => {
    const timer = timers.find((item) => item.live && (delay === undefined || item.delay === delay));
    assert.ok(timer, `no timer ${delay}`);
    timer.live = false;
    timer.callback();
  };
  const live = () => timers.filter((item) => item.live).map((item) => item.delay);
  return { store, calls, answer, fail, is, pending, fire, live, clock, written, events, clipboard, opened, state: () => store.getState() };
}

const own = (env, d) => env.state()[d.id];
const dispatch = (env, d, type, extra = {}) => env.store.dispatch({ type: `provider/${type}`, provider: d.id, ...extra });

// Open the dialog and answer both loads: the provider is connected with its catalog.
async function loaded(env, { list = LIST, open = true } = {}) {
  if (open) env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 200, list);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, MODELS.chatgpt);
  await env.answer(env.is("GET", "/api/providers"), 200, list);
  if (list.providers.some((item) => item.id === "antigravity" && item.status === "connected")) {
    await env.answer(env.is("GET", "/api/providers/antigravity/models"), 200, MODELS.antigravity);
  }
}

// ---- settings sections ----

test("the settings dialog opens at AI provider, or at the section a language change left", () => {
  const opened = (state, action) => reduce(state, { type: "provider/dialog-opened", ...action });
  const selected = reduce(initialState, { type: "provider/settings-section-selected", section: "about" }).state;
  assert.equal(selected.launcher.section, "about");
  assert.equal(opened(selected, { launcher: null }).state.launcher.section, "ai");
  assert.equal(opened(selected, { launcher: "chatgpt" }).state.launcher.section, "ai");

  const reopened = opened(initialState, { launcher: null, section: "theme" });
  assert.equal(reopened.state.launcher.section, "theme");
  const removal = reopened.effects.filter((effect) => effect.key === SETTINGS_SECTION_KEY);
  assert.deepEqual(removal, [{ type: "storage", key: SETTINGS_SECTION_KEY, area: "session", op: "remove" }]);
  assert.equal(opened(initialState, { launcher: null }).effects.some((effect) => effect.key === SETTINGS_SECTION_KEY), false);
});

test("choosing a section drops a pending scroll to the Google part; the scroll keeps the section", () => {
  const pending = reduce(initialState, { type: "provider/dialog-opened", launcher: "antigravity" }).state;
  assert.equal(pending.launcher.scroll, true);
  const moved = reduce(pending, { type: "provider/settings-section-selected", section: "asr" }).state;
  assert.deepEqual(moved.launcher, { section: "asr", scroll: false });
  const handled = reduce(pending, { type: "provider/scroll-handled" }).state;
  assert.deepEqual(handled.launcher, { section: "ai", scroll: false });
});

// ---- load ----

test("opening the dialog loads status, preferences and catalog for both providers", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "antigravity" });
  assert.equal(own(env, CHATGPT).badge.state, "loading");
  assert.equal(own(env, CHATGPT).launcher.status, "loading");
  assert.equal(env.state().launcher.scroll, true);
  assert.equal(own(env, ANTIGRAVITY).badge.state, "loading"); // Google keeps its markup until the answer
  await loaded(env, { list: LIST, open: false });
  const chatgpt = own(env, CHATGPT);
  assert.equal(chatgpt.badge.state, "connected");
  assert.deepEqual(chatgpt.models.map((m) => m.id), ["m1", "m2"]);
  assert.equal(chatgpt.selectedModel, "m1");
  assert.equal(chatgpt.selectedEffort, "low");
  assert.deepEqual(selectView(CHATGPT, chatgpt).effortOptions.map((o) => o.value), ["default", "low", "high"]);
  const google = own(env, ANTIGRAVITY);
  assert.equal(google.savedModel, "g1");
  assert.equal(google.selectedEffort, "medium");
  assert.equal(selectLauncher(ANTIGRAVITY, google).status, "connected");
  assert.equal(chatgpt.loading || google.loading, false);
});

test("in-use metadata blocks only the pinned provider and keeps the other provider usable", async () => {
  const env = setup();
  await loaded(env, { list: LIST_IN_USE });
  const pinned = own(env, CHATGPT);
  const before = pinned.selectedModel;
  dispatch(env, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  dispatch(env, CHATGPT, "save-requested");
  dispatch(env, CHATGPT, "verify-requested");
  dispatch(env, CHATGPT, "disconnect-confirm-toggled", { open: true });
  assert.equal(pinned.selectedModel, before);
  assert.equal(pinned.confirming, false);
  assert.equal(env.pending((call) => call.url.includes("/api/notes-preferences")).length, 0);
  assert.equal(selectView(CHATGPT, pinned).modelDisabled, true);
  assert.equal(selectView(CHATGPT, pinned).disconnectDisabled, true);
  assert.match(selectView(CHATGPT, pinned).mutationReason.text, /đổi model/);
  assert.equal(selectView(ANTIGRAVITY, own(env, ANTIGRAVITY)).modelDisabled, false);
  assert.equal(selectView(ANTIGRAVITY, own(env, ANTIGRAVITY)).disconnectDisabled, false);
});

test("a provider_in_use catalog refusal preserves the known connected provider state", async () => {
  const env = setup();
  await loaded(env);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 200, LIST_IN_USE);
  await env.answer(env.is("GET", "/api/providers"), 200, LIST_IN_USE);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 409, { detail: { code: "provider_in_use" } });
  await env.answer(env.is("GET", "/api/providers/antigravity/models"), 200, MODELS.antigravity);
  const s = own(env, CHATGPT);
  assert.equal(s.info.status, "connected");
  assert.equal(s.info.in_use, true);
  assert.equal(s.badge.state, "connected");
  assert.equal(s.info.account.label, "a@example.com");
  assert.deepEqual(s.models.map((model) => model.id), ["m1", "m2"]);
  assert.equal(s.error.code, "provider_in_use");
});

test("dedicated provider copy wins over a generic HTTP status, while transport loss stays distinct", async () => {
  const unavailable = setup();
  await loaded(unavailable);
  dispatch(unavailable, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  dispatch(unavailable, CHATGPT, "save-requested");
  await unavailable.answer(unavailable.is("PUT", "/api/notes-preferences"), 503, {
    detail: { code: "provider_unavailable", message: "The provider is offline." },
  });
  assert.match(unavailable.state().chatgpt.error.text.text, /tạm thời không khả dụng/);
  assert.doesNotMatch(unavailable.state().chatgpt.error.text.text, /không kết nối được Internet/);

  const network = setup();
  await loaded(network);
  dispatch(network, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  dispatch(network, CHATGPT, "save-requested");
  await network.fail(network.is("PUT", "/api/notes-preferences"));
  assert.deepEqual(network.state().chatgpt.error.text, { text: "Không kết nối được với máy chủ trên máy này." });
});

test("ChatGPT sign-in keeps its recovery copy on a 503", async () => {
  const env = setup();
  dispatch(env, CHATGPT, "login-requested");
  await env.answer(env.is("POST", "/api/providers/chatgpt/login-sessions"), 503, {
    detail: { code: "SIGN_IN_FAILED", message: "The sign-in service failed." },
  });
  assert.deepEqual(own(env, CHATGPT).error.text, { text: "Không thể hoàn tất đăng nhập ChatGPT." });
});

for (const { d, other } of CASES) {
  test(`${d.id}: a load answer with an old request id is stale`, async () => {
    const env = setup();
    env.store.dispatch({ type: "provider/dialog-opened", launcher: d.id });
    const first = own(env, d).loadId;
    env.store.dispatch({ type: "provider/dialog-opened", launcher: d.id });
    assert.equal(own(env, d).loadId, first + 1);
    // Reaches the reducer even if the runner had not dropped it.
    env.store.dispatch({
      type: "provider/response",
      meta: { provider: d.id, op: "providers", id: first },
      response: { status: 200, body: LIST },
    });
    assert.equal(own(env, d).info, null);
    await env.answer((c) => c.url.includes("/api/providers") && !c.done && c.method === "GET", 200, LIST);
    assert.ok(other);
  });

  test(`${d.id}: a network error on load shows the error and the unavailable badge`, async () => {
    const env = setup();
    env.store.dispatch({ type: "provider/dialog-opened", launcher: d.id });
    await env.fail(env.is("GET", "/api/providers"));
    await env.fail(env.is("GET", "/api/providers"));
    const s = own(env, d);
    assert.equal(s.badge.state, "error");
    assert.equal(s.error.code, "NETWORK_ERROR");
    assert.equal(s.launcher.status, "error");
    assert.equal(s.loading, false);
    assert.deepEqual(s.error.text, { text: "Không kết nối được với máy chủ trên máy này." });
  });
}

test("a failed load after a known status keeps the last status (ChatGPT)", async () => {
  const env = setup();
  await loaded(env);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.fail(env.is("GET", "/api/providers"));
  await env.fail(env.is("GET", "/api/providers"));
  const s = own(env, CHATGPT);
  assert.equal(s.badge.state, "connected");
  assert.equal(s.error.code, "NETWORK_ERROR");
});

test("an invalid ChatGPT status answer is an error; a missing Google entry too", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 200, { providers: [{ id: "chatgpt", status: "weird" }] });
  await env.answer(env.is("GET", "/api/providers"), 200, { providers: [] });
  assert.equal(own(env, CHATGPT).error.code, "INVALID_PROVIDER_STATUS");
  assert.equal(own(env, ANTIGRAVITY).error.code, "INVALID_PROVIDER_STATUS");
  assert.equal(own(env, ANTIGRAVITY).badge.state, "error");
});

test("the page-load status reads both buttons; a newer dialog read wins", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/header-requested" });
  await env.answer(env.is("GET", "/api/providers"), 200, LIST);
  assert.equal(own(env, CHATGPT).launcher.status, "connected");
  assert.equal(own(env, ANTIGRAVITY).launcher.status, "connected");

  const slow = setup();
  slow.store.dispatch({ type: "provider/header-requested" });
  slow.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" }); // newer ChatGPT read
  await slow.answer((c) => c.url.includes("/api/providers") && c.method === "GET" && c.init.headers.Accept && true, 200, LIST_OUT);
  const first = own(slow, CHATGPT);
  assert.notEqual(first.launcher?.status, "disconnected"); // the loading state stays; the stale header answer is dropped
});

test("the page-load status read failing sets both buttons to error", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/header-requested" });
  await env.fail(env.is("GET", "/api/providers"));
  assert.equal(own(env, CHATGPT).launcher.status, "error");
  assert.equal(own(env, ANTIGRAVITY).launcher.status, "error");
});

test("a page-load answer does not overwrite a Google status the dialog already showed", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/header-requested" });
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "antigravity" });
  const [header, chatgptRead, googleRead] = env.calls;
  assert.equal(header.init.method, "GET");
  for (const call of [chatgptRead, googleRead]) {
    call.done = true;
    call.resolve(reply(200, LIST));
  }
  await tick();
  header.done = true;
  header.resolve(reply(200, LIST_OUT)); // the slower page-load answer
  await tick();
  assert.equal(own(env, ANTIGRAVITY).launcher.status, "connected");
});

// ---- repeated actions while busy ----

for (const { d, other, save } of CASES) {
  test(`${d.id}: save, verify and disconnect ignore a second request while one runs`, async () => {
    const env = setup();
    await loaded(env);
    dispatch(env, d, "choice-edited", { field: "model", value: other });
    dispatch(env, d, "save-requested");
    dispatch(env, d, "save-requested");
    assert.equal(env.pending(env.is(...save)).length, 1);
    assert.equal(selectView(d, own(env, d)).saveDisabled, true);
    await env.answer(env.is(...save), 200, d === CHATGPT ? { models: { chatgpt: other }, reasoning_effort: "default" } : {});
    assert.equal(own(env, d).savedModel, other);
    assert.deepEqual(env.events, ["provider.choice-changed"]);

    dispatch(env, d, "verify-requested");
    dispatch(env, d, "verify-requested");
    assert.equal(env.pending(env.is("POST", "/verify")).length, 1);
    await env.answer(env.is("POST", "/verify"), 200, { model: other, reasoning_effort: "default", verified_at: LATER });
    assert.ok(own(env, d).summary);
    assert.equal(own(env, d).verifyResult.state, "success");

    dispatch(env, d, "disconnect-confirm-toggled", { open: true });
    dispatch(env, d, "disconnect-requested");
    dispatch(env, d, "disconnect-requested");
    assert.equal(env.pending(env.is("DELETE", "/connection")).length, 1);
  });
}

test("Google runs one operation at a time; ChatGPT lets save and verify overlap", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, ANTIGRAVITY, "choice-edited", { field: "model", value: "g2" });
  dispatch(env, ANTIGRAVITY, "save-requested");
  dispatch(env, ANTIGRAVITY, "verify-requested");
  assert.equal(env.pending(env.is("POST", "/api/providers/antigravity/verify")).length, 0);
  dispatch(env, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  dispatch(env, CHATGPT, "save-requested");
  dispatch(env, CHATGPT, "verify-requested");
  assert.equal(env.pending(env.is("POST", "/api/providers/chatgpt/verify")).length, 1);
});

// ---- model choice ----

test("ChatGPT: changing the model resets the effort; the save request carries both", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, CHATGPT, "choice-edited", { field: "effort", value: "high" });
  assert.equal(own(env, CHATGPT).modelStatus.text.text, "Mức suy luận mới chưa được lưu.");
  dispatch(env, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  assert.equal(own(env, CHATGPT).selectedEffort, "default");
  dispatch(env, CHATGPT, "save-requested");
  const call = env.pending(env.is("PUT", "/api/notes-preferences"))[0];
  assert.deepEqual(call.body, { provider: "chatgpt", model: "m2", reasoning_effort: "default" });
});

test("Google: effort edits mark the choice unsaved until it matches the saved one again", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, ANTIGRAVITY, "choice-edited", { field: "effort", value: "high" });
  assert.equal(own(env, ANTIGRAVITY).modelStatus.text.text, "Lựa chọn mới chưa được lưu.");
  dispatch(env, ANTIGRAVITY, "choice-edited", { field: "effort", value: "medium" });
  assert.equal(own(env, ANTIGRAVITY).modelStatus.text, null);
  assert.equal(selectView(ANTIGRAVITY, own(env, ANTIGRAVITY)).saveDisabled, true);
});

test("a saved model that left the catalog stays visible as a disabled option", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 200, LIST);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, { ...PREFS, models: { chatgpt: "gone" } });
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, MODELS.chatgpt);
  const s = own(env, CHATGPT);
  assert.equal(s.modelOptions[0].value, "gone");
  assert.equal(s.modelOptions[0].disabled, true);
  assert.equal(s.modelStatus.state, "error");
  assert.equal(selectView(CHATGPT, s).saveDisabled, true);
});

// ---- reconnect required ----

for (const { d, other, save } of CASES) {
  test(`${d.id}: a reconnect-required save error refreshes the status and never replays the save`, async () => {
    const env = setup();
    await loaded(env);
    dispatch(env, d, "choice-edited", { field: "model", value: other });
    dispatch(env, d, "save-requested");
    await env.answer(env.is(...save), 409, { detail: { code: "reconnect_required" } });
    const waiting = own(env, d);
    assert.equal(waiting.pending.save, true); // still busy until the status read ends
    if (d === ANTIGRAVITY) {
      assert.equal(waiting.info.status, "reconnect_required"); // the conservative state applies at once
      assert.deepEqual(waiting.models, []);
    }
    await env.answer(env.is("GET", "/api/providers"), 200, { providers: [{ id: d.id, status: "reconnect_required" }] });
    const s = own(env, d);
    assert.equal(s.info.status, "reconnect_required");
    assert.equal(s.pending.save, false);
    assert.equal(s.error.code, "reconnect_required");
    assert.equal(env.calls.filter((c) => c.method === "PUT").length, 1);
    assert.deepEqual(env.events, []);
  });

  test(`${d.id}: the status read after a reconnect error failing keeps the state and shows the error`, async () => {
    const env = setup();
    await loaded(env);
    dispatch(env, d, "choice-edited", { field: "model", value: other });
    dispatch(env, d, "save-requested");
    await env.answer(env.is(...save), 409, { detail: { code: "reconnect_required" } });
    await env.fail(env.is("GET", "/api/providers"));
    assert.equal(own(env, d).pending.save, false);
    assert.equal(own(env, d).error.code, "reconnect_required");
  });
}

test("ChatGPT: a 401 is the workspace session, not a provider reconnect", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, CHATGPT, "choice-edited", { field: "model", value: "m2" });
  dispatch(env, CHATGPT, "save-requested");
  await env.answer(env.is("PUT", "/api/notes-preferences"), 401, { detail: { code: "reconnect_required" } });
  assert.equal(env.pending(env.is("GET", "/api/providers")).length, 0);
  assert.equal(own(env, CHATGPT).modelStatus.state, "error");
  assert.equal(own(env, CHATGPT).pending.save, false);
});

test("a failed verify shows the failure text and is not sent again", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, CHATGPT, "verify-requested");
  await env.answer(env.is("POST", "/api/providers/chatgpt/verify"), 500, {});
  assert.equal(own(env, CHATGPT).verifyResult.state, "error");
  assert.equal(env.calls.filter((c) => c.url.includes("/verify")).length, 1);
});

// ---- disconnect ----

test("ChatGPT: disconnect asks first, needs a disconnected answer, and tells notes.js", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, CHATGPT, "disconnect-confirm-toggled", { open: true });
  assert.equal(own(env, CHATGPT).confirming, true);
  assert.equal(own(env, CHATGPT).ui, "focus:disconnect-confirm");
  dispatch(env, CHATGPT, "disconnect-requested");
  await env.answer(env.is("DELETE", "/connection"), 200, { status: "connected" });
  assert.equal(own(env, CHATGPT).error.code, "DISCONNECT_NOT_CONFIRMED");
  assert.equal(own(env, CHATGPT).confirming, true);
  assert.deepEqual(env.events, []);
  dispatch(env, CHATGPT, "disconnect-requested");
  await env.answer(env.is("DELETE", "/connection"), 200, { status: "disconnected", remote_revocation_confirmed: true });
  const s = own(env, CHATGPT);
  assert.equal(s.info.status, "disconnected");
  assert.equal(s.confirming, false);
  assert.deepEqual(s.models, []);
  assert.equal(s.ui, "focus:sign-in");
  assert.equal(s.message.text.startsWith("Đã xóa thông tin"), true);
  assert.deepEqual(env.events, ["provider.choice-changed"]);
});

test("Google: disconnect confirms locally, then tells notes.js; the busy flag holds until then", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, ANTIGRAVITY, "disconnect-confirm-toggled", { open: true });
  assert.equal(own(env, ANTIGRAVITY).confirming, true);
  assert.equal(own(env, ANTIGRAVITY).ui, "focus:disconnect-confirm");
  dispatch(env, ANTIGRAVITY, "disconnect-requested");
  await env.answer(env.is("DELETE", "/api/providers/antigravity/connection"), 200, { status: "disconnected", remote_revocation_confirmed: false });
  const s = own(env, ANTIGRAVITY);
  assert.equal(s.info.status, "disconnected");
  assert.equal(s.pending.disconnect, false);
  assert.equal(s.ui, "focus:sign-in");
  assert.deepEqual(env.events, ["provider.choice-changed"]);
});

test("a failed disconnect keeps the connection and frees the button", async () => {
  for (const { d } of CASES) {
    const env = setup();
    await loaded(env);
    dispatch(env, d, "disconnect-confirm-toggled", { open: true });
    dispatch(env, d, "disconnect-requested");
    await env.fail(env.is("DELETE", "/connection"));
    assert.equal(own(env, d).pending.disconnect, false);
    assert.equal(own(env, d).info.status, "connected");
    assert.equal(own(env, d).error.code, "NETWORK_ERROR");
  }
});

// ---- ChatGPT login ----

const SESSION = { id: "s1", status: "pending", expires_at: SOON, authorization_url: "https://auth.openai.com/authorize?x=1" };

async function chatgptLogin(env, session = SESSION) {
  dispatch(env, CHATGPT, "login-requested");
  await env.answer(env.is("POST", "/api/providers/chatgpt/login-sessions"), 200, session);
}

test("ChatGPT: login reserves the window first, sends it to the URL and polls", async () => {
  const env = setup();
  dispatch(env, CHATGPT, "login-requested");
  assert.equal(env.opened.popup.opener, null);
  assert.equal(env.opened.popup.document.title, "Continue with ChatGPT");
  assert.equal(own(env, CHATGPT).signInDisabled, true);
  await env.answer(env.is("POST", "/login-sessions"), 200, SESSION);
  assert.deepEqual(env.opened.replaced, ["https://auth.openai.com/authorize?x=1"]);
  assert.deepEqual(env.live(), [500]);
  assert.deepEqual(env.written.at(-1), ["write", "asr.chatgpt.pendingLogin", JSON.stringify({ id: "s1", expiresAt: SOON })]);
  env.fire(500);
  await env.answer(env.is("GET", "/login-sessions/s1"), 200, { status: "pending" });
  assert.deepEqual(env.live(), [1000]);
  env.fire(1000);
  await env.answer(env.is("GET", "/login-sessions/s1"), 200, { status: "connected" });
  const s = own(env, CHATGPT);
  assert.equal(s.login, null);
  assert.equal(s.message.text, "Đã xác thực tài khoản ChatGPT.");
  assert.deepEqual(env.written.at(-1), ["remove", "asr.chatgpt.pendingLogin"]);
  assert.deepEqual(env.events, ["provider.choice-changed"]);
  assert.equal(env.pending(env.is("GET", "/api/providers")).length, 1); // the account reload
});

test("ChatGPT: a blocked window ends the login before any request", () => {
  const env = setup();
  env.opened.blocked = true;
  dispatch(env, CHATGPT, "login-requested");
  assert.equal(env.calls.length, 0);
  assert.equal(own(env, CHATGPT).error.code, "POPUP_BLOCKED");
  assert.equal(own(env, CHATGPT).signInDisabled, false);
});

test("ChatGPT: a second sign-in click while the first starts sends one request", async () => {
  const env = setup();
  dispatch(env, CHATGPT, "login-requested");
  dispatch(env, CHATGPT, "login-requested");
  assert.equal(env.pending(env.is("POST", "/login-sessions")).length, 1);
});

test("ChatGPT: an unsafe authorization URL or a bad session closes the window and frees the button", async () => {
  for (const session of [
    { ...SESSION, authorization_url: "https://evil.example/x" },
    { ...SESSION, authorization_url: "https://auth.openai.com/a?id_token_hint=z" },
    { ...SESSION, authorization_url: "nope" },
    { id: "s1", status: "failed" },
  ]) {
    const env = setup();
    await chatgptLogin(env, session);
    assert.equal(env.opened.popup.closed, true);
    assert.equal(own(env, CHATGPT).signInDisabled, false);
    assert.equal(own(env, CHATGPT).login, null);
    assert.ok(own(env, CHATGPT).error);
  }
  const env = setup();
  dispatch(env, CHATGPT, "login-requested");
  await env.fail(env.is("POST", "/login-sessions"));
  assert.equal(env.opened.popup.closed, true);
  assert.equal(own(env, CHATGPT).error.code, "NETWORK_ERROR");
});

test("ChatGPT: a window the user closed shows an error but the poll still runs", async () => {
  const env = setup();
  dispatch(env, CHATGPT, "login-requested");
  env.opened.popup.closed = true;
  await env.answer(env.is("POST", "/login-sessions"), 200, SESSION);
  assert.equal(own(env, CHATGPT).error.code, "POPUP_BLOCKED");
  assert.deepEqual(env.opened.replaced, []);
  assert.deepEqual(env.live(), [500]);
});

test("ChatGPT: a login that expires between the answer and the first poll ends as expired", async () => {
  const env = setup();
  await chatgptLogin(env, { ...SESSION, expires_at: LATER });
  env.clock.now = T0 + 120_000;
  env.fire(500);
  assert.equal(own(env, CHATGPT).message.text.startsWith("Phiên đăng nhập đã hết hạn"), true);
  assert.equal(own(env, CHATGPT).login, null);
  assert.equal(env.pending(env.is("GET", "/login-sessions/s1")).length, 0);

  const late = setup();
  late.clock.now = T0 + 120_000; // already past when the answer arrives
  dispatch(late, CHATGPT, "login-requested");
  await late.answer(late.is("POST", "/login-sessions"), 200, { ...SESSION, expires_at: LATER });
  assert.equal(late.opened.popup.closed, true);
  assert.deepEqual(late.opened.replaced, []);
  assert.equal(own(late, CHATGPT).login, null);
});

test("ChatGPT: a pending answer past expiry ends the login", async () => {
  const env = setup();
  await chatgptLogin(env, { ...SESSION, expires_at: LATER });
  env.fire(500);
  env.clock.now = T0 + 120_000;
  await env.answer(env.is("GET", "/login-sessions/s1"), 200, { status: "pending" });
  assert.equal(own(env, CHATGPT).login, null);
  assert.equal(own(env, CHATGPT).message.text.startsWith("Phiên đăng nhập đã hết hạn"), true);
});

test("ChatGPT: a poll error offers a retry; the retry polls again", async () => {
  const env = setup();
  await chatgptLogin(env);
  env.fire(500);
  await env.fail(env.is("GET", "/login-sessions/s1"));
  const s = own(env, CHATGPT);
  assert.equal(s.error.retryPoll, true);
  assert.equal(s.error.code, "NETWORK_ERROR");
  assert.deepEqual(env.live(), []); // no automatic retry
  dispatch(env, CHATGPT, "poll-retry-requested");
  assert.equal(own(env, CHATGPT).error, null);
  assert.deepEqual(env.live(), [0]);
  env.fire(0);
  await env.answer(env.is("GET", "/login-sessions/s1"), 200, { status: "mystery" });
  assert.equal(own(env, CHATGPT).error.code, "INVALID_LOGIN_STATUS");
});

test("ChatGPT: cancelling mid-way drops the poll in flight and ends the login", async () => {
  const env = setup();
  await chatgptLogin(env);
  env.fire(500);
  const gen = own(env, CHATGPT).loginGen;
  dispatch(env, CHATGPT, "login-cancel-requested");
  assert.equal(own(env, CHATGPT).loginGen, gen + 1);
  assert.equal(own(env, CHATGPT).cancelling, true);
  // The poll answer that left before the cancel is stale, at the reducer too.
  env.store.dispatch({ type: "provider/response", meta: { provider: "chatgpt", op: "poll", gen }, response: { body: { status: "connected" } }, now: T0 });
  assert.notEqual(own(env, CHATGPT).login, null);
  await env.answer(env.is("DELETE", "/login-sessions/s1"), 200, { status: "cancelled" });
  const s = own(env, CHATGPT);
  assert.equal(s.login, null);
  assert.equal(s.message.text, "Đã hủy đăng nhập ChatGPT.");
  assert.deepEqual(env.events, []);
  assert.deepEqual(env.written.at(-1), ["remove", "asr.chatgpt.pendingLogin"]);
});

test("a cancel answer from an older cancel is ignored, answer or error (both providers)", async () => {
  for (const { d } of CASES) {
    const env = setup();
    if (d === CHATGPT) await chatgptLogin(env);
    else await googleLogin(env);
    dispatch(env, d, "login-cancel-requested");
    const stale = { provider: d.id, op: "cancel", gen: own(env, d).loginGen - 1 };
    env.store.dispatch({ type: "provider/response", meta: stale, response: { body: { status: "cancelled" } } });
    env.store.dispatch({ type: "provider/request-failed", meta: stale, error: { message: "x", status: 500, body: {} } });
    assert.notEqual(own(env, d).login, null);
    assert.equal(own(env, d).error, null);
  }
});

test("ChatGPT: a cancel the server does not confirm keeps the login and polls again", async () => {
  const env = setup();
  await chatgptLogin(env);
  dispatch(env, CHATGPT, "login-cancel-requested");
  await env.answer(env.is("DELETE", "/login-sessions/s1"), 200, { status: "pending" });
  assert.equal(own(env, CHATGPT).error.code, "CANCEL_NOT_CONFIRMED");
  assert.equal(own(env, CHATGPT).cancelling, false);
  assert.notEqual(own(env, CHATGPT).login, null);
  assert.deepEqual(env.live(), [1000]);
});

test("ChatGPT: a failed login shows the server's error and the failure message", async () => {
  const env = setup();
  await chatgptLogin(env);
  env.fire(500);
  await env.answer(env.is("GET", "/login-sessions/s1"), 200, { status: "failed", error: { code: "access_denied", message: "Bị từ chối" } });
  const s = own(env, CHATGPT);
  assert.equal(s.error.code, "access_denied");
  assert.equal(s.error.text, "Bị từ chối");
  assert.equal(s.message.text, "Đăng nhập ChatGPT chưa hoàn tất.");
});

test("ChatGPT: a replaced login says so", async () => {
  const env = setup();
  await chatgptLogin(env, { ...SESSION, replaced_previous: true });
  assert.equal(own(env, CHATGPT).message.text.startsWith("Đã thay thế phiên đăng nhập"), true);
});

// ---- Google login ----

const CODE_SESSION = {
  id: "g1",
  status: "pending",
  expires_at: SOON,
  instructions: "Mở trang rồi dán mã.",
  authorization_url: "https://accounts.example.com/auth",
  input_prompt: "Mã xác nhận",
};

async function googleLogin(env, session = CODE_SESSION) {
  dispatch(env, ANTIGRAVITY, "login-requested");
  await env.answer(env.is("POST", "/api/providers/antigravity/login-sessions"), 200, session);
}

test("Google: login shows the link and the code input, polls at once and again every second", async () => {
  const env = setup();
  await googleLogin(env);
  const s = own(env, ANTIGRAVITY);
  assert.equal(s.loginLink, "https://accounts.example.com/auth");
  assert.equal(s.inputOpen, true);
  assert.equal(s.loginCopy, "Mở trang rồi dán mã.");
  assert.equal(s.signInDisabled, false);
  await env.answer(env.is("GET", "/login-sessions/g1"), 200, { ...CODE_SESSION });
  assert.deepEqual(env.live(), [1000]);
  env.fire(1000);
  await env.answer(env.is("GET", "/login-sessions/g1"), 200, { status: "connected" });
  assert.equal(own(env, ANTIGRAVITY).login, null);
  assert.equal(own(env, ANTIGRAVITY).loginLink, null);
  assert.equal(own(env, ANTIGRAVITY).message.text, "Đã kết nối Google Antigravity qua OMP.");
  assert.deepEqual(env.events, ["provider.choice-changed"]);
});

test("Google: an unsafe or malformed link is an error and does not start polling", async () => {
  for (const url of ["http://example.com/x", "nope", "https://u:p@example.com/"]) {
    const env = setup();
    await googleLogin(env, { ...CODE_SESSION, authorization_url: url });
    assert.equal(own(env, ANTIGRAVITY).error.code, "INVALID_AUTHORIZATION_URL");
    assert.equal(env.pending(env.is("GET", "/login-sessions/g1")).length, 0);
    assert.equal(own(env, ANTIGRAVITY).signInDisabled, false);
  }
  const env = setup();
  await googleLogin(env, { ...CODE_SESSION, authorization_url: "http://127.0.0.1:1455/cb" });
  assert.equal(own(env, ANTIGRAVITY).loginLink, "http://127.0.0.1:1455/cb");
});

test("Google: submitting the code posts it trimmed, clears the field and applies the answer", async () => {
  const env = setup();
  await googleLogin(env);
  dispatch(env, ANTIGRAVITY, "login-input-submitted", { value: "   " });
  assert.equal(env.pending(env.is("POST", "/input")).length, 0);
  dispatch(env, ANTIGRAVITY, "login-input-submitted", { value: " code-1 " });
  const call = env.pending(env.is("POST", "/login-sessions/g1/input"))[0];
  assert.deepEqual(call.body, { value: "code-1" });
  await env.answer(env.is("POST", "/input"), 200, { id: "g1", status: "pending", expires_at: SOON });
  const s = own(env, ANTIGRAVITY);
  assert.equal(s.inputOpen, false);
  assert.equal(s.ui, "clear-input");
  dispatch(env, ANTIGRAVITY, "login-input-submitted", { value: "x" });
  await env.fail(env.is("POST", "/input"));
  assert.equal(own(env, ANTIGRAVITY).error.code, "NETWORK_ERROR");
});

test("Google: a login the server forgot ends as expired; other poll errors retry in two seconds", async () => {
  const env = setup();
  await googleLogin(env);
  await env.answer(env.is("GET", "/login-sessions/g1"), 404, { detail: { code: "login_session_not_found" } });
  assert.equal(own(env, ANTIGRAVITY).login, null);
  assert.equal(own(env, ANTIGRAVITY).error.code, "provider_timeout");

  const flaky = setup();
  await googleLogin(flaky);
  await flaky.fail(flaky.is("GET", "/login-sessions/g1"));
  assert.notEqual(own(flaky, ANTIGRAVITY).login, null);
  assert.deepEqual(flaky.live(), [2000]);
  flaky.fire(2000);
  assert.equal(flaky.pending(flaky.is("GET", "/login-sessions/g1")).length, 1);
});

test("Google: cancelling mid-way drops a poll answer that was already in flight", async () => {
  const env = setup();
  await googleLogin(env);
  const poll = env.pending(env.is("GET", "/login-sessions/g1"))[0];
  assert.ok(poll);
  dispatch(env, ANTIGRAVITY, "login-cancel-requested");
  // The runner drops the answer of the replaced poll; a stale action reaching the reducer is ignored too.
  env.store.dispatch({ type: "provider/response", meta: { provider: "antigravity", op: "poll", gen: 0 }, response: { body: { ...CODE_SESSION } }, now: T0 });
  assert.equal(own(env, ANTIGRAVITY).login.id, "g1");
  await env.answer(env.is("DELETE", "/login-sessions/g1"), 200, { status: "cancelled" });
  const s = own(env, ANTIGRAVITY);
  assert.equal(s.login, null);
  assert.equal(s.message.text, "Đã hủy đăng nhập Antigravity.");
  assert.equal(s.inputOpen, false);
});

test("Google: cancelling a login the server forgot ends it; another cancel error resumes polling", async () => {
  const env = setup();
  await googleLogin(env);
  dispatch(env, ANTIGRAVITY, "login-cancel-requested");
  await env.answer(env.is("DELETE", "/login-sessions/g1"), 404, {});
  assert.equal(own(env, ANTIGRAVITY).login, null);

  const other = setup();
  await googleLogin(other);
  dispatch(other, ANTIGRAVITY, "login-cancel-requested");
  await other.answer(other.is("DELETE", "/login-sessions/g1"), 500, {});
  assert.notEqual(own(other, ANTIGRAVITY).login, null);
  assert.deepEqual(other.live(), [1000]);
});

test("Google: an expired or failed session shows the matching error", async () => {
  const expired = setup();
  await googleLogin(expired);
  await expired.answer(expired.is("GET", "/login-sessions/g1"), 200, { status: "expired" });
  assert.equal(own(expired, ANTIGRAVITY).error.code, "provider_timeout");
  const failed = setup();
  await googleLogin(failed);
  await failed.answer(failed.is("GET", "/login-sessions/g1"), 200, { status: "failed", error: { code: "provider_quota" } });
  assert.equal(own(failed, ANTIGRAVITY).error.code, "provider_quota");
});

// ---- restore after reload ----

test("restore: a stored ChatGPT login is polled once; an expired one is finished by that poll", async () => {
  const env = setup();
  const raw = JSON.stringify({ id: "s9", expiresAt: SOON });
  env.store.dispatch({ type: "provider/boot-restored", provider: "chatgpt", raw, now: T0 });
  assert.equal(own(env, CHATGPT).login.id, "s9");
  assert.deepEqual(env.live(), [0]);
  env.fire(0);
  await env.answer(env.is("GET", "/login-sessions/s9"), 200, { status: "cancelled" });
  assert.equal(own(env, CHATGPT).login, null);

  const old = setup();
  old.store.dispatch({ type: "provider/boot-restored", provider: "chatgpt", raw: JSON.stringify({ id: "s9", expiresAt: LATER }), now: T0 });
  old.clock.now = T0 + 120_000;
  old.fire(0);
  assert.equal(own(old, CHATGPT).login, null);
  assert.equal(old.calls.length, 0);
  assert.equal(own(old, CHATGPT).message.text.startsWith("Phiên đăng nhập đã hết hạn"), true);
});

test("restore: a stored Google login polls at once; an expired one is dropped without a request", () => {
  const env = setup();
  env.store.dispatch({ type: "provider/boot-restored", provider: "antigravity", raw: JSON.stringify({ id: "g7", expiresAt: SOON }), now: T0 });
  assert.equal(own(env, ANTIGRAVITY).login.id, "g7");
  assert.equal(env.pending(env.is("GET", "/login-sessions/g7")).length, 1);

  const old = setup();
  old.store.dispatch({ type: "provider/boot-restored", provider: "antigravity", raw: JSON.stringify({ id: "g7", expiresAt: LATER }), now: T0 + 120_000 });
  assert.equal(own(old, ANTIGRAVITY).login, null);
  assert.equal(old.calls.length, 0);
  assert.deepEqual(old.written, [["remove", "asr.antigravity.pendingLogin"]]);
});

test("restore: nothing stored does nothing; broken JSON is removed; wrong shapes are ignored", () => {
  for (const { d } of CASES) {
    const env = setup();
    env.store.dispatch({ type: "provider/boot-restored", provider: d.id, raw: null, now: T0 });
    env.store.dispatch({ type: "provider/boot-restored", provider: d.id, raw: JSON.stringify({ id: 5 }), now: T0 });
    assert.deepEqual(env.written, []);
    assert.equal(own(env, d).login, null);
    env.store.dispatch({ type: "provider/boot-restored", provider: d.id, raw: "{oops", now: T0 });
    assert.deepEqual(env.written, [["remove", d.storageKey]]);
  }
});

test("restore for one provider leaves the other one alone", () => {
  const env = setup();
  env.store.dispatch({ type: "provider/boot-restored", provider: "chatgpt", raw: JSON.stringify({ id: "s9", expiresAt: SOON }), now: T0 });
  assert.equal(own(env, ANTIGRAVITY).login, null);
});

// ---- shared pieces ----

test("the Google button scroll waits for both status reads, then is handled once", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "antigravity" });
  assert.equal(env.state().launcher.scroll, true);
  assert.equal(own(env, CHATGPT).loading && own(env, ANTIGRAVITY).loading, true);
  await env.answer(env.is("GET", "/api/providers"), 200, LIST);
  await env.answer(env.is("GET", "/api/notes-preferences"), 200, PREFS);
  await env.answer(env.is("GET", "/api/providers/chatgpt/models"), 200, MODELS.chatgpt);
  assert.equal(own(env, ANTIGRAVITY).loading, true);
  await env.answer(env.is("GET", "/api/providers"), 200, LIST);
  await env.answer(env.is("GET", "/antigravity/models"), 200, MODELS.antigravity);
  assert.equal(own(env, CHATGPT).loading || own(env, ANTIGRAVITY).loading, false);
  env.store.dispatch({ type: "provider/scroll-handled" });
  assert.equal(env.state().launcher.scroll, false);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  assert.equal(env.state().launcher.scroll, false);
});

test("provider actions are namespaced and other features' actions leave the state alone", () => {
  const env = setup();
  const before = env.state();
  env.store.dispatch({ type: "shell/dialog-opened" });
  assert.equal(env.state(), before);
  env.store.dispatch({ type: "provider/save-requested", provider: "chatgpt" }); // nothing selected: ignored
  assert.equal(env.state().chatgpt, before.chatgpt);
  assert.equal(env.calls.length, 0);
});

test("the launcher selector names the provider, the state and the account", () => {
  const env = setup();
  assert.equal(selectLauncher(CHATGPT, own(env, CHATGPT)), null);
  env.store.dispatch({ type: "provider/header-requested" });
  const launcher = selectLauncher(CHATGPT, { launcher: { status: "connected", account: "a@b" } });
  assert.equal(launcher.status, "connected");
  assert.deepEqual(launcher.title, ["OpenAI", ": ", { text: "Đã kết nối" }]);
  assert.equal(launcher.ariaLabel.params.name, "OpenAI");
});

// ---- round 2: stale replies and network errors ----

test("a failed load that is no longer the latest changes nothing", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  const [stale] = env.calls; // ChatGPT load 1
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.fail((c) => c === stale);
  assert.equal(own(env, CHATGPT).error, null);
  assert.equal(own(env, CHATGPT).loading, true);
});

test("a status refresh after a failed load is dropped when a newer load started", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 403, { detail: { code: "provider_auth_error" } }); // ChatGPT: needs a refresh
  const refresh = env.pending(env.is("GET", "/api/providers")).at(-1);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer((c) => c === refresh, 200, LIST_OUT);
  assert.equal(own(env, CHATGPT).error, null);
  assert.equal(own(env, CHATGPT).loading, true);
});

test("request errors: 401 and 5xx get their own text; a non-object 2xx reply is a connection error", async () => {
  for (const [status, text] of [[401, "Phiên truy cập"], [500, "Máy chủ chưa thể"]]) {
    const env = setup();
    env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
    await env.answer(env.is("GET", "/api/providers"), status, {});
    assert.equal(own(env, CHATGPT).error.code, `HTTP_${status}`);
    assert.ok(own(env, CHATGPT).error.text.text.startsWith(text), String(status));
  }
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  await env.answer(env.is("GET", "/api/providers"), 200, null);
  assert.equal(own(env, CHATGPT).error.code, "CONNECTION_ERROR");
});

test("starting a load clears the last error; a failed verify frees the pending flag", async () => {
  const env = setup();
  await loaded(env);
  dispatch(env, CHATGPT, "verify-requested");
  assert.equal(own(env, CHATGPT).pending.verify, true);
  await env.answer(env.is("POST", "/api/providers/chatgpt/verify"), 500, {});
  assert.equal(own(env, CHATGPT).pending.verify, false);
  assert.ok(own(env, CHATGPT).error);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: "chatgpt" });
  assert.equal(own(env, CHATGPT).error, null);
});

test("Google: 429 reads as quota and a 5xx as the server-error text", async () => {
  for (const [status, expected] of [[429, ANTIGRAVITY.errors.provider_quota], [500, ANTIGRAVITY.copy.serverError]]) {
    const env = setup();
    await loaded(env);
    dispatch(env, ANTIGRAVITY, "verify-requested");
    await env.answer(env.is("POST", "/api/providers/antigravity/verify"), status, {});
    assert.deepEqual(own(env, ANTIGRAVITY).error.text, expected, String(status));
  }
});

test("Google: a login-session answer that is not an object ends the start and frees the button", async () => {
  const env = setup();
  await googleLogin(env, null);
  const s = own(env, ANTIGRAVITY);
  assert.equal(s.error.code, "INVALID_LOGIN_SESSION");
  assert.equal(s.signInDisabled, false);
  assert.equal(s.login, null);
});

// ---- ASR models in Settings ----

const ASR = {
  asr_models: [
    { id: "auto", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
    { id: "whisper-large-v3", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
    { id: "pho-whisper-large", model_name: "pho", ready: false, reason: "not_prepared", prepare_command: "prepare pho" },
    { id: "qwen3-asr-1.7b", model_name: "qwen", ready: false, reason: "gpu_required", prepare_command: null },
    { id: "future-model", model_name: "x", ready: true, reason: null, prepare_command: null },
  ],
};
const asrCalls = (env) => env.calls.filter((c) => c.url === "/api/asr-models");

test("opening Settings reads the real catalog and lists the three models with readiness", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  assert.equal(env.state().asr.status, "loading");
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR);
  const view = selectAsr(env.state().asr);
  assert.deepEqual(view.rows.map((row) => [row.id, row.state, row.isDefault]), [
    ["whisper-large-v3", "ready", true],
    ["pho-whisper-large", "not_prepared", false],
    ["qwen3-asr-1.7b", "gpu_required", false],
  ]);
  assert.deepEqual(view.rows.map((row) => row.command), ["", "prepare pho", ""]); // only an unprepared model has one
  assert.match(view.rows[2].guidance.text, /chỉ chạy trên GPU NVIDIA/);
  assert.deepEqual(view.defaultControl.options.map((item) => item.value), ["auto", "whisper-large-v3", "pho-whisper-large", "qwen3-asr-1.7b"]);
  assert.equal(view.defaultControl.value, "auto");
});

test("a model catalog reply of an older read is dropped, and a failed read shows an error", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  assert.equal(asrCalls(env).length, 2);
  // A reply of the first read that still arrives (the id is what drops it) changes nothing.
  env.store.dispatch({ type: "provider/asr-loaded", meta: { id: 1 }, response: { body: ASR } });
  assert.equal(env.state().asr.status, "loading");
  env.store.dispatch({ type: "provider/asr-failed", meta: { id: 1 } });
  assert.equal(env.state().asr.status, "loading");
  asrCalls(env)[0].done = true; // the runner aborted the first request; only the second can answer
  await env.answer(env.is("GET", "/api/asr-models"), 500, {});
  assert.equal(env.state().asr.status, "error");
  assert.deepEqual(selectAsr(env.state().asr).rows, []);
});

test("the copy button writes the command through the clipboard effect and the reducer owns the feedback", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR);
  env.store.dispatch({ type: "provider/asr-copy-requested", id: "pho-whisper-large" });
  await tick();
  assert.deepEqual(env.clipboard.written, ["prepare pho"]);
  assert.deepEqual(env.state().asr.copy, { id: "pho-whisper-large", how: "copied" });
  const rows = selectAsr(env.state().asr).rows;
  assert.deepEqual(rows.map((row) => row.copy?.text ?? null), [null, "Đã sao chép lệnh.", null]);
  // A model without a command, or an unknown id, copies nothing.
  env.store.dispatch({ type: "provider/asr-copy-requested", id: "qwen3-asr-1.7b" });
  env.store.dispatch({ type: "provider/asr-copy-requested", id: "nope" });
  await tick();
  assert.deepEqual(env.clipboard.written, ["prepare pho"]);
  // Opening Settings again clears the line.
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  assert.equal(env.state().asr.copy, null);
});

test("a refused clipboard selects the command and says to press Ctrl+C", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR);
  env.clipboard.refuse = true;
  env.store.dispatch({ type: "provider/asr-copy-requested", id: "pho-whisper-large" });
  await tick();
  assert.deepEqual(env.clipboard.written, []);
  assert.deepEqual(env.clipboard.selected, ["pho-whisper-large"]);
  assert.deepEqual(env.state().asr.copy, { id: "pho-whisper-large", how: "selected" });
  assert.match(selectAsr(env.state().asr).rows[1].copy.text, /Ctrl\+C/);
});

test("a copy result that arrives after Settings was reopened changes nothing", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR);
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  env.store.dispatch({ type: "provider/asr-copy-succeeded", meta: { id: "pho-whisper-large", request: 1 } });
  env.store.dispatch({ type: "provider/asr-copy-failed", meta: { id: "pho-whisper-large", request: 1 } });
  assert.equal(env.state().asr.copy, null);
  assert.deepEqual(env.clipboard.selected, []);
});

test("a catalog reply with no usable model says so instead of showing a blank section", async () => {
  for (const body of [[], [null, 5, { id: "evil" }], [{ id: "auto", model_name: "x", ready: true }]]) {
    const env = setup();
    env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
    await env.answer(env.is("GET", "/api/asr-models"), 200, { asr_models: body });
    assert.equal(env.state().asr.status, "empty");
    const view = selectAsr(env.state().asr);
    assert.deepEqual(view.rows, []);
    assert.match(view.text.text, /không trả về model nào/);
  }
});

test("without a worker every model reports that it cannot be checked", async () => {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  const down = { asr_models: ["auto", "whisper-large-v3", "pho-whisper-large", "qwen3-asr-1.7b"].map((id) => ({ id, model_name: null, ready: false, reason: "worker_unavailable", prepare_command: null })) };
  await env.answer(env.is("GET", "/api/asr-models"), 200, down);
  const view = selectAsr(env.state().asr);
  assert.deepEqual(view.rows.map((row) => row.state), ["worker_unavailable", "worker_unavailable", "worker_unavailable"]);
  assert.equal(view.rows.some((row) => row.isDefault), false);
});

// ---- installing an ASR model from Settings ----

const PHO = "pho-whisper-large";
const GB = 10 ** 9;
const model = (id, extra = {}) => ({ id, model_name: id, ready: false, reason: "not_prepared", prepare_command: `prepare ${id}`, required_bytes: 9 * GB, free_bytes: 100 * GB, install: { state: "idle" }, ...extra });
const catalog = ({ pho = {}, qwen = {}, installer = { available: true, models_writable: true } } = {}) => ({
  asr_models: [
    { id: "auto", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
    { ...model("whisper-large-v3"), ready: true, reason: null, prepare_command: null, install: { state: "idle" } },
    model(PHO, pho),
    model("qwen3-asr-1.7b", { reason: "gpu_required", prepare_command: null, ...qwen }),
  ],
  installer,
});
const phoRow = (env) => selectAsr(env.state().asr).rows.find((row) => row.id === PHO);
const catalogCalls = (env) => env.calls.filter((c) => c.method === "GET" && c.url === "/api/asr-models");

async function openedWith(body) {
  const env = setup();
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, body);
  return env;
}

test("with an installer an unprepared model offers Cài đặt model with the space it needs, and no command", async () => {
  const env = await openedWith(catalog());
  const row = phoRow(env);
  assert.equal(row.command, "");
  assert.equal(row.install.kind, "offer");
  assert.deepEqual(row.install.spaceLine, { text: "Cần {required} trống, còn {free}.", params: { required: { fmt: "bytes", value: 9 * GB }, free: { fmt: "bytes", value: 100 * GB } } });
  assert.equal(row.install.blocked, false);
  // A model that cannot run here gets no button, and a prepared one has no install block.
  const rows = selectAsr(env.state().asr).rows;
  assert.deepEqual(rows.map((r) => r.install?.kind ?? null), [null, "offer", null]);
});

test("without a usable installer the copy command stays, as it always was", async () => {
  for (const installer of [{ available: false, models_writable: false }, { available: true, models_writable: false }, undefined]) {
    const env = await openedWith({ ...catalog(), installer });
    const row = phoRow(env);
    assert.equal(row.install, null);
    assert.equal(row.command, `prepare ${PHO}`);
  }
});

test("less free space than the model needs blocks the button and says how much is missing", async () => {
  const env = await openedWith(catalog({ pho: { free_bytes: 2 * GB } }));
  const { install } = phoRow(env);
  assert.equal(install.blocked, true);
  assert.deepEqual(install.blockedMessage.params, { required: { fmt: "bytes", value: 9 * GB }, free: { fmt: "bytes", value: 2 * GB } });
  const unknown = await openedWith(catalog({ pho: { free_bytes: null } }));
  assert.equal(phoRow(unknown).install.blocked, false);
  assert.equal(phoRow(unknown).install.spaceLine.text, "Cần {required} trống.");
});

test("Cài đặt model posts once, shows queued at once, then follows the job until the model is ready", async () => {
  const env = await openedWith(catalog());
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  assert.equal(phoRow(env).state, "installing");
  assert.equal(phoRow(env).install.phase.text, "Đang chờ cài đặt…");
  assert.equal(env.pending(env.is("POST", `/api/asr-models/${PHO}/install`)).length, 1);
  await env.answer(env.is("POST", `/api/asr-models/${PHO}/install`), 202, { install: { state: "queued" } });
  assert.deepEqual(env.live(), [1000]); // reading again while the job is active

  env.fire(1000);
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { install: { state: "running", phase: "downloading", bytes_done: 3 * GB, bytes_total: 6 * GB } } }));
  let row = phoRow(env);
  assert.equal(row.install.phase.text, "Đang tải model…");
  assert.equal(row.progress.ratio, 0.5);
  assert.deepEqual(row.progress.detail.params, { done: { fmt: "bytes", value: 3 * GB }, total: { fmt: "bytes", value: 6 * GB } });
  assert.deepEqual(env.live(), [1000]);

  env.fire(1000);
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { install: { state: "running", phase: "converting", bytes_done: 6 * GB, bytes_total: 6 * GB } } }));
  row = phoRow(env);
  assert.match(row.install.phase.text, /chuyển đổi/);
  assert.equal(row.progress.ratio, null); // an unknown share is not drawn as a number

  env.fire(1000);
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { ready: true, reason: null, prepare_command: null, install: { state: "idle" } } }));
  row = phoRow(env);
  assert.deepEqual([row.state, row.status.text, row.install, row.command], ["ready", "Sẵn sàng", null, ""]);
  assert.deepEqual(env.live(), []); // nothing left to wait for: polling stops by itself
});

test("a double click makes one request on screen and Hủy asks to stop the job", async () => {
  const env = await openedWith(catalog());
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  assert.equal(env.pending(env.is("POST", "/install")).length, 1); // the second click asked for nothing
  await env.answer(env.is("POST", "/install"), 202, { install: { state: "running", phase: "downloading", bytes_done: 0, bytes_total: 6 * GB } });
  env.store.dispatch({ type: "provider/asr-cancel-requested", id: PHO });
  assert.equal(phoRow(env).install.cancelDisabled, true);
  assert.equal(phoRow(env).install.phase.text, "Đang hủy…");
  await env.answer(env.is("DELETE", `/api/asr-models/${PHO}/install`), 202, { install: { state: "cancelling" } });
  env.fire(1000);
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog());
  assert.equal(phoRow(env).install.kind, "offer"); // the partial download is gone and it can be started again
  assert.deepEqual(env.live(), []);
});

test("Hủy waits until the server has the job, so a job cannot start after it looked cancelled", async () => {
  const env = await openedWith(catalog());
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  assert.equal(phoRow(env).install.cancelDisabled, true);
  env.store.dispatch({ type: "provider/asr-cancel-requested", id: PHO });
  assert.equal(env.pending(env.is("DELETE", "/install")).length, 0);
  assert.equal(env.pending(env.is("POST", "/install")).length, 1); // the create is still on its way
  await env.answer(env.is("POST", "/install"), 202, { install: { state: "queued" } });
  assert.equal(phoRow(env).install.cancelDisabled, false);
  assert.deepEqual(env.live(), [1000]); // the job is known: its progress is being read
  env.store.dispatch({ type: "provider/asr-cancel-requested", id: PHO });
  env.store.dispatch({ type: "provider/asr-cancel-requested", id: PHO });
  assert.equal(env.pending(env.is("DELETE", "/install")).length, 1);
});

test("a late reply to a create cannot undo the cancel that came after it", async () => {
  const env = await openedWith(catalog());
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  const create = env.pending(env.is("POST", "/install"))[0];
  // The dialog reopens, reads the job the server already has, and the user cancels it.
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { install: { state: "running", phase: "downloading", bytes_done: 1, bytes_total: 2 } } }));
  env.store.dispatch({ type: "provider/asr-cancel-requested", id: PHO });
  assert.equal(phoRow(env).install.phase.text, "Đang hủy…");
  env.store.dispatch({ type: "provider/asr-install-sent", response: { status: 202, body: { install: { state: "queued" } } }, meta: { id: PHO, mutation: 1 } });
  assert.ok(create);
  assert.equal(phoRow(env).install.phase.text, "Đang hủy…");
  env.store.dispatch({ type: "provider/asr-install-refused", error: { body: { detail: { code: "gpu_required" } } }, meta: { id: PHO, mutation: 1 } });
  assert.equal(phoRow(env).install.refusal, null);
});

test("a reply of a catalog read made before the click cannot undo the click", async () => {
  const env = await openedWith(catalog({ pho: { install: { state: "idle" } } }));
  env.store.dispatch({ type: "provider/asr-poll" });
  const staleId = env.state().asr.request; // the read made before the click
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  // The runner aborts that read; if its answer still arrives, the id drops it.
  env.store.dispatch({ type: "provider/asr-loaded", meta: { id: staleId }, response: { body: catalog() } });
  assert.equal(phoRow(env).install.kind, "progress");
});

test("a failed install says what went wrong in plain words and offers Thử lại", async () => {
  const messages = {
    network: /Mất kết nối/,
    disk_full: /Ổ đĩa đầy/,
    integrity: /không hợp lệ/,
    target_not_empty: /đã có file khác/,
    models_not_writable: /không ghi được/,
    interrupted: /gián đoạn/,
    insufficient_disk: /Không đủ dung lượng: cần \{required\}, còn \{free\}/,
    unheard_of: /gián đoạn/,
  };
  for (const [error, pattern] of Object.entries(messages)) {
    const failed = { state: "failed", error, required_bytes: 9 * GB, free_bytes: 1 * GB };
    const env = await openedWith(catalog({ pho: { install: failed } }));
    const { install } = phoRow(env);
    assert.equal(install.kind, "failed");
    assert.match(install.message.text, pattern);
    assert.deepEqual(env.live(), []); // a failure is final: no polling
  }
  // Thử lại is the same request as the first one.
  const env = await openedWith(catalog({ pho: { install: { state: "failed", error: "network" } } }));
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  assert.equal(env.pending(env.is("POST", "/install")).length, 1);
  assert.equal(phoRow(env).install.kind, "progress");
});

test("a refused install shows why, then reads the catalog again for the truth", async () => {
  const env = await openedWith(catalog());
  env.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  await env.answer(env.is("POST", "/install"), 409, { detail: { code: "model_ready" } });
  assert.match(catalogCalls(env).at(-1).url, /asr-models/);
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { ready: true, reason: null, prepare_command: null } }));
  assert.equal(phoRow(env).state, "ready");

  const down = await openedWith(catalog());
  down.store.dispatch({ type: "provider/asr-install-requested", id: PHO });
  await down.fail(down.is("POST", "/install"));
  await down.answer(down.is("GET", "/api/asr-models"), 200, catalog());
  assert.match(phoRow(down).install.refusal.text, /Không gửi được yêu cầu/);
  const withNotice = selectAsr(down.state().asr).rows.find((row) => row.id === PHO);
  assert.equal(withNotice.install.kind, "offer");
});

test("polling reads only while Settings is open, and closing it stops the timer", async () => {
  const env = await openedWith(catalog({ pho: { install: { state: "running", phase: "downloading", bytes_done: 1, bytes_total: 2 } } }));
  assert.deepEqual(env.live(), [1000]);
  env.store.dispatch({ type: "provider/dialog-closed" });
  assert.deepEqual(env.live(), []);
  // A poll that was already due does nothing once the dialog is closed.
  const before = catalogCalls(env).length;
  env.store.dispatch({ type: "provider/asr-poll" });
  assert.equal(catalogCalls(env).length, before);
  // Opening again reads again, and resumes polling while the job is still going.
  env.store.dispatch({ type: "provider/dialog-opened", launcher: null });
  await env.answer(env.is("GET", "/api/asr-models"), 200, catalog({ pho: { install: { state: "running", phase: "downloading", bytes_done: 1, bytes_total: 2 } } }));
  assert.deepEqual(env.live(), [1000]);
});

test("an idle catalog does not poll, and a failed poll keeps the rows on screen", async () => {
  const env = await openedWith(catalog());
  assert.deepEqual(env.live(), []);
  const busy = await openedWith(catalog({ pho: { install: { state: "running", phase: "downloading", bytes_done: 1, bytes_total: 2 } } }));
  busy.fire(1000);
  await busy.fail(busy.is("GET", "/api/asr-models"));
  assert.equal(busy.state().asr.status, "ready");
  assert.equal(phoRow(busy).install.kind, "progress");
  assert.deepEqual(busy.live(), [1000]); // and it tries again
});

test("a job found running when Settings opens is shown at once, and stays shown if the installer stops", async () => {
  const running = { state: "running", phase: "verifying", bytes_done: 6 * GB, bytes_total: 6 * GB };
  const env = await openedWith(catalog({ pho: { install: running } }));
  assert.match(phoRow(env).install.phase.text, /kiểm tra file/);
  assert.equal(phoRow(env).state, "installing");
  const stopped = await openedWith(catalog({ pho: { install: running }, installer: { available: false, models_writable: false } }));
  assert.equal(phoRow(stopped).install, null); // no installer: back to the command, not a bar that never moves
  assert.equal(phoRow(stopped).command, `prepare ${PHO}`);
  assert.deepEqual(stopped.live(), []); // and nothing keeps reading for a service that is gone
});

// ---- the default model in Settings ----

test("a model that cannot run here cannot be chosen as the default", () => {
  const state = { ...initialState.asr, status: "ready", models: ASR.asr_models.filter((m) => m.id !== "future-model"), installer: { available: true, models_writable: true }, hardwareModel: "whisper-large-v3" };
  const control = selectAsr(state).defaultControl;
  assert.deepEqual(control.options.filter((o) => o.disabled).map((o) => o.value), ["pho-whisper-large", "qwen3-asr-1.7b"]);
  assert.equal(control.options.find((o) => o.value === "whisper-large-v3").disabled, false);
  assert.equal(control.disabled, false);
});

test("a chosen default that stopped working says so instead of looking fine", () => {
  const models = [{ id: "auto", model_name: "pho", ready: false, reason: "not_prepared", chosen: true }, ...ASR.asr_models.slice(1, 3)];
  const state = { ...initialState.asr, status: "ready", models, defaultModel: "pho-whisper-large", installer: { available: true, models_writable: true } };
  const control = selectAsr(state).defaultControl;
  assert.equal(control.broken, true);
  assert.equal(control.value, "pho-whisper-large");
  assert.deepEqual(control.text.params, { model: "PhoWhisper-large" });
});

test("choosing sends the id, and the hardware choice sends null", () => {
  for (const [value, sent] of [["whisper-large-v3", "whisper-large-v3"], ["auto", null]]) {
    const { state: next, effects } = reduce({ ...initialState, asr: { ...initialState.asr, status: "ready", models: ASR.asr_models } }, { type: "provider/asr-default-chosen", value });
    const put = effects.find((e) => e.url === "/api/asr-models/default");
    assert.equal(put.init.method, "PUT");
    assert.deepEqual(JSON.parse(put.init.body), { asr_model: sent });
    assert.equal(next.asr.defaultPending, sent);
  }
});

test("a refused choice goes back to what the server has and says why; a read in flight cannot undo a click", () => {
  const base = { ...initialState, asr: { ...initialState.asr, status: "ready", models: ASR.asr_models, defaultModel: "whisper-large-v3", request: 5 } };
  const { state: chosen } = reduce(base, { type: "provider/asr-default-chosen", value: "auto" });
  assert.equal(chosen.asr.request, 6);
  const { state: stale } = reduce(chosen, { type: "provider/asr-loaded", meta: { id: 5 }, response: { body: { ...ASR, default_model: "whisper-large-v3" } } });
  assert.equal(stale.asr.defaultPending, null);
  // A read after the click may still answer the old value; the refusal must fall back to the server's own.
  const { state: polled } = reduce(chosen, { type: "provider/asr-loaded", meta: { id: 6 }, response: { body: { ...ASR, default_model: "whisper-large-v3" } } });
  assert.equal(selectAsr(polled.asr).defaultControl.value, "auto");
  const { state: backed } = reduce(polled, { type: "provider/asr-default-refused", meta: { mutation: polled.asr.defaultMutation }, error: { body: { detail: { code: "not_prepared" } } } });
  assert.equal(backed.asr.defaultModel, "whisper-large-v3");
  const { state: refused } = reduce(chosen, { type: "provider/asr-default-refused", meta: { mutation: chosen.asr.defaultMutation }, error: { body: { detail: { code: "not_prepared" } } } });
  assert.equal(refused.asr.defaultModel, "whisper-large-v3");
  assert.equal(refused.asr.defaultRefusal, "not_prepared");
  const { state: newer } = reduce(chosen, { type: "provider/asr-default-chosen", value: "whisper-large-v3" });
  const { state: late } = reduce(newer, { type: "provider/asr-default-saved", meta: { mutation: chosen.asr.defaultMutation } });
  assert.equal(late.asr.defaultPending, "whisper-large-v3");
});
