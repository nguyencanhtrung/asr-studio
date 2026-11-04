import assert from "node:assert/strict";
import test from "node:test";

import { createRunner, createStore } from "../../src/asr_studio/web/store.js";
import {
  AUTO_TRANSCRIBE_HANDOFF_PREFIX,
  canSubmit,
  initialState,
  isWorkspaceAuthenticationFailure,
  reduce,
  responseMessage,
  selectView,
} from "../../src/asr_studio/web/shell-core.js";

test("workspace authentication failure is exact and does not classify provider 401 details", () => {
  assert.equal(isWorkspaceAuthenticationFailure(401, { detail: "Authentication required" }), true);
  assert.equal(isWorkspaceAuthenticationFailure(401, { detail: { code: "reconnect_required" } }), false);
  assert.equal(isWorkspaceAuthenticationFailure(401, { detail: "authentication required" }), false);
  assert.equal(isWorkspaceAuthenticationFailure(403, { detail: "Authentication required" }), false);
});

const tick = () => new Promise((resolve) => setImmediate(resolve));
const reply = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const POLICY = { supported_extensions: ["wav", ".mp3", "mp4"], max_bytes: 1000, max_duration_seconds: 7200, chunk_bytes: 1000 };
const ASR_MODELS = {
  asr_models: [
    { id: "auto", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
    { id: "whisper-large-v3", model_name: "whisper-large-v3", ready: true, reason: null, prepare_command: null },
    { id: "pho-whisper-large", model_name: "pho-whisper-large", ready: false, reason: "not_prepared", prepare_command: "docker compose run prepare" },
    { id: "qwen3-asr-1.7b", model_name: "qwen3-asr-1.7b", ready: true, reason: null, prepare_command: null },
  ],
};
const FILE = { name: "Weekly sync.wav", size: 500 };
const CHUNKED_POLICY = { ...POLICY, chunk_bytes: 4 };
const CHUNKED_FILE = {
  name: "Chunked.wav",
  size: 10,
  slice(start, end) {
    return { start, end, size: end - start };
  },
};

test("server error details are resolved by code instead of leaking raw English", () => {
  assert.deepEqual(
    responseMessage(422, {
      detail: {
        code: "invalid_media",
        message: "The uploaded file is corrupt or cannot be read.",
      },
    }),
    { text: "File không đạt yêu cầu. Hãy kiểm tra định dạng, thời lượng và nội dung file." },
  );
  assert.deepEqual(
    responseMessage(413, { detail: { code: "duration_limit", max_duration_seconds: 14_400 } }),
    { text: "File vượt giới hạn thời lượng {duration}. Hãy chọn bản ghi ngắn hơn.", params: { duration: { text: "{n} giờ", params: { n: 4 } } } },
  );
});

// Real store and runner over fakes: requests wait in `calls` until a test answers them.
function setup() {
  const calls = [];
  const fetch = (url, init) =>
    new Promise((resolve, reject) => {
      calls.push({ url, init, resolve, reject, method: init.method });
    });
  const xhrs = [];
  class FakeXhr {
    constructor() {
      this.upload = {};
      xhrs.push(this);
    }
    open(method, url) {
      Object.assign(this, { method, url });
    }
    setRequestHeader() {}
    send(body) {
      this.body = body;
    }
    abort() {
      this.aborted = true;
    }
  }
  const written = [];
  const navigated = [];
  // extract: calls wait here until a test finishes them; `signal` shows whether the runner aborted one.
  const extracts = [];
  const extract = (file, options) =>
    new Promise((resolve, reject) => {
      extracts.push({ file, options, resolve, reject });
    });
  const scheduled = [];
  let nextTimerId = 0;
  const setTimeout = (callback, delay) => {
    const timer = { id: ++nextTimerId, callback, delay, cancelled: false, fired: false };
    scheduled.push(timer);
    return timer.id;
  };
  const clearTimeout = (id) => {
    const timer = scheduled.find((candidate) => candidate.id === id);
    if (timer) timer.cancelled = true;
  };
  const runNextTimer = async () => {
    const timer = scheduled.find((candidate) => !candidate.cancelled && !candidate.fired);
    assert.ok(timer, "no pending timer");
    timer.fired = true;
    timer.callback();
    await tick();
    return timer;
  };
  const run = createRunner({
    extract,
    fetch,
    setTimeout,
    clearTimeout,
    XMLHttpRequest: FakeXhr,
    storage: { local: () => ({ setItem: (k, v) => written.push(["local", k, v]) }), session: () => ({ setItem: (k, v) => written.push(["session", k, v]) }) },
    navigate: (url) => navigated.push(url),
  });
  const store = createStore({ reduce, initialState, run });
  const answer = async (match, status, body) => {
    const call = calls.find((c) => !c.done && match(c));
    assert.ok(call, "no pending call");
    call.done = true;
    call.resolve(reply(status, body));
    await tick();
  };
  const is = (method, path) => (c) => c.method === method && c.url.includes(path);
  const form = { title: "Weekly sync", date: "", language: "en" };
  return {
    store,
    calls,
    xhrs,
    extracts,
    scheduled,
    runNextTimer,
    written,
    navigated,
    answer,
    is,
    form,
    state: () => store.getState(),
  };
}

async function opened(env, stored = {}, policy = POLICY, models = ASR_MODELS) {
  env.store.dispatch({ type: "shell/dialog-opened", stored });
  await env.answer(env.is("GET", "/api/uploads/policy"), 200, policy);
  await env.answer(env.is("GET", "/api/asr-models"), 200, models);
}

async function selected(env, file = FILE, policy = POLICY) {
  await opened(env, {}, policy);
  env.store.dispatch({ type: "shell/file-selected", file });
  env.store.dispatch({ type: "shell/form-edited", form: env.form });
}

async function uploading(env, file = FILE, policy = POLICY) {
  await selected(env, file, policy);
  env.store.dispatch({ type: "shell/submit-requested" });
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
}

async function ready(env) {
  await uploading(env);
  env.xhrs[0].upload.onprogress({ loaded: 250, total: 500, lengthComputable: true });
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  await env.answer(env.is("POST", "/complete"), 200, { status: "ready" });
}

test("opening loads the policy; a reply for a closed or reopened dialog is stale", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  assert.equal(env.state().phase, "idle");
  assert.equal(env.state().policy, null);
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  await env.answer(env.is("GET", "/policy"), 200, POLICY); // cancelled by close: ignored
  assert.equal(env.state().policy, null);
  // A reply carrying an old request id is ignored even if it reaches the reducer.
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  const { id } = { id: env.state().policyRequest };
  env.store.dispatch({ type: "shell/policy-loaded", meta: { id: id - 1 }, response: { body: POLICY } });
  assert.equal(env.state().policy, null);
  await env.answer(env.is("GET", "/policy"), 200, POLICY);
  assert.deepEqual(env.state().policy, { extensions: [".wav", ".mp3", ".mp4"], maxBytes: 1000, maxDurationSeconds: 7200, chunkBytes: 1000 });
});

