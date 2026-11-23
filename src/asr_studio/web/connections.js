// View of the connections dialog and the header buttons: reads store state, renders the DOM, dispatches actions.
// All state and rules live in provider-core.js (contract: docs/adr/0010-frontend-reducer-core.md). This module
// also creates the one store both providers share; antigravity.js imports it and renders the Google section.
import { asrSettings, providerChoice } from "./channel.js";
import { selectDevices } from "./extension-devices-core.js";
import { createRunner, createStore } from "./store.js";
import { setAttr, setProp, tr } from "./shell.js";
import { ANTIGRAVITY, CHATGPT, SETTINGS_SECTION_KEY, SETTINGS_SECTIONS, SETTINGS_TOP_LEVELS, initialState, reduce, selectAsr, selectLauncher, selectProviderOverview, selectView, settingsParent } from "./provider-core.js";
import { selectPreferences } from "./processing-preferences-core.js";

const launcherElements = {
  chatgpt: document.querySelector("#launcher-chatgpt"),
  antigravity: document.querySelector("#launcher-antigravity"),
};

const settingsButton = document.querySelector("#settings-open");
const settingsBody = document.querySelector(".connections-body");
const settingsTabs = document.querySelector("#settings-tabs");
const phoneLayout = window.matchMedia("(max-width: 680px)");
const overviewEls = {
  panel: document.querySelector("#settings-provider-overview"),
  list: document.querySelector("#provider-overview-list"),
};
const providerPages = {
  openai: document.querySelector("#settings-page-openai"),
  antigravity: document.querySelector("#settings-page-antigravity"),
};
const asrEls = {
  status: document.querySelector("#asr-status"),
  defaultText: document.querySelector("#asr-default"),
  list: document.querySelector("#asr-models"),
  control: document.querySelector("#asr-default-control"),
  select: document.querySelector("#asr-default-select"),
  refusal: document.querySelector("#asr-default-refusal"),
};

const deviceEls = {
  panel: document.querySelector("#settings-panel-devices"),
  form: document.querySelector("#extension-device-code-form"),
  lifetime: document.querySelector("#extension-device-lifetime"),
  create: document.querySelector("#extension-device-code-button"),
  notice: document.querySelector("#extension-device-notice"),
  error: document.querySelector("#extension-device-error"),
  errorMessage: document.querySelector("#extension-device-error-message"),
  codeOutput: document.querySelector("#extension-device-code-output"),
  code: document.querySelector("#extension-device-code"),
  codeExpiry: document.querySelector("#extension-device-code-expiry"),
  codeLifetime: document.querySelector("#extension-device-code-lifetime"),
  copy: document.querySelector("#extension-device-code-copy"),
  copyStatus: document.querySelector("#extension-device-code-copy-status"),
  endpoints: document.querySelector("#extension-device-endpoints"),
  endpointsEmpty: document.querySelector("#extension-device-endpoints-empty"),
  listStatus: document.querySelector("#extension-device-list-status"),
  listEmpty: document.querySelector("#extension-device-list-empty"),
  showRevoked: document.querySelector("#extension-device-show-revoked"),
  list: document.querySelector("#extension-device-list"),
};

const processingPreferenceEls = {
  title: document.querySelector("#automatic-preferences-title"),
  form: document.querySelector("#automatic-preferences-form"),
  version: document.querySelector("#automatic-preferences-version"),
  asrChoice: document.querySelector("#automatic-preferences-asr-choice"),
  asrLanguage: document.querySelector("#automatic-preferences-asr-language"),
  provider: document.querySelector("#automatic-preferences-provider"),
  providerHelp: document.querySelector("#automatic-preferences-provider-help"),
  modelField: document.querySelector("#automatic-preferences-model-field"),
  model: document.querySelector("#automatic-preferences-model"),
  effortField: document.querySelector("#automatic-preferences-effort-field"),
  effort: document.querySelector("#automatic-preferences-effort"),
  outputLanguage: document.querySelector("#automatic-preferences-output-language"),
  notice: document.querySelector("#automatic-preferences-notice"),
  error: document.querySelector("#automatic-preferences-error"),
  save: document.querySelector("#automatic-preferences-save"),
};

