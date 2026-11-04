// View of the connections dialog and the header buttons: reads store state, renders the DOM, dispatches actions.
// All state and rules live in provider-core.js (contract: docs/adr/0010-frontend-reducer-core.md). This module
// also creates the one store both providers share; antigravity.js imports it and renders the Google section.
import { providerChoice } from "./channel.js";
import { createRunner, createStore } from "./store.js";
import { setAttr, setProp, tr } from "./shell.js";
import { ANTIGRAVITY, CHATGPT, initialState, reduce, selectAsr, selectLauncher, selectView } from "./provider-core.js";

const launcherElements = {
  chatgpt: document.querySelector("#launcher-chatgpt"),
  antigravity: document.querySelector("#launcher-antigravity"),
};

const settingsButton = document.querySelector("#settings-open");
const asrEls = {
  status: document.querySelector("#asr-status"),
  defaultText: document.querySelector("#asr-default"),
  list: document.querySelector("#asr-models"),
};

const els = {
  dialog: document.querySelector("#connections-dialog"),
  close: document.querySelector("#connections-close"),
  badge: document.querySelector("#connection-state-badge"),
  status: document.querySelector("#connection-status"),
  account: document.querySelector("#connection-account"),
  verificationRow: document.querySelector("#connection-verification-row"),
  verificationSummary: document.querySelector("#connection-verification-summary"),
  message: document.querySelector("#connection-message"),
  error: document.querySelector("#connection-error"),
  errorMessage: document.querySelector("#connection-error-message"),
  mutationReason: document.querySelector("#connection-mutation-reason"),
  retryPoll: document.querySelector("#connection-retry-poll"),
  signIn: document.querySelector("#connection-sign-in"),
  pendingActions: document.querySelector("#connection-pending-actions"),
  pendingCopy: document.querySelector("#connection-pending-copy"),
  cancelSignIn: document.querySelector("#connection-cancel-sign-in"),
  disconnect: document.querySelector("#connection-disconnect"),
  disconnectTrigger: document.querySelector("#connection-disconnect-trigger"),
  disconnectConfirm: document.querySelector("#connection-disconnect-confirm"),
  disconnectKeep: document.querySelector("#connection-disconnect-keep"),
  disconnectConfirmButton: document.querySelector("#connection-disconnect-confirm-button"),
  modelSection: document.querySelector("#connection-model-section"),
  modelForm: document.querySelector("#connection-model-form"),
  modelSelect: document.querySelector("#connection-model"),
  effortSelect: document.querySelector("#connection-effort"),
  modelStatus: document.querySelector("#connection-model-status"),
  modelSave: document.querySelector("#connection-model-save"),
  verify: document.querySelector("#connection-verify"),
  verificationResult: document.querySelector("#connection-verification-result"),
};

// Rebuild a select only when its option list changed, so an open list and the focus survive other updates.
function renderOptions(select, options, value) {
  const signature = JSON.stringify(options);
  if (select.dataset.options !== signature) {
    select.dataset.options = signature;
    select.replaceChildren(
      ...options.map((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = tr(item.label);
        option.disabled = Boolean(item.disabled);
        return option;
      }),
    );
  }
  setProp(select, "value", options.some((item) => item.value === value) ? value : "");
}