test("opening loads the model catalog, ignores stale replies, and hides it after a failed load", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  const first = env.state().asrModelsRequest;
  assert.equal(env.state().asrModels, null);
  // A reply for the closed dialog, then one from before the reopen: both are stale if they reach the reducer.
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR_MODELS); // cancelled by close: ignored
  env.store.dispatch({ type: "shell/asr-models-loaded", meta: { id: env.state().asrModelsRequest }, response: { body: ASR_MODELS } });
  assert.equal(env.state().asrModels, null);
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  env.store.dispatch({ type: "shell/asr-models-loaded", meta: { id: first }, response: { body: ASR_MODELS } });
  assert.equal(env.state().asrModels, null);
  await env.answer(env.is("GET", "/api/asr-models"), 200, ASR_MODELS);
  assert.equal(env.state().asrModels.length, 4);
  assert.equal(selectView(env.state()).asrModel.options.length, 4);

  const failed = setup();
  failed.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  await failed.answer(failed.is("GET", "/api/asr-models"), 500, {});
  assert.equal(failed.state().asrModels, null);
  assert.equal(selectView(failed.state()).asrModel, null);
});

test("a model this page does not know is dropped from the catalog", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  await env.answer(env.is("GET", "/api/asr-models"), 200, { asr_models: [null, { id: "future-model", ready: true }, ...ASR_MODELS.asr_models] });
  assert.deepEqual(
    selectView(env.state()).asrModel.options.map((option) => option.id),
    ASR_MODELS.asr_models.map((choice) => choice.id),
  );
});

test("remembered choices fill the form; unknown language is ignored", () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: { language: "vi", autoTranscribe: "0" } });
  assert.deepEqual(env.state().form, { title: "", date: "", language: "vi", autoTranscribe: false, asrModel: "auto" });
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  env.store.dispatch({ type: "shell/dialog-opened", stored: { language: "fr", autoTranscribe: null } });
  assert.deepEqual(env.state().form, { title: "", date: "", language: "", autoTranscribe: true, asrModel: "auto" });
});

test("the create model selector shows readiness, accepts ready ids only, and resets on reopen", async () => {
  const env = setup();
  await opened(env);
  let view = selectView(env.state());
  assert.equal(view.asrModel.value, "auto");
  assert.deepEqual(view.asrModel.options.map((option) => [option.id, option.ready, option.disabled]), [
    ["auto", true, false],
    ["whisper-large-v3", true, false],
    ["pho-whisper-large", false, true],
    ["qwen3-asr-1.7b", true, false],
  ]);
  assert.match(view.asrModel.help.at(-1).text, /Model chưa sẵn sàng/);

  env.store.dispatch({ type: "shell/asr-model-chosen", model: "pho-whisper-large" });
  assert.equal(env.state().form.asrModel, "auto");
  env.store.dispatch({ type: "shell/asr-model-chosen", model: "qwen3-asr-1.7b" });
  assert.equal(env.state().form.asrModel, "qwen3-asr-1.7b");
  env.store.dispatch({ type: "shell/asr-model-chosen", model: "cloud-model" });
  assert.equal(env.state().form.asrModel, "qwen3-asr-1.7b");

  env.store.dispatch({ type: "shell/auto-transcribe-toggled", value: false });
  assert.equal(selectView(env.state()).asrModel, null);
  env.store.dispatch({ type: "shell/auto-transcribe-toggled", value: true });
  view = selectView(env.state());
  assert.equal(view.asrModel.value, "qwen3-asr-1.7b");

  env.store.dispatch({ type: "shell/dialog-close-requested" });
  await opened(env);
  assert.equal(env.state().form.asrModel, "auto");
});

test("auto remains selectable when the worker is down", async () => {
  const env = setup();
  const workerDown = {
    asr_models: ASR_MODELS.asr_models.map((choice) => ({
      ...choice,
      model_name: null,
      ready: false,
      reason: "worker_unavailable",
      prepare_command: null,
    })),
  };
  await opened(env);
  env.store.dispatch({ type: "shell/asr-model-chosen", model: "qwen3-asr-1.7b" });
  env.store.dispatch({
    type: "shell/asr-models-loaded",
    meta: { id: env.state().asrModelsRequest },
    response: { body: workerDown },
  });
  env.store.dispatch({ type: "shell/asr-model-chosen", model: "auto" });
  assert.equal(env.state().form.asrModel, "auto");
  assert.equal(selectView(env.state()).asrModel.options[0].disabled, false);
});

test("a policy that fails to load or parse leaves an error and no upload", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  await env.answer(env.is("GET", "/policy"), 500, {});
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().policyFailed, true);
  assert.equal(canSubmit(env.state()), false);
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  await env.answer(env.is("GET", "/policy"), 200, { max_bytes: 5 });
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().policy, null);
});

test("file checks: unsupported extension, too large, missing policy, title prefill", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  env.store.dispatch({ type: "shell/file-selected", file: FILE }); // policy not loaded yet
  assert.equal(env.state().phase, "error");
  await env.answer(env.is("GET", "/policy"), 200, POLICY);
  env.store.dispatch({ type: "shell/file-selected", file: { name: "notes.txt", size: 1 } });
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().fileError.params.extension, ".txt");
  env.store.dispatch({ type: "shell/file-selected", file: { name: "big.wav", size: 5000 } });
  assert.equal(env.state().phase, "error");
  env.store.dispatch({ type: "shell/file-selected", file: FILE });
  assert.equal(env.state().phase, "selected");
  assert.equal(env.state().fileError, null);
  assert.equal(env.state().form.title, "Weekly sync");
  env.store.dispatch({ type: "shell/form-edited", form: { title: "My title", date: "", language: "en" } });
  env.store.dispatch({ type: "shell/file-selected", file: { name: "other.wav", size: 2 } });
  assert.equal(env.state().form.title, "My title"); // a typed title is never replaced
});

