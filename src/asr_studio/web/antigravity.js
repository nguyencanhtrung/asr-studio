// View of the Google Antigravity section of the connections dialog. It renders the second provider of the
// store created in connections.js; the flows are the same provider-core.js reducer with the Antigravity descriptor.
import { dispatch, readStored, renderSection, store } from "./connections.js";
import { setAttr, setProp, tr } from "./shell.js";
import { ANTIGRAVITY } from "./provider-core.js";

const els = {
  badge: document.querySelector("#antigravity-badge"),
  status: document.querySelector("#antigravity-status"),
  account: document.querySelector("#antigravity-account"),
  verificationRow: document.querySelector("#antigravity-verification-row"),
  verificationSummary: document.querySelector("#antigravity-verification-summary"),
  message: document.querySelector("#antigravity-message"),
  error: document.querySelector("#antigravity-error"),
  errorMessage: document.querySelector("#antigravity-error-message"),
  mutationReason: document.querySelector("#antigravity-mutation-reason"),
  signIn: document.querySelector("#antigravity-sign-in"),
  login: document.querySelector("#antigravity-login"),
  loginCopy: document.querySelector("#antigravity-login-copy"),
  loginLink: document.querySelector("#antigravity-login-link"),
  inputForm: document.querySelector("#antigravity-input-form"),
  inputLabel: document.querySelector("#antigravity-input-label"),
  input: document.querySelector("#antigravity-input"),
  cancel: document.querySelector("#antigravity-cancel"),
  disconnect: document.querySelector("#antigravity-disconnect-wrap"),
  disconnectTrigger: document.querySelector("#antigravity-disconnect"),
  disconnectConfirm: document.querySelector("#antigravity-disconnect-confirm"),
  disconnectKeep: document.querySelector("#antigravity-disconnect-keep"),
  disconnectConfirmButton: document.querySelector("#antigravity-disconnect-confirm-button"),
  modelSection: document.querySelector("#antigravity-model-section"),
  modelForm: document.querySelector("#antigravity-model-form"),
  modelSelect: document.querySelector("#antigravity-model"),
  effortSelect: document.querySelector("#antigravity-effort"),
  modelStatus: document.querySelector("#antigravity-model-status"),
  modelSave: document.querySelector("#antigravity-model-save"),
  verificationSection: document.querySelector("#antigravity-verification-section"),
  ledger: document.querySelector("#antigravity-ledger"),
  verificationResult: document.querySelector("#antigravity-verification-result"),
  verify: document.querySelector("#antigravity-verify"),
};

function render(state) {
  const s = state.antigravity;
  const view = renderSection(els, ANTIGRAVITY, s);
  setProp(els.verificationSection, "hidden", view.modelSectionHidden);
  setProp(els.login, "hidden", view.loginHidden);
  if (s.loginCopy) setProp(els.loginCopy, "textContent", tr(s.loginCopy));
  setProp(els.loginLink, "hidden", !s.loginLink);
  if (s.loginLink) setAttr(els.loginLink, "href", s.loginLink);
  setProp(els.inputForm, "hidden", !s.inputOpen);
  if (s.inputPrompt) setProp(els.inputLabel, "textContent", s.inputPrompt);
  if (s.ui) {
    const focus = {
      "focus:sign-in": els.signIn,
      "focus:disconnect-confirm": els.disconnectConfirmButton,
      "focus:disconnect-trigger": els.disconnectTrigger,
    }[s.ui];
    if (focus) focus.focus();
    if (s.ui === "clear-input") els.input.value = "";
    dispatch({ type: "provider/ui-handled", provider: ANTIGRAVITY.id });
  }
}
store.subscribe(render);

const antigravity = (action) => dispatch({ ...action, provider: ANTIGRAVITY.id });
els.signIn.addEventListener("click", () => antigravity({ type: "provider/login-requested" }));
els.cancel.addEventListener("click", () => antigravity({ type: "provider/login-cancel-requested" }));
els.inputForm.addEventListener("submit", (event) => {
  event.preventDefault();
  antigravity({ type: "provider/login-input-submitted", value: els.input.value });
});
els.modelSelect.addEventListener("change", () => antigravity({ type: "provider/choice-edited", field: "model", value: els.modelSelect.value }));
els.effortSelect.addEventListener("change", () => antigravity({ type: "provider/choice-edited", field: "effort", value: els.effortSelect.value }));
els.modelForm.addEventListener("submit", (event) => {
  event.preventDefault();
  antigravity({ type: "provider/save-requested" });
});
els.verify.addEventListener("click", () => antigravity({ type: "provider/verify-requested" }));
els.disconnectTrigger.addEventListener("click", () => antigravity({ type: "provider/disconnect-confirm-toggled", open: true }));
els.disconnectKeep.addEventListener("click", () => antigravity({ type: "provider/disconnect-confirm-toggled", open: false }));
els.disconnectConfirmButton.addEventListener("click", () => antigravity({ type: "provider/disconnect-requested" }));

antigravity({ type: "provider/boot-restored", raw: readStored(ANTIGRAVITY.storageKey), now: Date.now() });