// The parts of a provider section that both providers have; each view adds its own login controls.
export function renderSection(section, d, s) {
  const view = selectView(d, s);
  setAttr(section.badge, "data-state", s.badge.state);
  setProp(section.badge, "textContent", tr(s.badge.text));
  if (s.statusText) setProp(section.status, "textContent", tr(s.statusText));
  if (view.account !== null) setProp(section.account, "textContent", tr(view.account));
  setProp(section.verificationRow, "hidden", !s.summary);
  if (s.summary) setProp(section.verificationSummary, "textContent", tr(s.summary));
  else if (d.copy.noVerification) setProp(section.verificationSummary, "textContent", tr(d.copy.noVerification));
  setProp(section.message, "textContent", tr(s.message));
  setProp(section.error, "hidden", !s.error);
  setProp(section.errorMessage, "textContent", s.error ? tr(s.error.text) : "");
  if (section.mutationReason) {
    setProp(section.mutationReason, "hidden", !view.mutationReason);
    setProp(section.mutationReason, "textContent", tr(view.mutationReason));
  }
  if (section.retryPoll) {
    setAttr(section.error, "data-code", s.error ? s.error.code : "");
    setProp(section.retryPoll, "hidden", !(s.error && s.error.retryPoll));
  }
  setProp(section.signIn, "hidden", view.signInHidden);
  setProp(section.signIn, "disabled", s.signInDisabled);
  setProp(section.modelSection, "hidden", view.modelSectionHidden);
  if (s.modelOptions) renderOptions(section.modelSelect, s.modelOptions, s.selectedModel);
  if (view.effortOptions) renderOptions(section.effortSelect, view.effortOptions, view.effortValue);
  else setProp(section.effortSelect, "value", view.effortValue);
  setProp(section.modelSelect, "disabled", view.modelDisabled);
  setProp(section.effortSelect, "disabled", view.effortDisabled);
  setProp(section.modelSave, "disabled", view.saveDisabled);
  setProp(section.verify, "disabled", view.verifyDisabled);
  const describedBy = view.mutationReason ? section.mutationReason?.id : "";
  for (const element of [section.modelSelect, section.effortSelect, section.modelSave, section.verify, section.disconnectTrigger, section.disconnectConfirmButton]) {
    if (!element) continue;
    if (element.dataset.baseDescribedBy === undefined) element.dataset.baseDescribedBy = element.getAttribute("aria-describedby") || "";
    const ids = [element.dataset.baseDescribedBy, describedBy].filter(Boolean).join(" ");
    if (ids) setAttr(element, "aria-describedby", ids);
    else element.removeAttribute("aria-describedby");
  }
  setAttr(section.modelStatus, "data-state", s.modelStatus.state);
  setProp(section.modelStatus, "textContent", tr(s.modelStatus.text));
  setAttr(section.verificationResult, "data-state", s.verifyResult.state);
  setProp(section.verificationResult, "textContent", tr(s.verifyResult.text));
  setProp(section.disconnect, "hidden", view.disconnectHidden);
  if (section.disconnectConfirm) setProp(section.disconnectConfirm, "hidden", !s.confirming);
  if (section.disconnectTrigger) {
    setProp(section.disconnectTrigger, "hidden", s.confirming);
    setProp(section.disconnectTrigger, "disabled", view.disconnectDisabled);
  }
  if (section.disconnectConfirmButton) {
    setProp(section.disconnectConfirmButton, "disabled", view.disconnectDisabled);
    setProp(section.disconnectConfirmButton, "textContent", tr(view.disconnectLabel));
  }
  return view;
}

function renderLauncher(d, s) {
  const launcher = selectLauncher(d, s);
  if (!launcher) return;
  const button = launcherElements[d.id];
  setAttr(button, "data-state", launcher.status);
  setAttr(button, "aria-label", tr(launcher.ariaLabel));
  setProp(button, "title", tr(launcher.title));
}

// One row per model. The list is rebuilt only when what it shows changed, so a focused copy button survives.
function renderAsr(asr) {
  const view = selectAsr(asr);
  setProp(asrEls.status, "textContent", tr(view.text));
  setProp(asrEls.defaultText, "hidden", !view.defaultText);
  setProp(asrEls.defaultText, "textContent", tr(view.defaultText));
  const signature = JSON.stringify(view.rows.map(({ copy, ...row }) => row));
  if (asrEls.list.dataset.rows !== signature) {
    asrEls.list.dataset.rows = signature;
    asrEls.list.replaceChildren(...view.rows.map(asrRow));
  }
  // The copy feedback changes without a rebuild, so the focused button stays.
  for (const row of view.rows) {
    const result = asrEls.list.querySelector(`[data-model="${row.id}"] .asr-command-result`);
    if (result) setProp(result, "textContent", tr(row.copy));
  }
}

function asrRow(row) {
  const item = document.createElement("li");
  item.className = "asr-model";
  item.dataset.model = row.id;
  item.dataset.state = row.state;
  const head = document.createElement("div");
  head.className = "asr-model-head";
  const name = document.createElement("strong");
  name.textContent = row.label;
  head.append(name);
  if (row.isDefault) head.append(asrBadge("default", t("Mặc định")));
  head.append(asrBadge("status", tr(row.status)));
  const note = document.createElement("p");
  note.textContent = tr(row.note);
  item.append(head, note);
  if (row.guidance) {
    const guidance = document.createElement("p");
    guidance.className = "asr-model-guidance";
    guidance.textContent = tr(row.guidance);
    item.append(guidance);
  }
  if (row.command) item.append(asrCommand(row));
  return item;
}

function asrBadge(kind, text) {
  const badge = document.createElement("span");
  badge.className = "asr-badge";
  badge.dataset.kind = kind;
  badge.textContent = text;
  return badge;
}

// A read-only field with a copy button; when the clipboard is refused the command stays selected for Ctrl+C.
function asrCommand(row) {
  const wrap = document.createElement("div");
  wrap.className = "asr-command";
  const label = document.createElement("label");
  label.htmlFor = `asr-command-${row.id}`;
  label.textContent = t("Lệnh chuẩn bị model");
  const field = document.createElement("div");
  field.className = "asr-command-field";
  const input = document.createElement("input");
  input.id = label.htmlFor;
  input.type = "text";
  input.readOnly = true;
  input.spellcheck = false;
  input.value = row.command;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = t("Sao chép");
  const result = document.createElement("span");
  result.className = "asr-command-result";
  result.setAttribute("role", "status");
  button.addEventListener("click", () => dispatch({ type: "provider/asr-copy-requested", id: row.id }));
  field.append(input, button);
  wrap.append(label, field, result);
  return wrap;
}