test("form validation gates the one button", async () => {
  const env = setup();
  await selected(env);
  assert.equal(selectView(env.state()).createDisabled, false);
  const edit = (patch) => env.store.dispatch({ type: "shell/form-edited", form: { ...env.form, ...patch } });
  edit({ title: "   " });
  assert.equal(canSubmit(env.state()), false);
  edit({ title: "x".repeat(201) });
  assert.equal(canSubmit(env.state()), false);
  edit({ language: "" });
  assert.equal(canSubmit(env.state()), false);
  edit({ date: "2026-1-2" });
  assert.equal(canSubmit(env.state()), false);
  edit({ date: "0999-10-28" });
  assert.equal(canSubmit(env.state()), false);
  edit({ date: "2026-10-28" });
  assert.equal(canSubmit(env.state()), true);
});

test("one button: cleans, creates the upload, uploads with progress, checks, then creates the meeting", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().starting, true);
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.xhrs[0].url, "/api/uploads/u1/content?offset=0");
  assert.equal(env.xhrs[0].body, FILE);
  env.xhrs[0].upload.onprogress({ loaded: 250, total: 500, lengthComputable: true });
  assert.equal(env.state().progress.percent, 50);
  env.xhrs[0].upload.onprogress({ loaded: 700, lengthComputable: false });
  assert.equal(env.state().progress.text.text, "{size} đã gửi");
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  assert.equal(env.state().phase, "checking");
  assert.equal(env.state().progress.indeterminate, true);
  assert.equal(env.state().progress.percent, null);
  await env.answer(env.is("POST", "/u1/complete"), 200, { status: "ready" });
  assert.equal(env.state().phase, "creating");
  const create = env.calls.find((c) => c.url === "/api/meetings");
  assert.deepEqual(JSON.parse(create.init.body), { upload_id: "u1", title: "Weekly sync", meeting_date: null, language_hint: "en" });
  await env.answer(env.is("POST", "/api/meetings"), 201, { meeting: { id: "m 1" } });
  assert.equal(env.state().phase, "saved");
  assert.deepEqual(env.written, [
    ["local", "asr-studio:last-language-hint", "en"],
    ["session", `${AUTO_TRANSCRIBE_HANDOFF_PREFIX}m 1`, "auto"],
  ]);
  assert.deepEqual(env.navigated, ["/meetings/m%201"]);
});

test("a ready create-model choice is handed off for one run and is not stored locally", async () => {
  const env = setup();
  await ready(env);
  env.store.dispatch({ type: "shell/asr-model-chosen", model: "qwen3-asr-1.7b" });
  env.store.dispatch({ type: "shell/submit-requested" });
  await env.answer(env.is("POST", "/api/meetings"), 201, { meeting: { id: "m-model" } });
  assert.deepEqual(env.written, [
    ["local", "asr-studio:last-language-hint", "en"],
    ["session", `${AUTO_TRANSCRIBE_HANDOFF_PREFIX}m-model`, "qwen3-asr-1.7b"],
  ]);
  assert.equal(env.written.some(([, key]) => key.includes("asrModel")), false);
});

test("chunked upload sends sequential slices, resumes a failed chunk, and keeps cumulative progress", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  assert.equal(env.xhrs[0].url, "/api/uploads/u1/content?offset=0");
  assert.deepEqual(env.xhrs[0].body, { start: 0, end: 4, size: 4 });
  env.xhrs[0].upload.onprogress({ loaded: 2, total: 4, lengthComputable: true });
  assert.equal(env.state().progress.percent, 20);

  const finishChunk = (xhr, received) => {
    Object.assign(xhr, { status: 200, responseText: JSON.stringify({ status: received === 10 ? "uploaded" : "receiving", received_bytes: received }) });
    xhr.onload();
  };
  finishChunk(env.xhrs[0], 4);
  assert.equal(env.state().progress.percent, 40);
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=4");
  assert.deepEqual(env.xhrs[1].body, { start: 4, end: 8, size: 4 });

  env.xhrs[1].onerror();
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, { status: "receiving", received_bytes: 4 });
  const retryTimer = await env.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  assert.equal(env.xhrs[2].url, "/api/uploads/u1/content?offset=4");
  assert.deepEqual(env.xhrs[2].body, { start: 4, end: 8, size: 4 });
  finishChunk(env.xhrs[2], 8);
  assert.equal(env.state().progress.percent, 80);
  assert.equal(env.xhrs[3].url, "/api/uploads/u1/content?offset=8");
  assert.deepEqual(env.xhrs[3].body, { start: 8, end: 10, size: 2 });
  finishChunk(env.xhrs[3], 10);
  assert.equal(env.state().phase, "checking");
  assert.equal(env.state().progress.indeterminate, true);
  assert.equal(env.state().progress.percent, null);
  await env.answer(env.is("POST", "/complete"), 200, { status: "ready" });
  assert.equal(env.state().phase, "creating");
});

test("server-side checking stays indeterminate and reports elapsed time", async () => {
  const env = setup();
  await uploading(env);
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  const runId = env.state().runId;
  env.store.dispatch({ type: "shell/upload-checking-tick", meta: { runId }, now: 1_000 });
  assert.equal(env.state().progress.indeterminate, true);
  assert.equal(env.state().progress.percent, null);
  env.store.dispatch({ type: "shell/upload-checking-tick", meta: { runId }, now: 5_250 });
  assert.equal(env.state().progress.text.params.seconds, 4);
  assert.equal(env.state().progress.text.params.size.fmt, "bytes");
});

test("auto-transcribe off writes no handoff; toggling stores the choice", async () => {
  const env = setup();
  await ready(env);
  env.store.dispatch({ type: "shell/auto-transcribe-toggled", value: false });
  assert.deepEqual(env.written, [["local", "asr-studio:auto-transcribe", "0"]]);
});

test("a repeated create or upload while busy does nothing", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/submit-requested" });
  const starting = env.state();
  env.store.dispatch({ type: "shell/submit-requested" }); // still starting
  assert.equal(env.state(), starting);
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  const uploadingState = env.state();
  env.store.dispatch({ type: "shell/submit-requested" }); // uploading
  env.store.dispatch({ type: "shell/file-selected", file: { name: "b.wav", size: 1 } });
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  assert.equal(env.state(), uploadingState);
  assert.equal(env.calls.filter((c) => c.url === "/api/uploads").length, 1);
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  await env.answer(env.is("POST", "/complete"), 200, { status: "ready" });
  await env.answer(env.is("POST", "/api/meetings"), 201, { id: "m1" });
  const saved = env.state();
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state(), saved);
  assert.equal(env.calls.filter((c) => c.url === "/api/meetings").length, 1);
});