const els = {
  dialog: document.querySelector("#connections-dialog"),
  close: document.querySelector("#connections-close"),
  badge: document.querySelector("#connection-state-badge"),
  ledger: document.querySelector("#connection-ledger"),
  accountDetails: document.querySelector("#connection-account-details"),
  verificationSection: document.querySelector("#connection-verification-section"),
  status: document.querySelector("#connection-status"),
  account: document.querySelector("#connection-account"),
  verificationRow: document.querySelector("#connection-verification-row"),
  verificationSummary: document.querySelector("#connection-verification-summary"),
  message: document.querySelector("#connection-message"),
  error: document.querySelector("#connection-error"),
  errorMessage: document.querySelector("#connection-error-message"),
  mutationReason: document.querySelector("#connection-mutation-reason"),
  retryPoll: document.querySelector("#connection-retry-poll"),
  relogin: document.querySelector("#connection-relogin"),
  signIn: document.querySelector("#connection-sign-in"),
  pendingActions: document.querySelector("#connection-pending-actions"),
  pendingCopy: document.querySelector("#connection-pending-copy"),
  cancelSignIn: document.querySelector("#connection-cancel-sign-in"),
  callback: document.querySelector("#connection-callback"),
  callbackForm: document.querySelector("#connection-callback-form"),
  callbackInput: document.querySelector("#connection-callback-input"),
  callbackSubmit: document.querySelector("#connection-callback-submit"),
  registration: document.querySelector("#connection-registration"),
  registrationTrigger: document.querySelector("#connection-registration-trigger"),
  registrationConfirm: document.querySelector("#connection-registration-confirm"),
  registrationName: document.querySelector("#connection-registration-name"),
  registrationKeep: document.querySelector("#connection-registration-keep"),
  registrationConfirmButton: document.querySelector("#connection-registration-confirm-button"),
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
  if (s.statusText) {
    setProp(section.status, "textContent", tr(s.statusText));
    setProp(section.status, "hidden", tr(s.statusText) === tr(s.badge.text));
  } else {
    setProp(section.status, "hidden", true);
  }
  if (view.account !== null) setProp(section.account, "textContent", tr(view.account));
  if (section.ledger) setProp(section.ledger, "hidden", !s.info?.account?.label);
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
  if (section.callback) {
    setProp(section.callback, "hidden", !s.login);
    setProp(section.callbackSubmit, "disabled", s.callbackPending || s.cancelling);
    setProp(section.callbackInput, "disabled", s.callbackPending || s.cancelling);
  }
  if (section.registration) {
    setProp(section.registration, "hidden", view.registrationHidden);
    setProp(section.registrationConfirm, "hidden", !view.registrationConfirming);
    setProp(section.registrationTrigger, "hidden", view.registrationConfirming);
    setProp(section.registrationTrigger, "disabled", view.registrationDisabled);
    setProp(section.registrationConfirmButton, "disabled", view.registrationDisabled);
    setProp(section.registrationConfirmButton, "textContent", tr(view.registrationSubmitting && s.pending.recreate ? view.registrationSubmitting : view.registrationLabel));
  }
  if (section.relogin) {
    setProp(section.relogin, "hidden", view.reloginHidden);
    setProp(section.relogin, "disabled", view.reloginDisabled);
  }
  if (section.accountDetails) {
    const focusInside = section.accountDetails.contains(document.activeElement);
    const shouldOpen = s.info?.status !== "connected" || Boolean(s.login || s.error || s.confirming || s.recreateConfirming || focusInside);
    if (shouldOpen) section.accountDetails.open = true;
    else if (s.info?.status === "connected") section.accountDetails.open = false;
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

function overviewRow(row) {
  const item = document.createElement("li");
  item.className = "provider-overview-item";
  item.dataset.settingsOverview = row.id;
  if (row.disabled) item.setAttribute("aria-disabled", "true");

  const content = row.disabled ? document.createElement("div") : document.createElement("button");
  content.className = "provider-overview-row";
  if (!row.disabled) {
    content.type = "button";
    content.dataset.settingsTab = row.id;
    content.setAttribute("aria-label", `${tr(row.name)}: ${tr(row.status)}${row.model ? `, ${tr(row.model)}` : ""}`);
  }
  const name = document.createElement("strong");
  name.textContent = tr(row.name);
  const status = document.createElement("span");
  status.className = "provider-overview-secondary provider-overview-status";
  status.textContent = tr(row.status);
  content.append(name, status);
  if (row.model) {
    const model = document.createElement("span");
    model.className = "provider-overview-secondary provider-overview-model";
    model.textContent = tr(row.model);
    content.append(model);
  }
  if (!row.disabled) {
    const arrow = document.createElement("span");
    arrow.className = "provider-overview-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "›";
    content.append(arrow);
  }
  item.append(content);
  return item;
}

function renderOverview(state) {
  const rows = selectProviderOverview(state);
  if (!overviewEls.list.children.length) overviewEls.list.replaceChildren(...rows.map(overviewRow));
  for (const row of rows) {
    const item = overviewEls.list.querySelector(`[data-settings-overview="${row.id}"]`);
    if (!item) continue;
    const content = item.querySelector(".provider-overview-row");
    const name = content.querySelector("strong");
    const status = content.querySelector(".provider-overview-status");
    setProp(name, "textContent", tr(row.name));
    setProp(status, "textContent", tr(row.status));
    if (row.model) {
      let model = content.querySelector(".provider-overview-model");
      if (!model) {
        model = document.createElement("span");
        model.className = "provider-overview-secondary provider-overview-model";
        content.append(model);
      }
      setProp(model, "textContent", tr(row.model));
    } else {
      content.querySelector(".provider-overview-model")?.remove();
    }
    if (!row.disabled) content.setAttribute("aria-label", `${tr(row.name)}: ${tr(row.status)}${row.model ? `, ${tr(row.model)}` : ""}`);
  }
}

// One row per model. The list is rebuilt only when what it shows changed, so a focused button survives.
// Progress numbers and copy feedback change in place; a rebuild would drop the focus from Hủy every second.
function renderAsr(asr) {
  const view = selectAsr(asr);
  setProp(asrEls.status, "textContent", tr(view.text));
  const control = view.defaultControl;
  setProp(asrEls.control, "hidden", !control);
  setProp(asrEls.defaultText, "hidden", !control || control.disabled);
  if (control) {
    renderOptions(asrEls.select, control.options, control.value);
    setProp(asrEls.select, "disabled", control.disabled);
    setProp(asrEls.defaultText, "textContent", tr(control.text));
    asrEls.defaultText.classList.toggle("is-broken", control.broken);
    setProp(asrEls.refusal, "hidden", !control.refusal);
    setProp(asrEls.refusal, "textContent", tr(control.refusal));
  }
  const signature = JSON.stringify(view.rows.map(({ copy, progress, ...row }) => ({ ...row, progressKind: progress ? (progress.ratio === null ? "indeterminate" : "bar") : null })));
  if (asrEls.list.dataset.rows !== signature) {
    asrEls.list.dataset.rows = signature;
    // A click on an install button replaces it: remember whose it was, so the focus can follow.
    const held = document.activeElement?.closest?.("#asr-models [data-action]");
    if (held) asrEls.list.dataset.focusRow = held.closest("[data-model]")?.dataset.model ?? "";
    asrEls.list.replaceChildren(...view.rows.map(asrRow));
  }
  restoreAsrFocus();
  for (const row of view.rows) {
    const item = asrEls.list.querySelector(`[data-model="${row.id}"]`);
    const result = item.querySelector(".asr-command-result");
    if (result) setProp(result, "textContent", tr(row.copy));
    const bar = item.querySelector(".asr-progress");
    if (bar && row.progress) {
      if (row.progress.ratio !== null) setProp(bar, "value", row.progress.ratio);
      setProp(item.querySelector(".asr-progress-detail"), "textContent", tr(row.progress.detail));
    }
  }
}

function deviceButton(label, action, id, kind, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = kind;
  button.dataset.deviceAction = action;
  button.dataset.deviceId = id;
  button.disabled = disabled;
  button.textContent = tr(label);
  return button;
}

function deviceLedgerRow(label, value, { code = false } = {}) {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = tr(label);
  const description = document.createElement("dd");
  if (code) {
    const valueElement = document.createElement("code");
    valueElement.textContent = value;
    description.append(valueElement);
  } else {
    description.textContent = tr(value);
  }
  row.append(term, description);
  return row;
}

function deviceRow(row) {
  const item = document.createElement("li");
  item.className = "extension-device";
  item.dataset.device = row.id;
  const deviceLabel = tr(row.deviceLabel);

  const head = document.createElement("div");
  head.className = "extension-device-head";
  const id = document.createElement("span");
  id.className = "extension-device-id";
  id.tabIndex = -1;
  id.textContent = tr(row.deviceLabel);
  id.setAttribute("aria-label", tr(row.deviceAriaLabel));
  if (row.device_id) id.title = row.device_id;
  const status = document.createElement("span");
  status.className = "device-status";
  status.dataset.status = row.statusValue;
  status.textContent = tr(row.status);
  head.append(id, status);

  const ledger = document.createElement("dl");
  ledger.className = "extension-device-ledger";
  ledger.append(
    deviceLedgerRow(t("Mã bản ghi server"), row.recordLabel, { code: true }),
    deviceLedgerRow(t("Endpoint"), row.server_url ? row.server_url : t("Chưa xác định"), { code: Boolean(row.server_url) }),
    deviceLedgerRow(t("Thời hạn"), tr(row.lifetime)),
    deviceLedgerRow(t("Hết hạn"), tr(row.expires)),
    deviceLedgerRow(t("Lần thấy gần nhất"), tr(row.lastSeen)),
  );

  const actions = document.createElement("div");
  actions.className = "extension-device-actions";
  const repair = deviceButton(
    t("Cấp mã ghép lại"),
    "repair",
    row.id,
    "secondary-button",
    !row.canRepair,
  );
  repair.setAttribute("aria-label", tr(t("Cấp mã ghép lại cho thiết bị {id}", { id: deviceLabel })));
  actions.append(repair);

  if (row.statusValue !== "revoked") {
    const revoke = deviceButton(
      row.revokePending ? t("Đang thu hồi…") : t("Thu hồi"),
      row.confirming ? "revoke-cancel" : "revoke",
      row.id,
      "text-button device-revoke",
      row.revokePending,
    );
    revoke.setAttribute("aria-label", tr(t("Thu hồi thiết bị {id}", { id: deviceLabel })));
    actions.append(revoke);
  }

  if (row.confirming) {
    const confirm = document.createElement("div");
    confirm.className = "extension-device-confirm";
    const copy = document.createElement("p");
    copy.textContent = tr(t("Thu hồi sẽ vô hiệu hóa các phiên extension của thiết bị này. Bạn có chắc không?"));
    const keep = deviceButton(t("Giữ lại"), "revoke-cancel", row.id, "secondary-button");
    const confirmButton = deviceButton(t("Thu hồi thiết bị"), "revoke-confirm", row.id, "primary-button");
    confirm.append(copy, keep, confirmButton);
    actions.append(confirm);
  }

  item.append(head, ledger, actions);
  return item;
}

function restoreDeviceFocus() {
  const id = deviceEls.list.dataset.focusDevice;
  if (!id) return;
  if (!els.dialog.open || deviceEls.panel.hidden) {
    clearDeviceFocus();
    return;
  }
  const action = deviceEls.list.dataset.focusAction;
  const target = deviceActionTarget(id, action);
  const row = deviceEls.list.querySelector(`[data-device="${CSS.escape(id)}"]`);
  if (target && !target.disabled) {
    target.focus({ preventScroll: true });
    clearDeviceFocus();
  } else if (!target) {
    row?.querySelector(".extension-device-id")?.focus({ preventScroll: true });
    clearDeviceFocus();
  }
}

function deviceActionTarget(id, action) {
  const candidates = {
    revoke: ["revoke-confirm", "revoke-cancel", "revoke"],
    "revoke-confirm": ["revoke", "revoke-confirm"],
    "revoke-cancel": ["revoke", "revoke-cancel"],
    repair: ["repair"],
  }[action] || [action];
  return candidates
    .map((candidate) => [...deviceEls.list.querySelectorAll("[data-device-action]")].find((element) => element.dataset.deviceId === id && element.dataset.deviceAction === candidate))
    .find(Boolean) || null;
}

function clearDeviceFocus() {
  delete deviceEls.create.dataset.focusRecoveryRequest;
  for (const name of ["focusDevice", "focusAction", "focusCodeRequest", "focusRevokeRequest"]) delete deviceEls.list.dataset[name];
}

function forgetDeviceFocusOnMovement(event) {
  if (!deviceEls.create.dataset.focusRecoveryRequest && !deviceEls.list.dataset.focusDevice) return;
  if (event.type === "focusin" && event.target === deviceEls.create) return;
  if (event.type === "pointerdown" && event.target === deviceEls.create) return;
  clearDeviceFocus();
}

function renderDeviceRows(view) {
  const signature = JSON.stringify(view.rows);
  if (deviceEls.list.dataset.rows !== signature) {
    const active = document.activeElement?.closest?.("[data-device-action]");
    if (active) {
      deviceEls.list.dataset.focusDevice = active.dataset.deviceId;
      deviceEls.list.dataset.focusAction = active.dataset.deviceAction;
      if (view.pendingCode) deviceEls.list.dataset.focusCodeRequest = String(view.codeRequest);
      if (view.pendingRevoke) deviceEls.list.dataset.focusRevokeRequest = String(view.revokeRequest);
    }
    deviceEls.list.dataset.rows = signature;
    deviceEls.list.replaceChildren(...view.rows.map(deviceRow));
  }
  restoreDeviceFocus();
}

function focusDeviceRecovery(view) {
  if (!els.dialog.open || deviceEls.panel.hidden) {
    clearDeviceFocus();
    return;
  }
  let target = null;
  if (view.ui === "focus-retry") {
    if (view.pairingFor) {
      if (deviceEls.list.dataset.focusCodeRequest !== String(view.codeRequest)) return;
      target = deviceActionTarget(view.pairingFor, "repair");
    } else if (deviceEls.create.dataset.focusRecoveryRequest === String(view.codeRequest)) {
      target = deviceEls.create;
    }
  } else if (view.ui === "focus-revoke") {
    if (deviceEls.list.dataset.focusRevokeRequest !== String(view.revokeRequest)) return;
    target = deviceActionTarget(view.revokeTarget, "revoke-confirm");
  }
  if (!target || target.disabled) return;
  const active = document.activeElement;
  if (active && active !== document.body && active !== document.documentElement && active !== target) {
    clearDeviceFocus();
    return;
  }
  clearDeviceFocus();
  target.focus({ preventScroll: true });
}

function focusDeviceStatus() {
  const active = document.activeElement;
  if (active && active !== document.body && active !== document.documentElement && !deviceEls.list.dataset.focusDevice) {
    return;
  }
  clearDeviceFocus();
  deviceEls.notice.focus({ preventScroll: true });
}

function renderDevices(state) {
  const view = selectDevices(state);
  if (view.pendingCode && document.activeElement === deviceEls.create) {
    deviceEls.create.dataset.focusRecoveryRequest = String(view.codeRequest);
  }
  renderOptions(deviceEls.lifetime, view.lifetimeOptions, view.selectedLifetime);
  setProp(deviceEls.lifetime, "disabled", view.pendingCode);
  setProp(deviceEls.create, "disabled", view.pendingCode || view.loading);
  setProp(deviceEls.create, "textContent", view.pendingCode ? t("Đang tạo mã…") : t("Tạo mã ghép"));
  setProp(deviceEls.notice, "textContent", tr(view.notice));
  setProp(deviceEls.error, "hidden", !view.error);
  setProp(deviceEls.errorMessage, "textContent", view.error ? tr(view.error.text) : "");
  setProp(deviceEls.codeOutput, "hidden", !view.code);
  setProp(deviceEls.code, "value", view.code || "");
  setProp(deviceEls.codeExpiry, "textContent", tr(view.codeExpiry));
  setProp(deviceEls.codeLifetime, "textContent", tr(view.codeLifetime));
  setProp(deviceEls.copy, "disabled", !view.code);
  setProp(deviceEls.copyStatus, "textContent", view.copy ? tr(view.copy.text) : "");
  setAttr(deviceEls.copyStatus, "data-state", view.copy?.state || "");

  const endpointSignature = JSON.stringify(view.serverUrls);
  if (deviceEls.endpoints.dataset.urls !== endpointSignature) {
    deviceEls.endpoints.dataset.urls = endpointSignature;
    deviceEls.endpoints.replaceChildren(...view.serverUrls.map((url) => {
      const item = document.createElement("li");
      const endpoint = document.createElement("code");
      endpoint.textContent = url;
      item.append(endpoint);
      return item;
    }));
  }
  setProp(deviceEls.endpointsEmpty, "hidden", view.serverUrls.length > 0 || view.loading);
  setProp(deviceEls.listStatus, "textContent", view.loading ? t("Đang tải danh sách thiết bị…") : "");
  setProp(deviceEls.listEmpty, "hidden", !view.empty);
  setProp(deviceEls.listEmpty, "textContent", tr(view.emptyText));
  setProp(deviceEls.showRevoked, "checked", view.showRevoked);
  renderDeviceRows(view);

  if (view.ui === "focus-code" || view.ui === "select-code") {
    clearDeviceFocus();
    deviceEls.code.focus();
    if (view.ui === "select-code") deviceEls.code.select();
    dispatch({ type: "extension/devices-ui-handled", ui: view.ui });
  } else if (view.ui === "focus-retry" || view.ui === "focus-revoke") {
    focusDeviceRecovery(view);
    dispatch({ type: "extension/devices-ui-handled", ui: view.ui });
  } else if (view.ui === "focus-revoke-status") {
    focusDeviceStatus();
    dispatch({ type: "extension/devices-ui-handled", ui: view.ui });
  }
}

function processingCatalogs(state) {
  return {
    asrModels: state.asr.models,
    providers: {
      chatgpt: {
        status: state.chatgpt.info?.status,
        account: state.chatgpt.info?.account?.label || null,
        models: state.chatgpt.models,
        effortOptions: selectView(CHATGPT, state.chatgpt).effortOptions || [],
      },
      antigravity: {
        status: state.antigravity.info?.status,
        account: state.antigravity.info?.account?.label || null,
        models: state.antigravity.models,
        effortOptions: selectView(ANTIGRAVITY, state.antigravity).effortOptions || [],
      },
    },
  };
}

function renderProcessingPreferences(state, rootState) {
  const view = selectPreferences(state, processingCatalogs(rootState));
  const draft = view.draft;
  const form = processingPreferenceEls.form;
  if (view.saving && document.activeElement === processingPreferenceEls.save) {
    form.dataset.focusMutation = String(state.mutation);
  }
  setProp(processingPreferenceEls.version, "textContent", view.version ? `v${view.version}` : "");
  setAttr(processingPreferenceEls.version, "data-state", view.status === "error" ? "error" : view.status);
  if (!draft) {
    setAttr(processingPreferenceEls.form, "aria-busy", view.loading ? "true" : "false");
    setProp(processingPreferenceEls.notice, "textContent", view.message ? tr(view.message) : "");
    setProp(processingPreferenceEls.error, "hidden", !view.error);
    setProp(processingPreferenceEls.error, "textContent", view.error ? tr(view.error.text) : "");
    setProp(processingPreferenceEls.save, "disabled", true);
    return;
  }
  renderOptions(processingPreferenceEls.asrChoice, view.asrOptions, draft.asr.choice);
  setProp(processingPreferenceEls.asrLanguage, "value", draft.asr.language);
  renderOptions(processingPreferenceEls.provider, view.providerOptions, view.providerValue);
  renderOptions(processingPreferenceEls.model, view.modelOptions, view.modelValue);
  renderOptions(processingPreferenceEls.effort, view.effortOptions, view.effortValue);
  setProp(processingPreferenceEls.outputLanguage, "value", draft.notes.output_language);
  setProp(processingPreferenceEls.modelField, "hidden", view.waiting);
  setProp(processingPreferenceEls.effortField, "hidden", view.waiting);
  setProp(processingPreferenceEls.providerHelp, "textContent", tr(view.providerStatus));
  const disabled = view.loading || view.saving;
  for (const control of [processingPreferenceEls.asrChoice, processingPreferenceEls.asrLanguage, processingPreferenceEls.provider, processingPreferenceEls.model, processingPreferenceEls.effort, processingPreferenceEls.outputLanguage]) {
    setProp(control, "disabled", disabled || (view.waiting && (control === processingPreferenceEls.model || control === processingPreferenceEls.effort)));
  }
  setProp(processingPreferenceEls.save, "disabled", !view.canSave);
  setProp(processingPreferenceEls.save, "textContent", view.saving ? t("Đang lưu cài đặt…") : t("Lưu cài đặt xử lý"));
  setAttr(processingPreferenceEls.form, "aria-busy", view.saving || view.loading ? "true" : "false");
  setProp(processingPreferenceEls.notice, "textContent", view.notice ? tr(view.notice) : "");
  setProp(processingPreferenceEls.error, "hidden", !view.error);
  setProp(processingPreferenceEls.error, "textContent", view.error ? tr(view.error.text) : "");
  if (!view.saving && form.dataset.focusMutation) {
    const matches = form.dataset.focusMutation === String(state.mutation);
    delete form.dataset.focusMutation;
    const active = document.activeElement;
    if (matches && state.open && form.getClientRects().length
      && (!active || active === document.body || active === processingPreferenceEls.save)) {
      const target = view.error ? processingPreferenceEls.save : processingPreferenceEls.title;
      target.focus({ preventScroll: true });
    }
  }
}

// The focus went with the button that was replaced (Cài đặt model, Hủy, Thử lại). It goes to the button that
// took its place as soon as one can take it, unless the user has moved on by then.
// The row id waits in the list's own dataset: a view keeps no module state (ADR 0010).
function restoreAsrFocus() {
  const row = asrEls.list.dataset.focusRow;
  if (!row) return;
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected) {
    asrEls.list.dataset.focusRow = "";
    return;
  }
  const button = asrEls.list.querySelector(`[data-model="${row}"] button[data-action]:not(:disabled)`);
  if (button) {
    button.focus();
    asrEls.list.dataset.focusRow = "";
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
  if (row.install) item.append(asrInstall(row));
  if (row.command) item.append(asrCommand(row));
  return item;
}

function asrLine(className, text, role) {
  const line = document.createElement("p");
  line.className = className;
  line.textContent = tr(text);
  if (role) line.setAttribute("role", role);
  return line;
}

function asrButton(id, action, label, kind, disabled = false) {
  const button = document.createElement("button");
  button.className = kind;
  button.type = "button";
  button.dataset.action = action;
  // Hủy stays focusable while it waits: a disabled button would drop the keyboard focus to the page.
  if (action === "cancel") button.setAttribute("aria-disabled", String(disabled));
  else button.disabled = disabled;
  button.textContent = label;
  button.addEventListener("click", () => dispatch({ type: `provider/asr-${action}-requested`, id }));
  return button;
}

// The install offer, the running job with its bar and Hủy, or the failure with Thử lại.
function asrInstall({ install, progress, id }) {
  const wrap = document.createElement("div");
  wrap.className = "asr-install";
  if (install.kind === "progress") {
    wrap.append(asrLine("asr-install-phase", install.phase, "status"));
    const bar = document.createElement("progress");
    bar.className = "task-progress-bar asr-progress";
    bar.max = 1;
    if (progress.ratio !== null) bar.value = progress.ratio;
    bar.setAttribute("aria-label", tr(install.phase));
    const detail = asrLine("asr-progress-detail", progress.detail);
    wrap.append(bar, detail, asrButton(id, "cancel", t("Hủy"), "secondary-button", install.cancelDisabled));
  } else {
    if (install.kind === "failed") wrap.append(asrLine("asr-install-error", install.message, "alert"));
    if (install.kind === "offer") wrap.append(asrLine(install.blocked ? "asr-install-error" : "asr-install-space", install.blocked ? install.blockedMessage : install.spaceLine));
    wrap.append(asrButton(id, "install", install.kind === "failed" ? t("Thử lại") : t("Cài đặt model"), "primary-button", install.blocked));
  }
  if (install.refusal) wrap.append(asrLine("asr-install-error", install.refusal, "alert"));
  return wrap;
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
  const input = document.createElement("textarea");
  input.id = label.htmlFor;
  input.rows = 1;
  input.readOnly = true;
  input.spellcheck = false;
  input.value = row.command;
  const button = document.createElement("button");
  button.className = "secondary-button";
  button.type = "button";
  button.textContent = t("Sao chép");
  const result = document.createElement("span");
  result.className = "asr-command-result";
  result.setAttribute("role", "status");
  button.addEventListener("click", () => dispatch({ type: "provider/asr-copy-requested", id: row.id }));
  const text = document.createElement("div");
  text.className = "asr-command-text";
  text.dataset.value = row.command;
  text.append(input);
  field.append(text, button);
  wrap.append(label, field, result);
  return wrap;
}

function renderSettingsSection(section) {
  const current = SETTINGS_SECTIONS.includes(section) && section !== "local" ? section : "ai";
  const mobile = phoneLayout.matches;
  const selectedTab = mobile ? settingsParent(current) : current;
  const activePanel = current === "processing"
    ? "settings-panel-processing"
    : settingsParent(current) === "ai"
      ? "settings-panel-ai"
      : `settings-panel-${current}`;
  const visibleTabs = mobile ? SETTINGS_TOP_LEVELS : SETTINGS_SECTIONS;
  for (const tab of settingsTabs.querySelectorAll("[data-settings-tab]")) {
    const id = tab.dataset.settingsTab;
    const visible = visibleTabs.includes(id);
    const selected = visible && id === selectedTab;
    setProp(tab, "hidden", !visible);
    setAttr(tab, "aria-hidden", String(!visible));
    setAttr(tab, "aria-selected", String(selected));
    setProp(tab, "tabIndex", selected ? 0 : -1);
    const controls = selected && mobile ? activePanel : id === "processing" ? "settings-panel-processing" : settingsParent(id) === "ai" ? "settings-panel-ai" : `settings-panel-${id}`;
    setAttr(tab, "aria-controls", controls);
  }
  for (const panel of settingsBody.querySelectorAll(":scope > .settings-panel")) setProp(panel, "hidden", panel.id !== activePanel);
  const activePanelElement = document.getElementById(activePanel);
  if (activePanelElement) setAttr(activePanelElement, "aria-labelledby", `settings-tab-${selectedTab}`);
  setProp(overviewEls.panel, "hidden", current !== "ai");
  setProp(providerPages.openai, "hidden", current !== "openai");
  setProp(providerPages.antigravity, "hidden", current !== "antigravity");
}

function render(state) {
  renderSettingsSection(state.launcher.section);
  renderOverview(state);
  renderAsr(state.asr);
  renderDevices(state.devices);
  renderProcessingPreferences(state.processingPreferences, state);
  const s = state.chatgpt;
  const view = renderSection(els, CHATGPT, s);
  setProp(els.verificationSection, "hidden", view.modelSectionHidden);
  renderLauncher(CHATGPT, s);
  renderLauncher(ANTIGRAVITY, state.antigravity);
  if (s.loginCopy) setProp(els.pendingCopy, "textContent", tr(s.loginCopy));
  setProp(els.pendingActions, "hidden", view.pendingHidden);
  setProp(els.cancelSignIn, "disabled", view.cancelDisabled);
  if (s.ui) {
    if (s.ui.startsWith("clear-callback-input")) els.callbackInput.value = "";
    if (s.ui === "focus:registration-name") els.registrationName.value = s.recreateName;
    const focus = {
      "focus:sign-in": els.signIn,
      "focus:disconnect-confirm": els.disconnectConfirmButton,
      "focus:disconnect-trigger": els.disconnectTrigger,
      "focus:callback-input": els.callbackInput,
      "focus:registration-name": els.registrationName,
      "focus:registration-trigger": els.registrationTrigger,
      "clear-callback-input:focus-message": els.message,
    }[s.ui];
    if (focus) focus.focus();
    dispatch({ type: "provider/ui-handled", provider: CHATGPT.id });
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
    "asr.settings-changed": () => asrSettings.dispatch({ type: "channel/asr-settings-changed" }),
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
    "extension.devices.write": (code) => {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      return navigator.clipboard.writeText(code);
    },
    // The one-time code is the only device value that may be selected in the UI.
    "extension.devices.select-code": () => {
      deviceEls.code.focus();
      deviceEls.code.select();
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

// The gear opens the overview; a provider button opens that provider's sub-item.
function focusSettingsDestination(section) {
  const heading = section === "openai"
    ? document.querySelector("#connection-section-title")
    : section === "antigravity"
      ? document.querySelector("#antigravity-title")
      : section === "processing"
        ? document.querySelector("#automatic-preferences-title")
        : null;
  if (heading) {
    heading.focus({ preventScroll: true });
    return;
  }
  settingsTabs.querySelector('[aria-selected="true"]')?.focus();
}

function openDialog(launcher, section) {
  if (!els.dialog.open) els.dialog.showModal();
  dispatch({ type: "provider/dialog-opened", launcher, section });
  settingsBody.scrollTop = 0; // a closed dialog keeps its scroll
  const active = store.getState().launcher.section;
  focusSettingsDestination(active);
}
settingsButton.addEventListener("click", () => openDialog(null));
for (const provider of [CHATGPT.id, ANTIGRAVITY.id]) {
  launcherElements[provider].addEventListener("click", () => openDialog(provider));
}

function selectSection(section, focus = false) {
  dispatch({ type: "provider/settings-section-selected", section });
  settingsBody.scrollTop = 0;
  if (focus) focusSettingsDestination(store.getState().launcher.section);
}
settingsTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-settings-tab]");
  if (tab) selectSection(tab.dataset.settingsTab);
});
settingsBody.addEventListener("click", (event) => {
  const target = event.target.closest("[data-settings-tab]");
  if (!target || target.closest("#settings-tabs")) return;
  if (target.getAttribute("aria-disabled") === "true" || target.disabled) return;
  if (target.tagName === "A") event.preventDefault();
  selectSection(target.dataset.settingsTab, true);
});
// A column beside the content, a row on a phone: say which, as the CSS breakpoint does.
const announceOrientation = () => settingsTabs.setAttribute("aria-orientation", phoneLayout.matches ? "horizontal" : "vertical");
const lastSettingsFocus = (() => {
  let element = null;
  els.dialog.addEventListener("focusin", (event) => { element = event.target; });
  els.dialog.addEventListener("close", () => { element = null; });
  return () => element;
})();
phoneLayout.addEventListener("change", () => {
  const activeElement = document.activeElement;
  const focusedElement = els.dialog.contains(activeElement)
    ? activeElement
    : activeElement === document.body ? lastSettingsFocus() : null;
  renderSettingsSection(store.getState().launcher.section);
  announceOrientation();
  const focusIsRendered = focusedElement?.isConnected
    && focusedElement.getClientRects().length > 0
    && getComputedStyle(focusedElement).visibility !== "hidden";
  if (focusedElement && !focusIsRendered) settingsTabs.querySelector('[role="tab"][aria-selected="true"]')?.focus();
});
announceOrientation();

// Arrow keys, Home and End move between the sections and choose the one they land on, as the meeting tabs do.
// The list is a column beside the content and a row on a phone, so both axes move.
settingsTabs.addEventListener("keydown", (event) => {
  const tab = event.target.closest("[data-settings-tab]");
  if (!tab || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const sections = (phoneLayout.matches ? SETTINGS_TOP_LEVELS : SETTINGS_SECTIONS).filter((id) => id !== "local");
  const index = sections.indexOf(tab.dataset.settingsTab);
  if (index < 0) return;
  const last = sections.length - 1;
  const previous = index === 0 ? last : index - 1;
  const next = index === last ? 0 : index + 1;
  const target = { ArrowUp: previous, ArrowLeft: previous, ArrowDown: next, ArrowRight: next, Home: 0, End: last }[event.key];
  if (target === undefined) return;
  event.preventDefault();
  selectSection(sections[target]);
  document.getElementById(`settings-tab-${sections[target]}`).focus();
});

els.close.addEventListener("click", () => els.dialog.close());
// Esc, the close button and a click outside all end here: stop reading the install state.
els.dialog.addEventListener("close", () => dispatch({ type: "provider/dialog-closed" }));
els.dialog.addEventListener("click", (event) => {
  if (event.target === els.dialog) els.dialog.close();
});
// A pending save may restore lost button focus only while the user stays here.
function forgetPreferenceFocusOnMovement(event) {
  if (event.target !== processingPreferenceEls.save) {
    delete processingPreferenceEls.form.dataset.focusMutation;
  }
}
document.addEventListener("focusin", forgetPreferenceFocusOnMovement);
document.addEventListener("pointerdown", forgetPreferenceFocusOnMovement, true);
document.addEventListener("focusin", forgetDeviceFocusOnMovement);
document.addEventListener("pointerdown", forgetDeviceFocusOnMovement, true);

asrEls.select.addEventListener("change", () => dispatch({ type: "provider/asr-default-chosen", value: asrEls.select.value }));
processingPreferenceEls.form.addEventListener("submit", (event) => {
  event.preventDefault();
  dispatch({ type: "provider/processing-preferences-save-requested" });
});
processingPreferenceEls.asrChoice.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "asr_choice", value: processingPreferenceEls.asrChoice.value }));
processingPreferenceEls.asrLanguage.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "asr_language", value: processingPreferenceEls.asrLanguage.value }));
processingPreferenceEls.provider.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "notes_provider", value: processingPreferenceEls.provider.value || null }));
processingPreferenceEls.model.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "notes_model", value: processingPreferenceEls.model.value || null }));
processingPreferenceEls.effort.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "notes_reasoning_effort", value: processingPreferenceEls.effort.value || null }));
processingPreferenceEls.outputLanguage.addEventListener("change", () => dispatch({ type: "provider/processing-preference-edited", field: "notes_output_language", value: processingPreferenceEls.outputLanguage.value }));
deviceEls.form.addEventListener("submit", (event) => {
  event.preventDefault();
  dispatch({ type: "extension/devices-code-requested" });
});
deviceEls.lifetime.addEventListener("change", () => dispatch({ type: "extension/devices-lifetime-chosen", value: deviceEls.lifetime.value }));
deviceEls.showRevoked.addEventListener("change", () => dispatch({ type: "extension/devices-revoked-visibility-changed", value: deviceEls.showRevoked.checked }));
deviceEls.copy.addEventListener("click", () => dispatch({ type: "extension/devices-copy-requested" }));
deviceEls.list.addEventListener("click", (event) => {
  const action = event.target.closest("[data-device-action]");
  if (!action) return;
  const deviceId = action.dataset.deviceId;
  const actionType = {
    repair: "extension/devices-repair-requested",
    revoke: "extension/devices-revoke-requested",
    "revoke-cancel": "extension/devices-revoke-cancelled",
    "revoke-confirm": "extension/devices-revoke-confirmed",
  }[action.dataset.deviceAction];
  if (actionType && deviceId) dispatch({ type: actionType, deviceId });
});