function render(state) {
  renderAsr(state.asr);
  const s = state.chatgpt;
  const view = renderSection(els, CHATGPT, s);
  renderLauncher(CHATGPT, s);
  renderLauncher(ANTIGRAVITY, state.antigravity);
  if (s.loginCopy) setProp(els.pendingCopy, "textContent", tr(s.loginCopy));
  setProp(els.pendingActions, "hidden", view.pendingHidden);
  setProp(els.cancelSignIn, "disabled", view.cancelDisabled);
  if (s.ui) {
    const focus = { "focus:sign-in": els.signIn, "focus:disconnect-confirm": els.disconnectConfirmButton, "focus:disconnect-trigger": els.disconnectTrigger }[s.ui];
    if (focus) focus.focus();
    dispatch({ type: "provider/ui-handled", provider: CHATGPT.id });
  }
  // The Google button opens the dialog at its section once both status reads have settled.
  if (state.launcher.scroll && !s.loading && !state.antigravity.loading) {
    document.querySelector("#antigravity-section").scrollIntoView({ block: "start" });
    dispatch({ type: "provider/scroll-handled" });
  }
}

const base = createRunner({
  fetch: (url, init) => fetch(url, init),
  setTimeout: (callback, delay) => window.setTimeout(callback, delay),
  clearTimeout: (id) => window.clearTimeout(id),
  XMLHttpRequest,
  storage: { local: () => window.localStorage, session: () => window.sessionStorage },
  navigate: (url) => window.location.assign(url),
  open: (url, target) => window.open(url, target),
  calls: {
    "provider.choice-changed": () => providerChoice.dispatch({ type: "channel/provider-choice-changed" }),
    "clipboard.write": (text) => {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      return navigator.clipboard.writeText(text);
    },
    // The row's command field is the only DOM this touches; its id is one of the three catalog ids.
    "asr.select-command": (id) => {
      const input = document.getElementById(`asr-command-${id}`);
      input?.focus();
      input?.select();
    },
  },
  now: () => Date.now(),
});
// The reserved window shows a sentence in the page language; the effect carries it as data.
const run = (effect, dispatch) => base(effect.text && typeof effect.text === "object" ? { ...effect, text: tr(effect.text) } : effect, dispatch);

export const store = createStore({ reduce, initialState, run });
export const { dispatch } = store;
store.subscribe(render);

// A pending login enters the state through the boot action; writes go through storage effects.
export function readStored(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

// The gear opens the dialog at the top; a provider button opens it at that provider's section.
function openDialog(launcher) {
  if (!els.dialog.open) els.dialog.showModal();
  els.dialog.querySelector(".connections-body").scrollTop = 0; // a closed dialog keeps its scroll
  dispatch({ type: "provider/dialog-opened", launcher });
  if (launcher === ANTIGRAVITY.id) document.querySelector("#antigravity-title").focus({ preventScroll: true });
}
settingsButton.addEventListener("click", () => openDialog(null));
for (const provider of [CHATGPT.id, ANTIGRAVITY.id]) {
  launcherElements[provider].addEventListener("click", () => openDialog(provider));
}
els.close.addEventListener("click", () => els.dialog.close());
els.dialog.addEventListener("click", (event) => {
  if (event.target === els.dialog) els.dialog.close();
});

const chatgpt = (action) => dispatch({ ...action, provider: CHATGPT.id });
els.signIn.addEventListener("click", () => chatgpt({ type: "provider/login-requested" }));
els.cancelSignIn.addEventListener("click", () => chatgpt({ type: "provider/login-cancel-requested" }));
els.retryPoll.addEventListener("click", () => chatgpt({ type: "provider/poll-retry-requested" }));
els.modelSelect.addEventListener("change", () => chatgpt({ type: "provider/choice-edited", field: "model", value: els.modelSelect.value }));
els.effortSelect.addEventListener("change", () => chatgpt({ type: "provider/choice-edited", field: "effort", value: els.effortSelect.value }));
els.modelForm.addEventListener("submit", (event) => {
  event.preventDefault();
  chatgpt({ type: "provider/save-requested" });
});
els.verify.addEventListener("click", () => chatgpt({ type: "provider/verify-requested" }));
els.disconnectTrigger.addEventListener("click", () => chatgpt({ type: "provider/disconnect-confirm-toggled", open: true }));
els.disconnectKeep.addEventListener("click", () => chatgpt({ type: "provider/disconnect-confirm-toggled", open: false }));
els.disconnectConfirmButton.addEventListener("click", () => chatgpt({ type: "provider/disconnect-requested" }));

chatgpt({ type: "provider/boot-restored", raw: readStored(CHATGPT.storageKey), now: Date.now() });
dispatch({ type: "provider/header-requested" });