test("a late create reply for a replaced request is ignored", async () => {
  const env = setup();
  await ready(env);
  const id = env.state().createRequest;
  env.store.dispatch({ type: "shell/meeting-created", meta: { id: id - 1, payload: {} }, response: { body: { id: "old" } } });
  assert.equal(env.state().phase, "creating");
  assert.deepEqual(env.navigated, []);
});

test("an upload reply of an older run is ignored", async () => {
  const env = setup();
  await uploading(env);
  const run = env.state().runId;
  for (const type of ["shell/upload-sent", "shell/upload-completed"]) {
    env.store.dispatch({ type, meta: { runId: run - 1 }, response: { body: { status: "ready" } } });
  }
  env.store.dispatch({ type: "shell/upload-failed", meta: { runId: run - 1 }, error: { message: "x", kind: "network" } });
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().failure, null);
});

test("network error resumes the current chunk after reading the server offset", async () => {
  const env = setup();
  await uploading(env);
  env.xhrs[0].onerror();
  assert.equal(env.state().phase, "uploading");
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, { status: "receiving", received_bytes: 0 });
  const retryTimer = await env.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  assert.equal(env.xhrs.length, 2);
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=0");
  assert.equal(env.state().uploadRetries, 1);
});

test("a recovery GET failure is retried before resuming at the committed offset", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  env.xhrs[0].onerror();
  await env.answer(env.is("GET", "/api/uploads/u1"), 503, {});
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().uploadId, "u1");
  assert.equal(env.state().uploadStateRetries, 1);
  assert.equal(env.calls.some((call) => call.method === "DELETE"), false);
  assert.match(env.state().workflow.text, /thử đọc lại/);

  const retryTimer = await env.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, {
    status: "receiving",
    received_bytes: 4,
  });
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=4");
  assert.deepEqual(env.xhrs[1].body, { start: 4, end: 8, size: 4 });
  assert.equal(env.state().uploadStateRetries, 0);
});

test("recovery GET failures are capped before the upload is cleaned up", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  env.xhrs[0].onerror();

  const expectedDelays = [1_000, 2_000, 4_000, 8_000, 16_000, 32_000];
  for (let failure = 1; failure <= expectedDelays.length + 1; failure += 1) {
    await env.answer(env.is("GET", "/api/uploads/u1"), 503, {});
    if (failure <= expectedDelays.length) {
      assert.equal(env.state().phase, "uploading");
      assert.equal(env.state().uploadStateRetries, failure);
      const retryTimer = await env.runNextTimer();
      assert.equal(retryTimer.delay, expectedDelays[failure - 1]);
    }
  }

  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().uploadId, null);
  assert.equal(env.state().uploadStateRetries, 6);
  assert.equal(env.calls.filter((call) => call.method === "DELETE").length, 1);
  await env.answer(env.is("DELETE", "/api/uploads/u1"), 204, {});
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().pendingCleanup.length, 0);
});

test("an active-writer conflict keeps the upload and retries the same committed offset", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  Object.assign(env.xhrs[0], {
    status: 409,
    responseText: JSON.stringify({
      detail: {
        code: "upload_busy",
        message: "Another upload content request is already running.",
        received_bytes: 0,
      },
    }),
  });
  env.xhrs[0].onload();
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, {
    status: "receiving",
    received_bytes: 0,
  });
  const retryTimer = await env.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.xhrs.length, 2);
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=0");
  assert.equal(env.calls.some((call) => call.method === "DELETE"), false);
});

test("an interrupted upload response is resumable", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  Object.assign(env.xhrs[0], {
    status: 400,
    responseText: JSON.stringify({
      detail: {
        code: "upload_interrupted",
        message: "The upload was interrupted. Resume from received_bytes.",
        received_bytes: 0,
      },
    }),
  });
  env.xhrs[0].onload();
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, {
    status: "receiving",
    received_bytes: 0,
  });
  const retryTimer = await env.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=0");
  assert.equal(env.calls.some((call) => call.method === "DELETE"), false);
});

test("a recovery GET with an advanced offset resumes from that offset", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  env.xhrs[0].onerror();
  await env.answer(env.is("GET", "/api/uploads/u1"), 200, {
    status: "receiving",
    received_bytes: 4,
  });
  assert.equal(env.xhrs[1].url, "/api/uploads/u1/content?offset=4");
  assert.deepEqual(env.xhrs[1].body, { start: 4, end: 8, size: 4 });
  assert.equal(env.state().uploadRetries, 0);
});

test("a chunk is resent six times with exponential backoff before cleanup", async () => {
  const env = setup();
  await uploading(env, CHUNKED_FILE, CHUNKED_POLICY);
  const expectedDelays = [1_000, 2_000, 4_000, 8_000, 16_000, 32_000];
  for (let attempt = 0; attempt < expectedDelays.length + 1; attempt += 1) {
    env.xhrs.at(-1).onerror();
    await env.answer(env.is("GET", "/api/uploads/u1"), 200, {
      status: "receiving",
      received_bytes: 0,
    });
    if (attempt < expectedDelays.length) {
      const retryTimer = await env.runNextTimer();
      assert.equal(retryTimer.delay, expectedDelays[attempt]);
      assert.match(env.state().workflow.text, /thử lại phần file/);
    }
  }
  assert.equal(env.xhrs.length, 7); // original request plus six automatic resends
  assert.equal(env.state().phase, "uploading");
  assert.ok(env.calls.some((call) => call.method === "DELETE"));
  await env.answer(env.is("DELETE", "/api/uploads/u1"), 204, {});
  assert.equal(env.state().phase, "error");
});

test("server refusal of the file is final; a 5xx is retryable", async () => {
  const refused = setup();
  await uploading(refused);
  Object.assign(refused.xhrs[0], { status: 415, responseText: "{}" });
  refused.xhrs[0].onload();
  await refused.answer(refused.is("DELETE", "/u1"), 204, {});
  assert.equal(refused.state().fileRefused, true);
  assert.equal(refused.state().retryable, false);
  assert.equal(canSubmit(refused.state()), false);

  const flaky = setup();
  await uploading(flaky);
  Object.assign(flaky.xhrs[0], { status: 503, responseText: "{}" });
  flaky.xhrs[0].onload();
  await flaky.answer(flaky.is("GET", "/u1"), 200, { status: "receiving", received_bytes: 0 });
  const retryTimer = await flaky.runNextTimer();
  assert.equal(retryTimer.delay, 1_000);
  assert.equal(flaky.state().phase, "uploading");
  assert.equal(flaky.state().fileRefused, false);
  assert.equal(flaky.xhrs.length, 2);
});