const chatgpt = (action) => dispatch({ ...action, provider: CHATGPT.id });
els.signIn.addEventListener("click", () => chatgpt({ type: "provider/login-requested" }));
els.cancelSignIn.addEventListener("click", () => chatgpt({ type: "provider/login-cancel-requested" }));
els.callbackForm.addEventListener("submit", (event) => {
  event.preventDefault();
  chatgpt({ type: "provider/login-callback-submitted", value: els.callbackInput.value });
});
els.registrationTrigger.addEventListener("click", () => chatgpt({ type: "provider/registration-recreate-confirm-toggled", open: true }));
els.registrationKeep.addEventListener("click", () => chatgpt({ type: "provider/registration-recreate-confirm-toggled", open: false }));
els.registrationConfirmButton.addEventListener("click", () => chatgpt({ type: "provider/registration-recreate-requested", displayName: els.registrationName.value }));
els.retryPoll.addEventListener("click", () => chatgpt({ type: "provider/poll-retry-requested" }));
els.relogin.addEventListener("click", () => chatgpt({ type: "provider/login-requested" }));
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

// A language change in Settings reloads the page; Settings comes back where it was.
const reopenAt = readStored(SETTINGS_SECTION_KEY);
if (SETTINGS_SECTIONS.includes(reopenAt)) {
  settingsButton.focus(); // closing the dialog returns focus to what had it when it opened
  openDialog(null, reopenAt);
  if (reopenAt === "theme") els.dialog.querySelector("[data-lang-choice]:checked")?.focus();
}