test("a failed upload step before the slot exists needs no cleanup", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/submit-requested" });
  await env.answer(env.is("POST", "/api/uploads"), 500, {});
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().starting, false);
  assert.equal(env.calls.some((c) => c.method === "DELETE"), false);
  assert.equal(env.state().retryable, true);
});

test("cancel mid-upload aborts the transfer, cleans up and ignores late events", async () => {
  const env = setup();
  await uploading(env);
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(env.xhrs[0].aborted, true);
  assert.equal(env.state().cancelling, true);
  env.store.dispatch({ type: "shell/upload-cancel-requested" }); // a second click is ignored
  env.xhrs[0].upload.onprogress({ loaded: 400, total: 500, lengthComputable: true });
  env.xhrs[0].onerror(); // late transport event of the aborted run
  await env.answer(env.is("DELETE", "/api/uploads/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.state().retryable, true);
  assert.equal(env.state().serverReady, false);
  assert.equal(env.state().uploadId, null);
  assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 1);
  assert.equal(env.state().progress.percent, 0);
});

test("cancel while the server checks the file drops the late completion", async () => {
  const env = setup();
  await uploading(env);
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  assert.equal(env.state().phase, "checking");
  const complete = env.calls.find((c) => c.url.endsWith("/complete"));
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(complete.init.signal.aborted, true);
  complete.resolve(reply(200, { status: "ready" }));
  await tick();
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.calls.some((c) => c.url === "/api/meetings"), false);
});

test("a lost completion response checks the upload before deciding, without deleting it", async () => {
  const env = setup();
  await uploading(env);
  doneXhr(env.xhrs[0]);
  const complete = env.calls.find((c) => c.url.endsWith("/complete"));
  complete.reject(new TypeError("connection lost"));
  await tick();
  assert.equal(env.state().phase, "checking");
  assert.equal(env.state().completionRecovery, true);
  assert.equal(env.calls.some((c) => c.method === "DELETE"), false);
  await env.answer(env.is("GET", "/u1"), 200, { status: "ready", received_bytes: 500 });
  assert.equal(env.state().phase, "creating");
  assert.equal(env.state().serverReady, true);
  await env.answer(env.is("POST", "/api/meetings"), 201, { meeting: { id: "m-recovered" } });
  assert.deepEqual(env.navigated, ["/meetings/m-recovered"]);
  assert.equal(env.calls.some((c) => c.method === "DELETE"), false);
});

test("validation can stay pending, retries completion only after uploaded, and caps transport retries", async () => {
  const env = setup();
  await uploading(env);
  doneXhr(env.xhrs[0]);
  await env.answer(env.is("POST", "/complete"), 200, { status: "validating" });
  assert.equal(env.state().phase, "checking");
  await env.runNextTimer(); // checking clock
  await env.runNextTimer(); // validation poll
  await env.answer(env.is("GET", "/u1"), 200, { status: "validating", received_bytes: 500 });
  assert.equal(env.state().phase, "checking");
  await env.runNextTimer();
  await env.runNextTimer();
  await env.answer(env.is("GET", "/u1"), 200, { status: "uploaded", received_bytes: 500 });
  assert.equal(env.calls.filter((c) => c.url.endsWith("/complete")).length, 2);
  await env.answer(env.is("POST", "/complete"), 200, { status: "ready" });
  assert.equal(env.state().phase, "creating");

  const lost = setup();
  await uploading(lost);
  doneXhr(lost.xhrs[0]);
  lost.calls.find((c) => c.url.endsWith("/complete")).reject(new TypeError("down"));
  await tick();
  for (let retry = 0; retry <= 6; retry += 1) {
    const status = lost.calls.find((c) => !c.done && c.method === "GET" && c.url.endsWith("/u1"));
    assert.ok(status, `missing validation GET ${retry}`);
    status.done = true;
    status.reject(new TypeError("down"));
    await tick();
    if (retry < 6) {
      while (!lost.calls.some((c) => !c.done && c.method === "GET" && c.url.endsWith("/u1"))) {
        await lost.runNextTimer();
      }
    }
  }
  assert.equal(lost.state().phase, "error");
  assert.equal(lost.state().validationUncertain, true);
  assert.equal(lost.state().uploadId, "u1");
  assert.equal(lost.calls.some((c) => c.method === "DELETE"), false);
  lost.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(lost.state().phase, "checking");
  assert.equal(lost.calls.filter((c) => c.method === "GET" && c.url.endsWith("/u1")).length, 8);
});

test("cancel during validation recovery aborts polls and ignores a late status reply", async () => {
  const env = setup();
  await uploading(env);
  doneXhr(env.xhrs[0]);
  env.calls.find((c) => c.url.endsWith("/complete")).reject(new TypeError("lost"));
  await tick();
  const status = env.calls.find((c) => !c.done && c.method === "GET" && c.url.endsWith("/u1"));
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(status.init.signal.aborted, true);
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
  status.resolve(reply(200, { status: "ready" }));
  await tick();
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.state().serverReady, false);
});

test("closing during upload cancels it instead of closing; a failed cleanup is remembered and retried", async () => {
  const env = setup();
  await uploading(env);
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(env.state().open, true);
  assert.equal(env.state().cancelling, true);
  await env.answer(env.is("DELETE", "/u1"), 500, {});
  assert.equal(env.state().phase, "aborted");
  assert.deepEqual(env.state().pendingCleanup, ["u1"]);
  env.store.dispatch({ type: "shell/file-selected", file: FILE }); // retries the pending delete
  assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 2);
  await env.answer(env.is("DELETE", "/u1"), 404, {}); // already gone counts as cleaned
  assert.deepEqual(env.state().pendingCleanup, []);
});

test("a new run waits for the old upload to be deleted before it creates a slot", async () => {
  const env = setup();
  await ready(env); // creating... fail it definitely to get back to a ready upload
  await env.answer(env.is("POST", "/api/meetings"), 422, { detail: { code: "upload_not_ready", message: "gone" } });
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().serverReady, false);
  assert.equal(env.state().retryable, true);
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().starting, true);
  assert.equal(env.calls.filter((c) => c.url === "/api/uploads" && c.method === "POST").length, 1); // not yet
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.calls.filter((c) => c.url === "/api/uploads" && c.method === "POST").length, 2);
});

test("create refused with a definite 4xx keeps the upload; editing then retrying creates again", async () => {
  const env = setup();
  await ready(env);
  await env.answer(env.is("POST", "/api/meetings"), 400, { detail: "bad title" });
  const failed = env.state();
  assert.equal(failed.phase, "error");
  assert.equal(failed.createAttemptFailed, true);
  assert.equal(failed.createUncertain, false);
  assert.equal(failed.serverReady, true);
  assert.equal(selectView(failed).createLabel.text, "Thử tạo lại");
  env.store.dispatch({ type: "shell/form-edited", form: { ...env.form, title: "Better" } });
  assert.equal(env.state().phase, "ready");
  assert.equal(env.state().formError, null);
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().phase, "creating");
  assert.equal(JSON.parse(env.calls.filter((c) => c.url === "/api/meetings").at(-1).init.body).title, "Better");
});

test("create with an unknown outcome resends the same payload; fields stay locked", async () => {
  const env = setup();
  await ready(env);
  env.calls.find((c) => c.url === "/api/meetings").reject(new TypeError("Failed to fetch"));
  await tick();
  const failed = env.state();
  assert.equal(failed.createUncertain, true);
  assert.equal(selectView(failed).fieldsDisabled, true);
  assert.equal(selectView(failed).createLabel.text, "Xác minh và thử lại");
  const payload = failed.createRetryPayload;
  env.store.dispatch({ type: "shell/file-selected", file: { name: "b.wav", size: 1 } }); // ignored while uncertain
  assert.equal(env.state(), failed);
  env.store.dispatch({ type: "shell/submit-requested" });
  const [first, second] = env.calls.filter((c) => c.url === "/api/meetings");
  assert.deepEqual(JSON.parse(second.init.body), payload);
  assert.equal(env.state().phase, "creating");
  await env.answer((c) => c === second, 201, { id: "m9" });
  assert.equal(env.state().phase, "saved");
  assert.ok(first);
});

test("closing keeps an uncertain create's upload and deletes any other", async () => {
  const uncertain = setup();
  await ready(uncertain);
  uncertain.calls.find((c) => c.url === "/api/meetings").reject(new TypeError("down"));
  await tick();
  uncertain.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(uncertain.state().open, false);
  assert.equal(uncertain.calls.some((c) => c.method === "DELETE"), false);

  const idle = setup();
  await ready(idle);
  await idle.answer(idle.is("POST", "/api/meetings"), 400, { detail: "no" });
  idle.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(idle.state().open, false);
  assert.ok(idle.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/u1")));
  assert.deepEqual(idle.state().form, initialState.form); // the dialog starts clean next time
});

test("the dialog cannot be closed while the meeting is being saved", async () => {
  const env = setup();
  await ready(env);
  const creating = env.state();
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(env.state(), creating);
  assert.equal(selectView(creating).closeDisabled, true);
});

test("a server that does not confirm ready fails the run and cleans up", async () => {
  const env = setup();
  await uploading(env);
  Object.assign(env.xhrs[0], { status: 204, responseText: "" });
  env.xhrs[0].onload();
  await env.answer(env.is("POST", "/complete"), 200, { status: "pending" });
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().retryable, true);
});

test("dropping several files is refused", async () => {
  const env = setup();
  await opened(env);
  env.store.dispatch({ type: "shell/multiple-files-dropped" });
  assert.equal(env.state().phase, "error");
  assert.ok(env.state().fileError);
});

test("reduce is pure: the same input gives the same output and the input is not mutated", () => {
  const frozen = Object.freeze({ ...initialState, form: Object.freeze({ ...initialState.form }) });
  const action = { type: "shell/dialog-opened", stored: { language: "en" } };
  assert.deepEqual(reduce(frozen, action), reduce(frozen, action));
  assert.equal(frozen.open, false);
});

test("a failure without a message gets the text of the step that failed", async () => {
  const env = setup();
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  env.store.dispatch({ type: "shell/policy-load-failed", meta: { id: env.state().policyRequest }, error: {} });
  assert.equal(env.state().formError.params.message.text, "Không tải được chính sách file.");
  const upload = setup();
  await uploading(upload);
  upload.store.dispatch({ type: "shell/upload-failed", meta: { runId: upload.state().runId }, error: { kind: "aborted", message: "aborted" } });
  await upload.answer(upload.is("DELETE", "/u1"), 204, {});
  assert.equal(upload.state().formError[0].params.reason.text, "Đã hủy lượt truyền file.");
});

// Round 2: the cleanup window, the browser closing the dialog, and stale ids taken from real effects.
const doneXhr = (xhr) => {
  Object.assign(xhr, { status: 204, responseText: "" });
  xhr.onload();
};

test("a cancel during offset recovery stands; the late state reply does not turn it into an error", async () => {
  const env = setup();
  await uploading(env);
  env.xhrs[0].onerror();
  assert.ok(env.calls.some((call) => call.method === "GET" && call.url.endsWith("/u1")));
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().cancelling, true);
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.state().failure, null);
  assert.deepEqual(env.state().pendingCleanup, []);
});

test("cancel then retry during offset recovery: the old state reply is ignored by the new run", async () => {
  const env = setup();
  await uploading(env);
  env.xhrs[0].onerror();
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().starting, false); // cancellation is still settling
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().starting, true);
  assert.equal(env.state().formError, null);
  assert.equal(env.calls.filter((c) => c.method === "POST" && c.url === "/api/uploads").length, 2);
  await env.answer((c) => c.method === "POST" && c.url === "/api/uploads" && !c.done, 200, { id: "u2" });
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().formError, null);
});

test("close during a cancel whose DELETE hangs closes the dialog and keeps the id pending", async () => {
  const env = setup();
  await uploading(env);
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(selectView(env.state()).closeDisabled, false);
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(env.state().open, false);
  assert.deepEqual(env.state().pendingCleanup, ["u1"]);
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().open, false);
  assert.equal(env.state().phase, "idle");
  assert.deepEqual(env.state().pendingCleanup, []);
});

test("the browser closing the dialog cancels what is in flight and does not reopen it", async () => {
  const env = setup();
  await uploading(env);
  env.store.dispatch({ type: "shell/dialog-closed" });
  assert.equal(env.state().open, false);
  assert.equal(env.xhrs[0].aborted, true);
  assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 1);
  const closed = env.state();
  env.store.dispatch({ type: "shell/dialog-closed" }); // the echo of our own close
  assert.equal(env.state(), closed);
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().open, false);
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} }); // reopening still works
  assert.equal(env.state().open, true);
});

test("the browser closing the dialog while the meeting is saved still lets the save finish", async () => {
  const env = setup();
  await ready(env);
  env.store.dispatch({ type: "shell/dialog-closed" });
  assert.equal(env.state().open, false);
  await env.answer(env.is("POST", "/api/meetings"), 201, { meeting: { id: "m1" } });
  assert.deepEqual(env.navigated, ["/meetings/m1"]);
});

test("replies carrying an id other than the current one are ignored, for every reply type", async () => {
  const env = setup();
  await uploading(env);
  const { runId } = env.state();
  const before = env.state();
  const old = { runId: runId - 1 };
  for (const action of [
    { type: "shell/upload-progressed", meta: old, loaded: 100, total: 500, lengthComputable: true },
    { type: "shell/upload-sent", meta: old },
    { type: "shell/upload-completed", meta: old, response: { body: { status: "ready" } } },
    { type: "shell/upload-completion-failed", meta: old, error: { status: 500 } },
    { type: "shell/upload-create-failed", meta: old, error: { status: 500 } },
  ]) {
    assert.deepEqual(reduce(before, action), { state: before }, action.type);
  }
});

test("create and policy replies of an older request are ignored", async () => {
  const env = setup();
  await ready(env);
  const creating = env.state();
  const staleCreate = { id: creating.createRequest - 1, payload: {} };
  assert.equal(reduce(creating, { type: "shell/meeting-create-failed", meta: staleCreate, error: { status: 500 } }).state, creating);
  assert.equal(reduce(creating, { type: "shell/meeting-created", meta: staleCreate, response: { body: { id: "x" } } }).state, creating);
  const reopened = createReopened();
  const stalePolicy = { id: reopened.policyRequest - 1 };
  assert.equal(reduce(reopened, { type: "shell/policy-load-failed", meta: stalePolicy, error: { status: 500 } }).state, reopened);
  assert.equal(reduce(reopened, { type: "shell/policy-loaded", meta: stalePolicy, response: { body: POLICY } }).state, reopened);
});

function createReopened() {
  let state = reduce(initialState, { type: "shell/dialog-opened", stored: {} }).state;
  state = reduce(state, { type: "shell/dialog-close-requested" }).state;
  return reduce(state, { type: "shell/dialog-opened", stored: {} }).state;
}

test("a repeated cancel does nothing and a late upload-sent during cleanup is dropped", async () => {
  const env = setup();
  await uploading(env);
  const run = env.state().runId;
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  const cancelling = env.state();
  assert.deepEqual(reduce(cancelling, { type: "shell/upload-cancel-requested" }), { state: cancelling });
  env.store.dispatch({ type: "shell/upload-sent", meta: { runId: run } }); // late event of the cancelled run
  assert.equal(env.state(), cancelling);
  assert.equal(env.calls.some((c) => c.url.endsWith("/complete")), false);
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
});

test("a 5xx on create is an unknown outcome: the retry resends the same payload", async () => {
  const env = setup();
  await ready(env);
  await env.answer(env.is("POST", "/api/meetings"), 503, {});
  assert.equal(env.state().createUncertain, true);
  const payload = env.state().createRetryPayload;
  assert.ok(payload);
  env.store.dispatch({ type: "shell/submit-requested" });
  const sent = env.calls.filter((c) => c.url === "/api/meetings");
  assert.deepEqual(JSON.parse(sent[1].init.body), payload);
});

test("a created upload or meeting without an id fails the step", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/submit-requested" });
  await env.answer(env.is("POST", "/api/uploads"), 200, {});
  assert.equal(env.state().phase, "error");
  assert.equal(env.state().uploadId, null);
  const meeting = setup();
  await ready(meeting);
  await meeting.answer(meeting.is("POST", "/api/meetings"), 201, { meeting: {} });
  assert.equal(meeting.state().createUncertain, true);
  assert.deepEqual(meeting.navigated, []);
});

test("buttons are disabled while the run starts or the work is busy", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/submit-requested" });
  let view = selectView(env.state());
  assert.equal(env.state().starting, true);
  assert.equal(view.closeDisabled, true);
  assert.equal(view.createDisabled, true);
  assert.equal(view.fieldsDisabled, true);
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  view = selectView(env.state());
  assert.equal(view.closeDisabled, false);
  assert.equal(view.createDisabled, true);
  assert.equal(view.cancelHidden, false);
  assert.equal(view.changeFileDisabled, true);
});

test("a pending DELETE survives close and is retried on the start path", async () => {
  const env = setup();
  await uploading(env);
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  await env.answer(env.is("DELETE", "/u1"), 500, {});
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.deepEqual(env.state().pendingCleanup, ["u1"]);
  env.store.dispatch({ type: "shell/dialog-opened", stored: {} });
  await env.answer(env.is("GET", "/api/uploads/policy") , 200, POLICY);
  env.store.dispatch({ type: "shell/file-selected", file: FILE });
  env.store.dispatch({ type: "shell/form-edited", form: env.form });
  await env.answer(env.is("DELETE", "/u1"), 500, {}); // the select path's retry fails again
  env.store.dispatch({ type: "shell/submit-requested" });
  assert.equal(env.state().starting, true);
  assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 3); // start path asks again
  await env.answer(env.is("DELETE", "/u1"), 204, {});
  assert.equal(env.state().starting, true); // the slot request is out
  await env.answer((c) => c.method === "POST" && c.url === "/api/uploads" && !c.done, 200, { id: "u2" });
  assert.equal(env.state().phase, "uploading");
});

test("auto-transcribe off: a completed create writes no handoff key", async () => {
  const env = setup();
  await selected(env);
  env.store.dispatch({ type: "shell/auto-transcribe-toggled", value: false });
  env.store.dispatch({ type: "shell/submit-requested" });
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  doneXhr(env.xhrs[0]);
  await env.answer(env.is("POST", "/complete"), 200, { status: "ready" });
  await env.answer(env.is("POST", "/api/meetings"), 201, { meeting: { id: "m1" } });
  assert.deepEqual(env.written, [
    ["local", "asr-studio:auto-transcribe", "0"],
    ["local", "asr-studio:last-language-hint", "en"],
  ]);
  assert.deepEqual(env.navigated, ["/meetings/m1"]);
});

// Video upload: the browser cuts the audio out first (effect "extract"), then the usual upload sends that audio.
const VIDEO = { name: "Weekly sync.mp4", size: 900 };
const AUDIO = { name: "Weekly sync.m4a", size: 60 };

async function extracting(env) {
  await selected(env, VIDEO);
  env.store.dispatch({ type: "shell/submit-requested" });
  await tick(); // the runner starts the extractor on a microtask
}

test("a video is extracted first; the upload slot and the PUT carry the audio file", async () => {
  const env = setup();
  await extracting(env);
  assert.equal(env.state().phase, "extracting");
  assert.equal(env.state().progress.visible, true);
  assert.equal(env.extracts[0].file, VIDEO);
  assert.equal(env.calls.some((c) => c.url === "/api/uploads" && c.method === "POST"), false); // no slot before the audio exists
  env.extracts[0].options.onProgress(0.4);
  assert.equal(env.state().progress.percent, 40);
  assert.equal(selectView(env.state()).cancelHidden, false);
  env.extracts[0].resolve(AUDIO);
  await tick();
  const slot = env.calls.find((c) => c.url === "/api/uploads" && c.method === "POST");
  assert.deepEqual(JSON.parse(slot.init.body), { filename: "Weekly sync.m4a", size_bytes: 60 });
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().progress.percent, 0); // the upload step starts from zero again
  assert.equal(env.xhrs[0].body, AUDIO);
  assert.equal(env.state().notice.text, "Đã tách audio khỏi video: gửi {after} thay vì {before}.");
  assert.deepEqual(env.state().notice.params, { after: { fmt: "bytes", value: 60 }, before: { fmt: "bytes", value: 900 } });
});

test("a non-video file skips extraction", async () => {
  const env = setup();
  await uploading(env);
  assert.equal(env.extracts.length, 0);
  assert.equal(env.state().notice, null);
});

test("extraction failure uploads the original file and shows a notice", async () => {
  const env = setup();
  await extracting(env);
  env.extracts[0].reject(Object.assign(new Error("x"), { kind: "codec" }));
  await tick();
  const slot = env.calls.find((c) => c.url === "/api/uploads" && c.method === "POST");
  assert.deepEqual(JSON.parse(slot.init.body), { filename: "Weekly sync.mp4", size_bytes: 900 });
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  assert.equal(env.xhrs[0].body, VIDEO);
  assert.equal(env.state().phase, "uploading");
  assert.equal(env.state().notice.text.startsWith("Không tách được audio"), true);
  assert.equal(env.state().notice.params.reason.text, "codec audio không copy được sang file audio");
});

test("a runner without an extractor falls back to the original file", async () => {
  const calls = [];
  const run = createRunner({ fetch: (url, init) => new Promise((resolve) => calls.push({ url, init, resolve })), XMLHttpRequest: class {}, setTimeout: () => 0, clearTimeout: () => {}, storage: {}, navigate: () => {} });
  const seen = [];
  run({ type: "extract", key: "k", file: VIDEO, ok: "ok", error: "bad", meta: 1 }, (a) => seen.push(a));
  await tick();
  assert.deepEqual(seen, [{ type: "bad", error: { message: "extract is not available", kind: "unsupported" }, meta: 1 }]);
});

test("cancel during extraction aborts it, leaks no upload slot and ignores the late result", async () => {
  const env = setup();
  await extracting(env);
  const { signal } = env.extracts[0].options;
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(signal.aborted, true);
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.state().cancelling, false);
  assert.equal(env.state().retryable, true);
  env.extracts[0].resolve(AUDIO); // a result that arrives after the cancel
  env.extracts[0].options.onProgress(0.9);
  await tick();
  assert.equal(env.calls.some((c) => c.url === "/api/uploads" && c.method === "POST"), false);
  assert.equal(env.calls.some((c) => c.method === "DELETE"), false);
  assert.equal(env.state().phase, "aborted");
  assert.equal(env.state().progress.percent, 0);
  // a new run after the cancel extracts again and uploads normally
  env.store.dispatch({ type: "shell/submit-requested" });
  await tick();
  assert.equal(env.extracts.length, 2);
  env.extracts[1].resolve(AUDIO);
  await tick();
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u2" });
  assert.equal(env.xhrs[0].body, AUDIO);
});

test("closing the dialog during extraction cancels it; the browser closing it aborts it", async () => {
  const env = setup();
  await extracting(env);
  env.store.dispatch({ type: "shell/dialog-close-requested" });
  assert.equal(env.extracts[0].options.signal.aborted, true);
  assert.equal(env.state().phase, "aborted");
  const second = setup();
  await extracting(second);
  second.store.dispatch({ type: "shell/dialog-closed" });
  assert.equal(second.extracts[0].options.signal.aborted, true);
  assert.equal(second.state().open, false);
});

test("a cancel during upload after extraction still deletes the slot", async () => {
  const env = setup();
  await extracting(env);
  env.extracts[0].resolve(AUDIO);
  await tick();
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  env.store.dispatch({ type: "shell/upload-cancel-requested" });
  assert.equal(env.xhrs[0].aborted, true);
  await env.answer(env.is("DELETE", "/api/uploads/u1"), 204, {});
  assert.equal(env.state().phase, "aborted");
});

test("extract replies of an older run or another phase are ignored", async () => {
  const env = setup();
  await extracting(env);
  const before = env.state();
  const stale = { runId: before.runId - 1 };
  for (const type of ["shell/extract-done", "shell/extract-failed", "shell/extract-progressed"]) {
    const result = reduce(before, { type, meta: stale, value: AUDIO, error: { kind: "codec" }, ratio: 0.5 });
    assert.equal(result.state, before, type);
  }
  env.extracts[0].resolve(AUDIO);
  await tick();
  const uploadingState = env.state();
  const late = reduce(uploadingState, { type: "shell/extract-failed", meta: { runId: uploadingState.runId }, error: { kind: "codec" } });
  assert.equal(late.state, uploadingState);
});

test("selecting another file clears the previous extraction notice and file", async () => {
  const env = setup();
  await extracting(env);
  env.extracts[0].reject(Object.assign(new Error("x"), { kind: "no-audio" }));
  await tick();
  await env.answer(env.is("POST", "/api/uploads"), 200, { id: "u1" });
  env.store.dispatch({
    type: "shell/upload-failed",
    meta: { runId: env.state().runId },
    error: { kind: "aborted", message: "aborted" },
  });
  await env.answer(env.is("DELETE", "/api/uploads/u1"), 204, {});
  assert.notEqual(env.state().notice, null);
  env.store.dispatch({ type: "shell/file-selected", file: FILE });
  assert.equal(env.state().notice, null);
  assert.equal(env.state().sendFile, null);
});
