// View of the notes tab: reads store state, renders the DOM, dispatches actions.
// All state and rules live in notes-core.js (contract: docs/adr/0010-frontend-reducer-core.md).
// Commands to history.js leave as `call` effects (the registry below); what it announces (the channel's
// meeting context, the provider choice, popstate, ...) comes in as actions.
import { meetingChanges, meetingContext as meetingChannel, providerChoice } from "./channel.js";
import { meetingHistory } from "./history.js";
import { setAttr, setProp, tr } from "./shell.js";
import { createRunner, createStore } from "./store.js";
import {
  VIEW_MODE_KEY,
  blockRuns,
  currentGeneration,
  hasUnsavedDraft,
  initialState,
  outputLanguageStorageKey,
  pendingRequestStorageKey,
  reduce,
  requestStorageKey,
  selectView,
  selectedProvider,
  sourceReference,
  speakerReminder,
  warningLine,
  STEP_STATE_LABELS,
} from "./notes-core.js";

const $ = (selector) => document.querySelector(selector);
const els = {
  empty: $("#notes-empty"),
  emptyCopy: $("#notes-empty-copy"),
  create: $("#notes-create"),
  heading: $("#notes-create .notes-heading-row"),
  run: $("#notes-run"),
  status: $("#notes-status"),
  start: $("#notes-start"),
  reviewCorrection: $("#notes-review-correction"),
  cancel: $("#notes-cancel"),
  notesTab: $("#meeting-tab-notes"),
  regenerateDialog: $("#notes-regenerate-dialog"),
  regenerateTitle: $("#notes-regenerate-title"),
  regenerateProvider: $("#notes-regenerate-provider"),
  regenerateModel: $("#notes-regenerate-model"),
  regenerateEffort: $("#notes-regenerate-effort"),
  regenerateSource: $("#notes-regenerate-source"),
  regenerateSourceAdopt: $("#notes-regenerate-source-adopt"),
  regenerateLanguage: $("#notes-regenerate-language"),
  regenerateStatus: $("#notes-regenerate-status"),
  regenerateError: $("#notes-regenerate-error"),
  regenerateReload: $("#notes-regenerate-reload"),
  regenerateSubmit: $("#notes-regenerate-submit"),
  regenerateCancel: $("#notes-regenerate-cancel"),
  change: $("#notes-change"),
  toolbar: $("#notes-toolbar"),
  language: $("#notes-output-language"),
  provider: $("#notes-provider"),
  model: $("#notes-model"),
  plan: $("#notes-run-plan"),
  reload: $("#notes-choice-reload"),
  choice: $("#notes-choice"),
  preferenceStatus: $("#notes-preference-status"),
  policy: $("#notes-content-policy"),
  reminder: $("#notes-speaker-reminder"),
  reminderNames: $("#notes-speaker-names"),
  nameSpeakers: $("#notes-name-speakers"),
  openTranscript: $("#notes-empty-open-transcript"),
  details: $("#notes-task-details"),
  steps: $("#notes-steps"),
  elapsed: $("#notes-task-elapsed"),
  providerModel: $("#notes-task-provider-model"),
  progress: $("#notes-task-progress"),
  error: $("#notes-error"),
  errorMessage: $("#notes-error-message"),
  savedWork: $("#notes-saved-work"),
  selector: $("#notes-revision-selector"),
  history: $("#notes-history"),
  historyOpen: $("#notes-history-open"),
  historySelector: $("#notes-history-selector"),
  historyLink: $("#notes-history-copy-link"),
  historyClose: $("#notes-history-close"),
  historyStatus: $("#notes-history-status"),
  historyLegacy: $("#notes-history-legacy"),
  save: $("#notes-save"),
  editCancel: $("#notes-edit-cancel"),
  copyRich: $("#notes-copy-rich"),
  copyEmail: $("#notes-copy-email"),
  copyDialog: $("#notes-copy-dialog"),
  copyTitle: $("#notes-copy-title"),
  copyPin: $("#notes-copy-pin"),
  copySubject: $("#notes-copy-subject"),
  copyDocument: $("#notes-copy-document"),
  copyConfirm: $("#notes-copy-confirm"),
  copyClose: $("#notes-copy-close"),
  copyResult: $("#notes-copy-result"),
  copy: $("#notes-copy"),
  export: $("#notes-export"),
  draftStatus: $("#notes-draft-status"),
  copyStatus: $("#notes-copy-export-status"),
  conflict: $("#notes-conflict"),
  conflictMessage: $("#notes-conflict-message"),
  keepDraft: $("#notes-keep-draft"),
  useLatest: $("#notes-use-latest"),
  revisionMeta: $("#notes-revision-meta"),
  modeControls: $("#notes-mode-controls"),
  modeView: $("#notes-mode-view"),
  modeWrite: $("#notes-mode-write"),
  modeRich: $("#notes-mode-rich"),
  unsavedDot: $("#notes-unsaved-dot"),
  linkWarning: $("#notes-link-warning"),
  viewDraft: $("#notes-view-draft"),
  preview: $("#notes-preview"),
  rich: $("#notes-rich-editor"),
  richFallback: $("#notes-rich-fallback"),
  markdown: $("#notes-markdown"),
  sourceView: $("#notes-source-view"),
  sourceHeading: $("#notes-source-heading"),
  sourceContext: $("#notes-source-context"),
  sourceTime: $("#notes-source-time"),
  sourceSpeaker: $("#notes-source-speaker"),
  sourceDate: $("#notes-source-date"),
  sourceText: $("#notes-source-text"),
  sourceDifference: $("#notes-source-difference"),
  sourceSeek: $("#notes-source-seek"),
  sourceStatus: $("#notes-source-status"),
  sourceClose: $("#notes-source-close"),
};

let trashEls;

function makeTrashButton(id, className) {
  const button = document.createElement("button");
  button.id = id;
  button.className = className;
  button.type = "button";
  return button;
}

function mountTrashControls() {
  if (trashEls) return;

  const lifecycleNotice = document.createElement("p");
  lifecycleNotice.id = "notes-lifecycle-notice";
  lifecycleNotice.className = "notes-lifecycle-notice";
  lifecycleNotice.hidden = true;
  lifecycleNotice.setAttribute("role", "status");
  lifecycleNotice.setAttribute("aria-live", "polite");
  els.preview.parentElement.insertBefore(lifecycleNotice, els.preview);

  const root = document.createElement("section");
  root.id = "notes-trash";
  root.className = "notes-trash";
  root.hidden = true;
  root.setAttribute("aria-labelledby", "notes-trash-title");

  const title = document.createElement("h4");
  title.id = "notes-trash-title";
  title.className = "notes-trash-title";
  root.append(title);

  const scope = document.createElement("p");
  scope.id = "notes-trash-scope";
  scope.className = "notes-trash-scope";
  root.append(scope);

  const versionStatus = document.createElement("p");
  versionStatus.id = "notes-trash-version-status";
  versionStatus.className = "notes-trash-version-status";
  versionStatus.setAttribute("role", "status");
  root.append(versionStatus);

  const itemsTitle = document.createElement("h5");
  itemsTitle.id = "notes-trash-items-title";
  itemsTitle.className = "notes-trash-subtitle";
  root.append(itemsTitle);
  const items = document.createElement("ul");
  items.id = "notes-trash-items";
  items.className = "notes-trash-items";
  items.setAttribute("aria-labelledby", "notes-trash-items-title");
  root.append(items);

  const versionActions = document.createElement("div");
  versionActions.className = "notes-trash-actions";
  const move = makeTrashButton("notes-trash-move", "secondary-button");
  const restore = makeTrashButton("notes-trash-restore", "secondary-button");
  versionActions.append(move, restore);

  const retry = makeTrashButton("notes-trash-retry", "text-button");
  const reload = makeTrashButton("notes-trash-reload", "text-button");
  const empty = makeTrashButton("notes-trash-empty", "secondary-button");
  versionActions.append(retry, reload, empty);
  root.append(versionActions);

  const confirmation = document.createElement("div");
  confirmation.id = "notes-trash-confirmation";
  confirmation.className = "notes-trash-confirmation";
  confirmation.setAttribute("role", "group");
  confirmation.setAttribute("aria-labelledby", "notes-trash-confirmation-title");
  const confirmationTitle = document.createElement("h5");
  confirmationTitle.id = "notes-trash-confirmation-title";
  confirmation.append(confirmationTitle);
  const confirmationScope = document.createElement("p");
  confirmationScope.id = "notes-trash-confirmation-scope";
  confirmation.append(confirmationScope);
  const confirmationItemsTitle = document.createElement("p");
  confirmationItemsTitle.id = "notes-trash-confirmation-items-title";
  confirmationItemsTitle.className = "notes-trash-confirmation-items-title";
  confirmation.append(confirmationItemsTitle);
  const confirmationItems = document.createElement("ul");
  confirmationItems.id = "notes-trash-confirmation-items";
  confirmationItems.className = "notes-trash-items notes-trash-confirmation-items";
  confirmationItems.setAttribute("aria-labelledby", "notes-trash-confirmation-items-title");
  confirmation.append(confirmationItems);
  const confirmationLabel = document.createElement("label");
  const checkbox = document.createElement("input");
  checkbox.id = "notes-trash-confirmation-check";
  checkbox.type = "checkbox";
  const confirmationLabelText = document.createElement("span");
  confirmationLabel.append(checkbox, confirmationLabelText);
  confirmation.append(confirmationLabel);
  const confirmationActions = document.createElement("div");
  confirmationActions.className = "notes-trash-confirmation-actions";
  const confirm = makeTrashButton("notes-trash-confirm", "secondary-button");
  const cancel = makeTrashButton("notes-trash-cancel", "text-button");
  confirmationActions.append(confirm, cancel);
  confirmation.append(confirmationActions);
  const confirmationError = document.createElement("p");
  confirmationError.id = "notes-trash-confirmation-error";
  confirmationError.className = "notes-trash-confirmation-error";
  confirmationError.setAttribute("role", "alert");
  confirmation.append(confirmationError);
  root.append(confirmation);

  const status = document.createElement("p");
  status.id = "notes-trash-status";
  status.className = "notes-trash-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.tabIndex = -1;
  root.append(status);

  els.history.append(root);
  trashEls = {
    root,
    lifecycleNotice,
    title,
    scope,
    versionStatus,
    itemsTitle,
    items,
    move,
    restore,
    retry,
    reload,
    empty,
    confirmation,
    confirmationTitle,
    confirmationScope,
    confirmationItemsTitle,
    confirmationItems,
    confirmationLabel,
    confirmationLabelText,
    checkbox,
    confirm,
    cancel,
    confirmationError,
    status,
  };

  move.addEventListener("click", () => dispatch({ type: "notes/trash-version-requested" }));
  restore.addEventListener("click", () => {
    const action = selectView(store.getState()).trash.versionAction;
    dispatch({
      type: "notes/restore-version-requested",
      operationId: newRequestId(),
      noteId: action.noteId,
      editVersion: action.editVersion,
      transitionId: action.transitionId,
    });
  });
  items.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-trash-restore]");
    if (!button || !items.contains(button)) return;
    dispatch({
      type: "notes/restore-version-requested",
      operationId: newRequestId(),
      noteId: button.dataset.noteId,
      editVersion: Number(button.dataset.editVersion),
      transitionId: button.dataset.transitionId,
    });
  });
  retry.addEventListener("click", () => dispatch({ type: "notes/lifecycle-retry" }));
  reload.addEventListener("click", () => dispatch({ type: "notes/trash-read-requested" }));
  empty.addEventListener("click", () => dispatch({ type: "notes/empty-trash-requested" }));
  checkbox.addEventListener("change", (event) => dispatch({ type: "notes/empty-trash-checked", checked: event.currentTarget.checked }));
  confirm.addEventListener("click", () => {
    const current = selectView(store.getState()).trash.confirmation;
    dispatch({
      type: current?.kind === "trash-version" ? "notes/trash-version-confirmed" : "notes/empty-trash-confirmed",
      operationId: newRequestId(),
    });
  });
  cancel.addEventListener("click", () => dispatch({ type: "notes/trash-confirmation-cancelled" }));
}

function setData(element, name, value) {
  if (element.dataset[name] !== value) element.dataset[name] = value;
}

function trashTimestamp(value) {
  if (typeof value !== "string" || !value) return "";
  return value.includes(" ") ? `${value.replace(" ", "T")}Z` : value;
}

function renderTrashItems(container, items, { restore = false, busy = false } = {}) {
  const rows = Array.isArray(items) ? items : [];
  const key = rows.map((item) => [item.note_id, item.edit_version, item.transition_id, item.fixedHref, restore].join("\u0001")).join("\u0000");
  if (container.dataset.key === key) {
    for (const button of container.querySelectorAll("button[data-trash-restore]")) button.disabled = busy;
    return;
  }
  container.dataset.key = key;
  container.replaceChildren(...rows.map((item) => {
    const row = document.createElement("li");
    row.className = "notes-trash-item";
    row.dataset.noteId = item.note_id;
    row.dataset.editVersion = String(item.edit_version);

    const details = document.createElement("div");
    details.className = "notes-trash-item-details";
    const heading = document.createElement("strong");
    heading.className = "notes-trash-item-heading";
    heading.textContent = t("Notes revision {revision} · Save version {version}", {
      revision: item.note_id,
      version: item.edit_version,
    });
    details.append(heading);

    const saved = document.createElement("time");
    saved.className = "notes-trash-item-time";
    const savedAt = trashTimestamp(item.saved_at);
    saved.dateTime = savedAt;
    saved.textContent = `${t("Đã lưu:")} ${savedAt ? tr({ fmt: "timestamp", value: savedAt }) : "—"}`;
    details.append(saved);
    if (item.trashed_at) {
      const trashed = document.createElement("time");
      trashed.className = "notes-trash-item-time";
      const trashedAt = trashTimestamp(item.trashed_at);
      trashed.dateTime = trashedAt;
      trashed.textContent = `${t("Đã chuyển vào Thùng rác:")} ${tr({ fmt: "timestamp", value: trashedAt })}`;
      details.append(trashed);
    }

    const controls = document.createElement("div");
    controls.className = "notes-trash-item-controls";
    const link = document.createElement("a");
    link.className = "notes-trash-item-link";
    link.href = item.fixedHref;
    link.dataset.trashFixedLink = "true";
    link.textContent = t("Mở bản Save cố định");
    controls.append(link);
    if (restore) {
      const button = makeTrashButton("", "text-button");
      button.dataset.trashRestore = "true";
      button.dataset.noteId = item.note_id;
      button.dataset.editVersion = String(item.edit_version);
      button.dataset.transitionId = item.transition_id;
      button.textContent = t("Khôi phục Notes revision {revision}, Save version {version}", {
        revision: item.note_id,
        version: item.edit_version,
      });
      button.setAttribute("aria-label", button.textContent);
      button.disabled = busy;
      controls.append(button);
    }
    row.append(details, controls);
    return row;
  }));
}

// The remembered values enter the state through the context and boot actions; writes are storage effects.
function readStored(key, areas = ["local", "session"]) {
  for (const name of areas) {
    try {
      const value = window[`${name}Storage`].getItem(key);
      if (value) return value;
    } catch {
      // Try the next browser storage area.
    }
  }
  return "";
}

// ---- Markdown preview ----

function appendInline(parent, nodes, rich) {
  for (const node of nodes) {
    if (node.kind === "raw") {
      const literal = document.createElement("span");
      literal.className = "notes-inline-fallback";
      literal.textContent = node.text;
      parent.append(literal);
    } else if (!rich && node.kind === "text") {
      parent.append(document.createTextNode(node.text));
    } else if (node.kind === "invalid") {
      const invalid = document.createElement("span");
      invalid.className = "notes-link-invalid";
      invalid.textContent = node.text;
      const reason = document.createElement("span");
      reason.className = "visually-hidden";
      reason.textContent = tr(node.reason);
      invalid.append(reason);
      parent.append(invalid);
    } else if (node.kind === "source") {
      const link = document.createElement("a");
      link.href = node.href;
      link.dataset.notesSourceLink = "true";
      link.dataset.meetingId = node.meetingId;
      link.dataset.snapshotId = node.snapshotId;
      link.dataset.segmentId = node.segmentId;
      link.setAttribute("aria-label", tr(node.label));
      link.textContent = node.text;
      parent.append(link);
    } else {
      const tag = { strong: "strong", code: "code", em: "em", text: "span", link: rich ? "span" : "a" }[node.kind];
      const element = document.createElement(tag);
      element.textContent = node.text;
      if (node.kind === "link" && !rich) {
        element.href = node.href;
        element.target = "_blank";
        element.rel = "noopener noreferrer";
      }
      if (rich) {
        if (node.kind === "link") element.className = "notes-rich-link";
        rich.push(element);
      }
      parent.append(element);
    }
  }
}

// The chip is a link to the saved source, so a middle click or a copied address works as a click does.
function citationChip(chip) {
  const link = document.createElement("a");
  link.className = "notes-citation-chip";
  link.href = chip.href;
  link.dataset.notesSourceLink = "true";
  link.dataset.meetingId = chip.meetingId;
  link.dataset.snapshotId = chip.snapshotId;
  link.dataset.segmentId = chip.segmentId;
  link.setAttribute("aria-label", tr(chip.aria));
  link.textContent = tr(chip.label);
  return link;
}

// Leave the existing DOM in place while an editable run changes, so the browser
// keeps its caret and native text undo. The revision epoch owns the rendered runs.
function renderBlocks(root, blocks, { rich = false, disabled = false, selectionId = 0, clipboard = false } = {}) {
  const key = rich
    ? JSON.stringify([selectionId, blocks], function (name, value) {
      return Object.hasOwn(this, "end") && ["text", "start", "end"].includes(name) ? undefined : value;
    })
    : JSON.stringify(blocks);
  const shallowest = blocks.reduce((level, block) => block.kind === "heading" ? Math.min(level, block.level) : level, 6);
  const runs = rich ? [] : null;
  const addInline = (parent, nodes) => appendInline(parent, nodes, runs);
  if (root.dataset.blocksKey !== key) {
    setData(root, "blocksKey", key);
    const createBlock = (block) => {
      switch (block.kind) {
        case "heading": {
          const level = clipboard ? block.level : Math.min(6, block.level - shallowest + 4);
          const heading = document.createElement(`h${level}`);
          addInline(heading, block.nodes);
          return heading;
        }
        case "list": {
          const list = document.createElement(block.start === null ? "ul" : "ol");
          if (block.start !== null) list.start = block.start;
          for (const entry of block.items) {
            const item = document.createElement("li");
            addInline(item, entry.nodes);
            for (const chip of entry.chips) item.append(citationChip(chip));
            list.append(item);
          }
          return list;
        }
        case "table": {
          const wrap = document.createElement("div");
          wrap.className = "notes-table-wrap";
          const table = document.createElement("table");
          const head = table.createTHead();
          const body = table.createTBody();
          block.rows.forEach((cells, index) => {
            const row = (index === 0 ? head : body).insertRow();
            cells.forEach((nodes, column) => {
              const cell = document.createElement(index === 0 ? "th" : "td");
              if (index === 0) cell.scope = "col";
              cell.dataset.align = block.align[column];
              addInline(cell, nodes);
              row.append(cell);
            });
          });
          wrap.append(table);
          return wrap;
        }
        case "section": {
          const section = document.createElement("section");
          section.dataset.emailSection = block.section;
          section.setAttribute("aria-label", block.label);
          section.append(...block.blocks.map(createBlock));
          return section;
        }
        case "raw": {
          const fallback = document.createElement("pre");
          fallback.className = "notes-markdown-fallback";
          if (block.viewLines) {
            block.viewLines.forEach((line, index) => {
              if (index) fallback.append(document.createElement("br"));
              appendInline(fallback, line.nodes || line, false);
              line.chips?.forEach((chip) => fallback.append(citationChip(chip)));
            });
          } else {
            fallback.textContent = block.text;
          }
          return fallback;
        }
        default: {
          const paragraph = document.createElement("p");
          block.lines.forEach((nodes, index) => {
            if (index) paragraph.append(document.createElement("br"));
            addInline(paragraph, nodes);
          });
          return paragraph;
        }
      }
    };
    root.replaceChildren(...blocks.map(createBlock));
    runs?.forEach((element, index) => {
      element.dataset.richIndex = String(index);
      element.setAttribute("aria-label", t("Sửa đoạn {number}", { number: index + 1 }));
      element.setAttribute("role", "textbox");
      element.setAttribute("aria-multiline", "false");
      element.spellcheck = true;
    });
  }
  if (rich) {
    const nodes = blocks.flatMap(blockRuns);
    root.querySelectorAll("[data-rich-index]").forEach((element, index) => {
      const { text } = nodes[index];
      // While IME composition is active, the DOM owns the provisional text.
      if (!element.hasAttribute("data-composing") && element.textContent !== text) {
        setProp(element, "textContent", text);
      }
      setData(element, "richValue", text);
      setAttr(element, "contenteditable", disabled ? "false" : "plaintext-only");
    });
  }
}

// ---- render ----

// Saved Notes lead with the document. Active/error controls stay above it.
// Move the whole group so its choice, data policy and keyboard focus stay together.
function placeRunControls(inToolbar, urgent) {
  const host = inToolbar && !urgent ? els.savedWork : els.create;
  if (els.run.parentElement !== host) {
    const focused = els.run.contains(document.activeElement) ? document.activeElement : null;
    host.append(els.run);
    if (focused) focused.focus({ preventScroll: true });
  }
  els.start.classList.toggle("primary-button", !inToolbar);
  els.start.classList.toggle("secondary-button", inToolbar);
}

function renderChoice(view) {
  const choice = view.choice;
  setProp(els.provider, "value", choice.provider);
  setProp(els.provider, "disabled", choice.providerDisabled);
  if (els.model.dataset.optionsKey !== choice.modelKey) {
    els.model.replaceChildren(...choice.modelItems.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = tr(item.text);
      return option;
    }));
    setData(els.model, "optionsKey", choice.modelKey);
  }
  setProp(els.model, "value", choice.modelValue);
  setProp(els.model, "disabled", choice.modelDisabled);
  setProp(els.reload, "hidden", choice.reloadHidden);
  setProp(els.reload, "disabled", choice.reloadDisabled);
  setProp(els.plan, "textContent", tr(choice.plan).trim());
  setProp(els.preferenceStatus, "textContent", tr(choice.message));
  setProp(els.policy, "textContent", tr(choice.policy));
  setProp(els.plan, "hidden", choice.planHidden);
  setProp(els.choice, "hidden", choice.hidden);
  setAttr(els.change, "aria-expanded", String(choice.expanded));
  if (choice.focus > 0 && !choice.hidden && !choice.modelDisabled) {
    els.model.focus({ preventScroll: true });
    dispatch({ type: "notes/choice-focus-handled" });
  }
}

function renderSelect(select, items, value, disabled) {
  const key = JSON.stringify(items);
  if (select.dataset.optionsKey !== key) {
    select.replaceChildren(...items.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = tr(item.text);
      option.disabled = Boolean(item.disabled);
      return option;
    }));
    setData(select, "optionsKey", key);
  }
  setProp(select, "value", value);
  setProp(select, "disabled", Boolean(disabled));
}

function restoreRegenerateFocus() {
  // Native restoration finishes after close. Choose an enabled destination on
  // the next turn, after the accepted task has disabled the launcher.
  window.setTimeout(() => {
    if (els.regenerateDialog.open) return;
    const active = document.activeElement;
    if (active === els.reviewCorrection && !active.hidden && !active.disabled) return;
    if (active !== document.body && active !== els.start && active !== els.reviewCorrection
      && !els.regenerateDialog.contains(active)) return;
    const target = [els.start, els.cancel, els.notesTab].find((element) =>
      element?.isConnected && !element.disabled && element.getClientRects().length,
    );
    target?.focus({ preventScroll: true });
  }, 0);
}

function renderRegenerateDialog(view) {
  const dialog = els.regenerateDialog;
  const regenerate = view.regenerateDialog;
  if (!dialog || !regenerate) return;
  const focusedControl = document.activeElement;
  const opening = regenerate.open && dialog.dataset.openIntent !== "true";
  setData(dialog, "openIntent", String(regenerate.open));
  if (opening && !dialog.open) {
    dialog.showModal();
  } else if (!regenerate.open && dialog.open) {
    dialog.close();
    restoreRegenerateFocus();
  }
  renderSelect(dialog.querySelector("#notes-regenerate-provider"), regenerate.providerItems, regenerate.provider, regenerate.locked);
  renderSelect(dialog.querySelector("#notes-regenerate-model"), regenerate.modelItems, regenerate.model, regenerate.locked);
  renderSelect(dialog.querySelector("#notes-regenerate-effort"), regenerate.effortItems, regenerate.reasoningEffort, regenerate.locked);
  renderSelect(dialog.querySelector("#notes-regenerate-source"), regenerate.sourceItems, regenerate.generationId, regenerate.locked);
  setProp(els.regenerateLanguage, "value", regenerate.outputLanguage);
  setProp(els.regenerateLanguage, "disabled", regenerate.locked);
  setProp(els.regenerateStatus, "textContent", tr(regenerate.status));
  setProp(els.regenerateError, "textContent", tr(regenerate.error));
  setProp(els.regenerateError, "hidden", !regenerate.error);
  setProp(els.regenerateReload, "hidden", !regenerate.reloadVisible);
  setProp(els.regenerateReload, "disabled", regenerate.reloadDisabled);
  setProp(els.regenerateSourceAdopt, "hidden", !regenerate.sourceRefresh);
  setProp(els.regenerateSourceAdopt, "disabled", regenerate.locked);
  if (regenerate.sourceRefresh) setProp(els.regenerateSourceAdopt, "textContent", tr(regenerate.sourceRefreshLabel));
  setProp(els.regenerateSubmit, "textContent", tr(regenerate.submitLabel));
  setProp(els.regenerateSubmit, "disabled", regenerate.submitDisabled);
  setProp(els.regenerateCancel, "textContent", tr(regenerate.cancelLabel));
  setAttr(dialog, "aria-busy", String(regenerate.busy));
  if (dialog.open && (opening || dialog.dataset.focusToken !== String(regenerate.focus))) {
    setData(dialog, "focusToken", String(regenerate.focus));
    const target = !regenerate.locked ? els.regenerateProvider
      : !els.regenerateSubmit.disabled ? els.regenerateSubmit : els.regenerateCancel;
    target.focus({ preventScroll: true });
  } else if (dialog.open && dialog.contains(focusedControl) && focusedControl?.disabled) {
    // Submission disables the focused submit button; keep keyboard focus inside
    // the dialog on its available close action while the request is in flight.
    els.regenerateCancel.focus({ preventScroll: true });
  }
}

function renderTask(view) {
  const task = view.task;
  const summary = els.details.querySelector("summary");
  const completed = Boolean(task && task.steps.every((step) => step === "done"));
  const transferSummaryFocus = Boolean(task && !completed && document.activeElement === summary);
  setProp(els.details, "hidden", !task);
  // Polling must not close a disclosure the reader opened. Only a transition
  // into/out of successful completion changes its default presentation.
  const presentation = task ? (completed ? "completed" : "attention") : "";
  if (els.details.dataset.presentation !== presentation) {
    setProp(els.details, "open", Boolean(task && !completed));
    setData(els.details, "presentation", presentation);
  }
  // Active progress and failures are always exposed; only success is collapsible.
  setProp(summary, "hidden", Boolean(task && !completed));
  setProp(els.status, "textContent", tr(view.status));
  if (!task) {
    setProp(els.cancel, "hidden", true);
    return;
  }
  if (transferSummaryFocus || !els.details.contains(document.activeElement)) {
    const inPosition = completed
      ? els.savedWork.nextSibling === els.details
      : els.details.nextSibling === els.savedWork;
    if (!inPosition) {
      const anchor = completed ? els.savedWork.nextSibling : els.savedWork;
      els.savedWork.parentElement.insertBefore(els.details, anchor);
    }
  }
  const items = [...els.steps.children];
  const end = items.pop();
  items.forEach((item, index) => {
    const stepState = task.steps[index];
    setData(item, "stepState", stepState);
    setProp(item.querySelector(".notes-step-state"), "textContent", tr(STEP_STATE_LABELS[stepState]));
    if (stepState === "current") setAttr(item, "aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  setProp(end, "hidden", task.end.hidden);
  setData(end, "stepState", task.end.state);
  setProp(end.firstElementChild, "textContent", tr(task.end.label));
  setProp(els.elapsed, "textContent", tr(task.elapsed));
  setProp(els.providerModel, "textContent", task.providerModel.map(tr).join(" · "));
  setProp(els.progress, "textContent", tr(task.progress));
  setProp(els.cancel, "hidden", task.cancelHidden);
  setProp(els.cancel, "disabled", task.cancelDisabled);
  setProp(els.cancel, "textContent", tr(task.cancelLabel));
  // Focus only after the complete render has settled the generation controls.
  if (!transferSummaryFocus) return null;
  return task.cancelHidden || task.cancelDisabled ? els.status : els.cancel;
}

// The saved Markdown's source links that open no source, said in words above the notes.
function renderLinkWarning(warning) {
  setProp(els.linkWarning, "hidden", !warning);
  const key = warning ? `${warning.count}\u0000${warning.first}` : "";
  if (els.linkWarning.dataset.warningKey === key) return;
  setData(els.linkWarning, "warningKey", key);
  if (!warning) {
    els.linkWarning.replaceChildren();
    return;
  }
  const first = document.createElement("code");
  first.textContent = warning.first;
  els.linkWarning.replaceChildren(tr(warningLine(warning)), first);
}

function renderSavedOptions(select, options, selected, key) {
  if (select.dataset.optionsKey !== key) {
    select.replaceChildren(...options.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = tr(item.text);
      option.disabled = Boolean(item.disabled);
      return option;
    }));
    setData(select, "optionsKey", key);
  }
  setProp(select, "value", selected);
}

function renderSavedHistory(history) {
  const focused = document.activeElement;
  const focusInHistory = els.history.contains(focused);
  const previousPhase = els.history.dataset.latestPhase;
  setData(els.history, "latestPhase", history.latestPhase);
  setAttr(els.historyOpen, "aria-expanded", String(history.open));
  setProp(els.history, "hidden", !history.open);
  if (!history.open) {
    if (previousPhase === "pending" && focusInHistory) focusHistoryTrigger();
    return;
  }
  renderSavedOptions(els.historySelector, history.options, history.selected, JSON.stringify(history.options));
  setProp(els.historyLink, "disabled", history.linkDisabled);
  setProp(els.historyClose, "textContent", tr(history.closeLabel));
  setAttr(els.historyClose, "aria-disabled", String(history.latestPhase === "pending"));
  const legacyNotice = tr(history.legacyNotice);
  setProp(els.historyLegacy, "textContent", legacyNotice);
  setProp(els.historyLegacy, "hidden", !legacyNotice);
  const status = tr(history.status);
  setProp(els.historyStatus, "textContent", status);
  setProp(els.historyStatus, "hidden", !status);
  if (previousPhase === "pending" && history.latestPhase === "failed"
    && (focusInHistory || focused === els.historyOpen)) {
    els.historyClose.focus();
  } else if (previousPhase === "pending" && !history.latestPhase && history.selected === ""
    && focused === els.historyClose) {
    els.historySelector.focus();
  }
}

function renderLifecycleNotice(work) {
  let message = "";
  if (work.tombstone) {
    message = t("Bản lưu này đã bị xóa vĩnh viễn khỏi lịch sử Notes. Liên kết vẫn giữ mã bản lưu; không thể khôi phục từ giao diện.");
  } else if (work.canRestore) {
    message = t("Bản lưu này đang ở Thùng rác. Markdown và citations được ẩn cho đến khi bạn khôi phục bản lưu.");
  }
  setProp(trashEls.lifecycleNotice, "textContent", message);
  setProp(trashEls.lifecycleNotice, "hidden", !message);
}

function renderTrash(trash) {
  if (!trashEls) return;
  const focused = document.activeElement;
  // A row replacement can make the browser report <body> before the response
  // paint reaches this render. It is still the lifecycle action's own focus
  // transition, so the status region must receive the one-shot focus.
  const focusInTrash = trashEls.root.contains(focused) || focused === document.body;
  const previousConfirmation = trashEls.root.dataset.confirmationOpen === "true";
  const previousConfirmationKind = trashEls.root.dataset.confirmationKind || "";
  const previousMutation = trashEls.root.dataset.mutationPhase || "";
  const confirmation = trash.confirmation;
  const mutation = trash.mutation;
  const action = trash.versionAction;
  const confirmationKind = confirmation?.kind || "";
  const versionConfirmation = confirmationKind === "trash-version";
  const open = Boolean(trash.visible);
  const busy = trash.readPending || mutation?.phase === "pending";
  const protectedMessage = action.visible && action.lifecycleStatus === "active" && !action.canTrash
    ? t("Bản AI gốc và bản Save mới nhất của mỗi revision được bảo vệ.") : "";
  const lifecycleMessage = action.lifecycleStatus === "trashed"
    ? t("Bản lưu này đang ở Thùng rác; khôi phục để xem lại nội dung.")
    : action.lifecycleStatus === "purged"
      ? t("Bản lưu này đã bị xóa vĩnh viễn; nội dung không còn khả dụng qua liên kết này.")
      : protectedMessage;
  const meetingTitle = trash.meetingTitle || t("cuộc họp này");
  const scopeMessage = trash.count > 0
    ? t("Thùng rác của cuộc họp “{meeting}” có {count} bản Save lịch sử đủ điều kiện. Bản AI gốc và bản Save mới nhất của mỗi revision được giữ lại.", { meeting: meetingTitle, count: trash.count })
    : t("Thùng rác của cuộc họp “{meeting}” đang trống. Bản AI gốc và bản Save mới nhất của mỗi revision không nằm trong phạm vi này.", { meeting: meetingTitle });

  setProp(trashEls.root, "hidden", !open);
  setProp(trashEls.title, "textContent", t("Thùng rác Notes"));
  setProp(trashEls.scope, "textContent", scopeMessage);
  setProp(trashEls.versionStatus, "textContent", lifecycleMessage);
  setProp(trashEls.versionStatus, "hidden", !lifecycleMessage);
  setProp(trashEls.itemsTitle, "textContent", t("Các bản Save trong Thùng rác"));
  setProp(trashEls.itemsTitle, "hidden", trash.items.length === 0);
  renderTrashItems(trashEls.items, trash.items, { restore: true, busy });
  setProp(trashEls.move, "textContent", t("Chuyển bản lưu này vào Thùng rác"));
  setProp(trashEls.restore, "textContent", t("Khôi phục bản lưu này"));
  setProp(trashEls.move, "hidden", !action.visible || !action.canTrash);
  setProp(trashEls.restore, "hidden", !action.visible || !action.canRestore);
  setProp(trashEls.move, "disabled", action.disabled || Boolean(confirmation?.open));
  setProp(trashEls.restore, "disabled", action.disabled || Boolean(confirmation?.open));
  setProp(trashEls.retry, "textContent", t("Thử lại"));
  setProp(trashEls.retry, "hidden", !mutation?.retryable);
  setProp(trashEls.retry, "disabled", trash.readPending);
  setProp(trashEls.reload, "textContent", t("Tải lại Thùng rác"));
  setProp(trashEls.reload, "disabled", busy || Boolean(confirmation?.open));
  setProp(trashEls.empty, "textContent", t("Dọn sạch Thùng rác"));
  setProp(trashEls.empty, "disabled", trash.emptyDisabled || Boolean(confirmation?.open));

  setProp(trashEls.confirmation, "hidden", !confirmation?.open);
  setProp(trashEls.confirmationTitle, "textContent", versionConfirmation
    ? t("Xác nhận chuyển bản lưu vào Thùng rác") : t("Xác nhận dọn sạch Thùng rác"));
  setProp(trashEls.confirmationScope, "textContent", confirmation?.open
    ? versionConfirmation
      ? t("Bạn sắp chuyển bản Save {version} của Notes revision {revision} vào Thùng rác. Phiên bản và quyền bảo vệ sẽ được kiểm tra lại trước khi thực hiện.", {
        revision: confirmation.noteId,
        version: confirmation.editVersion,
      })
      : t("Bạn sắp xóa vĩnh viễn {count} bản Save lịch sử trong cuộc họp “{meeting}”. Danh sách và phiên bản sẽ được kiểm tra lại trước khi thực hiện.", {
        count: confirmation.count,
        meeting: confirmation.meetingTitle || meetingTitle,
      }) : "");
  setProp(trashEls.confirmationItemsTitle, "textContent", confirmation?.open
    ? versionConfirmation ? t("Bản Save chính xác sẽ được chuyển vào Thùng rác") : t("Các bản Save chính xác sẽ bị xóa vĩnh viễn") : "");
  setProp(trashEls.confirmationItemsTitle, "hidden", !confirmation?.open);
  renderTrashItems(trashEls.confirmationItems, confirmation?.items || [], { restore: false });
  setProp(trashEls.confirmationLabel, "hidden", versionConfirmation || !confirmation?.open);
  setProp(trashEls.confirmationLabelText, "textContent", confirmation?.open && !versionConfirmation
    ? t("Tôi xác nhận đúng cuộc họp và đúng {count} bản Save này.", { count: confirmation.count }) : "");
  setProp(trashEls.checkbox, "checked", Boolean(confirmation?.checked));
  setProp(trashEls.checkbox, "disabled", busy || versionConfirmation);
  setProp(trashEls.confirm, "textContent", versionConfirmation ? t("Chuyển vào Thùng rác") : t("Xác nhận dọn sạch"));
  setProp(trashEls.confirm, "disabled", !confirmation?.open || (!versionConfirmation && !confirmation.checked) || busy);
  setProp(trashEls.cancel, "textContent", t("Hủy"));
  setProp(trashEls.cancel, "disabled", busy);
  setProp(trashEls.confirmationError, "textContent", tr(confirmation?.error));
  setProp(trashEls.confirmationError, "hidden", !confirmation?.error);

  const status = mutation?.phase === "pending"
    ? t("Đang cập nhật Thùng rác…")
    : trash.readPending
      ? t("Đang đọc Thùng rác…")
      : mutation?.phase === "failed"
        ? (mutation.error || t("Không thể hoàn tất thao tác. Hãy thử lại sau khi tải lại Thùng rác."))
        : trash.error || trash.status;
  setProp(trashEls.status, "textContent", tr(status));
  setProp(trashEls.status, "hidden", !status);
  setData(trashEls.root, "confirmationOpen", String(Boolean(confirmation?.open)));
  setData(trashEls.root, "confirmationKind", confirmationKind);
  setData(trashEls.root, "mutationPhase", mutation?.phase || "");

  if (!open) return;
  if (confirmation?.open && !previousConfirmation) {
    (versionConfirmation ? trashEls.confirm : trashEls.checkbox).focus({ preventScroll: true });
  } else if (!confirmation?.open && previousConfirmation
    && [trashEls.checkbox, trashEls.confirm, trashEls.cancel].includes(focused)) {
    if (mutation?.phase === "pending") trashEls.status.focus({ preventScroll: true });
    else if (previousConfirmationKind === "trash-version") trashEls.move.focus({ preventScroll: true });
    else trashEls.empty.focus({ preventScroll: true });
  } else if (previousMutation === "pending" && mutation?.phase === "failed" && focusInTrash) {
    trashEls.status.focus({ preventScroll: true });
  } else if (previousMutation === "pending" && !mutation && focusInTrash) {
    trashEls.status.focus({ preventScroll: true });
  }
}

function renderSavedWork(work) {
  renderLifecycleNotice(work);
  renderSavedOptions(els.selector, work.options, work.selected, work.selectorKey);
  setProp(els.savedWork, "hidden", work.hidden);
  setProp(els.historyOpen, "hidden", !work.selected || !work.options.length);
  setProp(els.modeControls, "hidden", work.pinned);
  setProp(els.save, "hidden", work.pinned);
  setProp(els.editCancel, "hidden", work.pinned);
  const write = work.mode === "write";
  const rich = work.mode === "rich";
  setAttr(els.modeView, "aria-pressed", String(!write && !rich));
  setAttr(els.modeWrite, "aria-pressed", String(write));
  setAttr(els.modeRich, "aria-pressed", String(rich));
  setProp(els.preview, "hidden", write || rich);
  setProp(els.markdown, "hidden", !write);
  setProp(els.rich, "hidden", !rich);
  setProp(els.richFallback, "hidden", !work.richFallback);
  if (work.hidden) {
    delete els.savedWork.dataset.refocusSelection;
    setProp(els.revisionMeta, "textContent", tr(work.meta));
    renderBlocks(els.preview, []);
    setProp(els.markdown, "value", "");
    renderLinkWarning(null);
    return;
  }
  setProp(els.markdown, "disabled", work.textareaDisabled);
  setProp(els.markdown, "value", work.draftValue.replace(/\r\n?/g, "\n"));
  setProp(els.revisionMeta, "textContent", tr(work.meta));
  renderLinkWarning(work.warning);
  renderBlocks(els.preview, write || rich ? [] : work.blocks);
  if (rich) renderBlocks(els.rich, work.blocks, { rich, disabled: work.textareaDisabled, selectionId: work.selectionId });
  setProp(els.save, "disabled", work.saveDisabled);
  setProp(els.editCancel, "disabled", work.cancelDisabled);
  setProp(els.copyRich, "disabled", work.copyDisabled);
  setProp(els.copyEmail, "disabled", work.copyDisabled);
  setProp(els.copy, "disabled", work.copyDisabled);
  setProp(els.export, "disabled", work.copyDisabled);
  setProp(els.draftStatus, "textContent", tr(work.draftStatus));
  setProp(els.unsavedDot, "hidden", !work.dirty);
  setProp(els.viewDraft, "hidden", work.viewDraftHidden);
  setAttr(els.modeRich, "aria-label", tr(work.writeLabel));
  setProp(els.conflict, "hidden", !work.conflict);
  setProp(els.conflictMessage, "textContent", work.conflict ? tr(work.conflict.message) : "");
  setProp(els.keepDraft, "disabled", Boolean(work.conflict && work.conflict.disabled));
  setProp(els.useLatest, "disabled", Boolean(work.conflict && work.conflict.disabled));
  const { refocusSelection, refocusRun } = els.savedWork.dataset;
  if (refocusSelection && (refocusSelection !== String(work.selectionId) || !work.textareaDisabled)) {
    delete els.savedWork.dataset.refocusSelection;
    delete els.savedWork.dataset.refocusRun;
    if (refocusSelection === String(work.selectionId)
      && (document.activeElement === document.body || document.activeElement === els.save)) {
      const run = rich && refocusRun && els.rich.querySelector(`[data-rich-index="${refocusRun}"]`);
      if (run) run.focus({ preventScroll: true });
      else focusEditor();
    }
  }
}

function renderSource(source) {
  const members = document.getElementById("notes-source-members");
  const key = JSON.stringify(source.members);
  if (members.dataset.key !== key) {
    members.dataset.key = key;
    members.replaceChildren(...source.members.map(citationChip));
  }
  setProp(els.sourceView, "hidden", source.hidden);
  setProp(els.sourceContext, "textContent", tr(source.context));
  setProp(els.sourceTime, "textContent", source.time);
  setProp(els.sourceSpeaker, "textContent", tr(source.speaker));
  setProp(els.sourceDate, "textContent", tr(source.date));
  setProp(els.sourceText, "textContent", source.text);
  const difference = tr(source.difference);
  setProp(els.sourceDifference, "textContent", difference);
  setProp(els.sourceDifference, "hidden", !difference);
  setProp(els.sourceStatus, "textContent", tr(source.status));
  setProp(els.sourceSeek, "disabled", source.seekDisabled);
  if (els.sourceHeading.dataset.focusToken !== String(source.focus)) {
    setData(els.sourceHeading, "focusToken", String(source.focus));
    if (source.focus > 0) els.sourceHeading.focus({ preventScroll: true });
  }
}

function render(state) {
  const view = selectView(state);
  renderRegenerateDialog(view);
  if (!state.meeting) renderCopyPreview(null);
  if (!state.meeting) return; // the tab is hidden and keeps what it last showed
  setProp(els.empty, "hidden", !view.needed);
  setProp(els.create, "hidden", view.needed);
  setProp(els.run, "hidden", view.needed);
  setProp(els.status, "hidden", view.needed);
  if (view.needed) setProp(els.emptyCopy, "textContent", tr(view.emptyCopy));
  setProp(els.language, "value", view.outputLanguage);
  setProp(els.language, "disabled", view.languageDisabled);
  renderChoice(view);
  setData(els.start, "action", view.run.action);
  setAttr(els.start, "aria-describedby", view.run.describedBy);
  setProp(els.start, "disabled", view.run.disabled);
  setProp(els.start, "title", tr(view.run.title));
  setProp(els.start, "textContent", tr(view.run.label));
  setProp(els.reviewCorrection, "hidden", !view.run.correctionVisible);
  setProp(els.reviewCorrection, "disabled", view.run.correctionDisabled);
  // A run copies the speaker names as they stand, so say which ones are still the system's own.
  const names = view.reminder.names;
  setProp(els.reminder, "hidden", !names.length || view.reminder.blocked);
  setProp(els.reminderNames, "textContent", tr(speakerReminder(names)));
  const taskFocusTarget = renderTask(view);
  setProp(els.error, "hidden", !view.error);
  setProp(els.errorMessage, "textContent", tr(view.error));
  renderSavedWork(view.savedWork);
  renderSavedHistory(view.savedHistory);
  renderTrash(view.trash);
  // Move only after saved work settles, preserving focus on generation controls.
  els.heading.classList.toggle("visually-hidden", !view.savedWork.hidden);
  const urgent = Boolean(view.error || (view.task && !view.task.steps.every((step) => step === "done")));
  placeRunControls(view.run.inToolbar, urgent);
  setProp(els.copyStatus, "textContent", tr(view.actionMessage));
  renderSource(view.source);
  renderCopyPreview(state.activeTab === "notes" ? view.copyPreview : null);
  taskFocusTarget?.focus();
}

function renderCopyPreview(preview) {
  if (!preview) {
    if (els.copyDialog.open) {
      els.copyDialog.close();
      renderBlocks(els.copyDocument, []);
      setProp(els.copySubject, "textContent", "");
    }
    return;
  }
  const { content, status, copying } = preview;
  const email = content.kind === "email";
  setAttr(els.copyDialog, "data-copy-kind", content.kind);
  setProp(els.copyTitle, "textContent", email ? t("Xem trước email") : t("Xem trước nội dung có định dạng"));
  setProp(els.copyPin, "textContent", t("Bản đã lưu · edit {version}", { version: content.editVersion }));
  setProp(els.copySubject, "textContent", email ? t("Chủ đề: {subject}", { subject: content.subject }) : "");
  setProp(els.copySubject, "hidden", !email);
  setProp(els.copyConfirm, "textContent", email ? t("Sao chép nội dung email") : t("Sao chép"));
  setAttr(els.copyConfirm, "aria-disabled", String(copying));
  setProp(els.copyResult, "textContent", tr(status));
  renderBlocks(els.copyDocument, content.blocks, { clipboard: true });
  if (!els.copyDialog.open) els.copyDialog.showModal();
}

// Invoke write during the click. A resolved preview has no network dependency.
async function copyFormatted(html, markdown) {
  try {
    await navigator.clipboard.write([new ClipboardItem({
      "text/html": new Blob([html], { type: "text/html" }),
      "text/plain": new Blob([markdown], { type: "text/plain" }),
    })]);
    return "html";
  } catch {
    // Some browsers expose ClipboardItem but decline the HTML write.
    await navigator.clipboard.writeText(markdown);
    return "plain";
  }
}

// ---- runner ----

const run = createRunner({
  fetch: (url, init) => fetch(url, init),
  setTimeout: (callback, delay) => window.setTimeout(callback, delay),
  clearTimeout: (id) => window.clearTimeout(id),
  navigate: (url) => window.location.assign(url),
  storage: { local: () => window.localStorage, session: () => window.sessionStorage },
  calls: {
    "history.setNotesSourceLock": meetingHistory.setNotesSourceLock,
    "history.setNotesSummary": meetingHistory.setNotesSummary,
    "history.selectTab": meetingHistory.selectTab,
    "history.sourceRequestStarted": meetingHistory.sourceRequestStarted,
    "history.markNotesSource": meetingHistory.markNotesSource,
    "history.showNotesSourceTab": meetingHistory.showNotesSourceTab,
    "history.focusSpeakerCatalog": meetingHistory.focusSpeakerCatalog,
    "history.refreshMeeting": meetingHistory.refreshMeeting,
    "history.seekNotesSource": async (source, sourceRequestId, options) => meetingHistory.seekNotesSource(source, sourceRequestId, options),
    "history.playRecording": async () => meetingHistory.playRecording(),
    "meeting.change": meetingHistory.publishMeetingChange,
    replaceUrl: (url) => window.history.replaceState(window.history.state, "", url),
    copy: (text) => navigator.clipboard.writeText(text),
    copyFormatted,
    download: (filename, text) => {
      const objectUrl = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = filename;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    },
  },
});
const store = createStore({ reduce, initialState, run });
const { dispatch } = store;
mountTrashControls();
store.subscribe(render);

// ---- events in ----

function meetingContext(detail) {
  const meeting = detail.view === "meeting" ? detail.meeting : null;
  const meetingId = meeting ? meeting.id : null;
  // Renames, segment assignments and edits publish a context, so the names travel with it (an added
  // speaker without segments shows up at the next context).
  const generation = currentGeneration(meeting);
  dispatch({
    type: "notes/context",
    detail,
    speakerNames: generation ? meetingHistory.defaultSpeakerNames(generation) : [],
    href: window.location.href,
    stored: meetingId
      ? {
        pending: readStored(pendingRequestStorageKey(meetingId)),
        legacy: readStored(requestStorageKey(meetingId)),
        language: readStored(outputLanguageStorageKey(meetingId)),
      }
      : {},
  });
}

function newRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  return `notes-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

els.start.addEventListener("click", (event) => {
  // The header launcher already opens Connections at its own provider's section.
  if (event.currentTarget.dataset.action === "connect") $(`#launcher-${selectedProvider(store.getState(), null)}`).click();
  else if (event.currentTarget.dataset.action === "configure") dispatch({ type: "notes/configure-clicked" });
  else if (event.currentTarget.dataset.action === "regenerate") {
    dispatch({ type: "notes/regenerate-clicked" });
  }
  else dispatch({ type: "notes/start-clicked", requestId: newRequestId() });
});
els.reviewCorrection.addEventListener("click", () => dispatch({ type: "notes/regenerate-clicked" }));
els.regenerateProvider.addEventListener("change", (event) => dispatch({ type: "notes/regenerate-provider-picked", provider: event.currentTarget.value }));
els.regenerateModel.addEventListener("change", (event) => dispatch({ type: "notes/regenerate-model-picked", model: event.currentTarget.value }));
els.regenerateEffort.addEventListener("change", (event) => dispatch({ type: "notes/regenerate-effort-picked", reasoningEffort: event.currentTarget.value }));
els.regenerateSource.addEventListener("change", (event) => dispatch({ type: "notes/regenerate-source-picked", generationId: event.currentTarget.value }));
els.regenerateLanguage.addEventListener("change", (event) => dispatch({ type: "notes/regenerate-language-picked", language: event.currentTarget.value }));
els.regenerateReload.addEventListener("click", () => dispatch({ type: "notes/regenerate-reload-clicked" }));
els.regenerateSourceAdopt.addEventListener("click", () => dispatch({ type: "notes/regenerate-source-adopted" }));
els.regenerateSubmit.addEventListener("click", () => dispatch({ type: "notes/regenerate-submit", requestId: newRequestId() }));
els.regenerateCancel.addEventListener("click", () => dispatch({ type: "notes/regenerate-cancelled" }));
els.regenerateDialog.addEventListener("cancel", (event) => {
  if (event.target !== els.regenerateDialog) return;
  event.preventDefault();
  dispatch({ type: "notes/regenerate-cancelled" });
});
els.regenerateDialog.addEventListener("close", () => {
  if (!els.regenerateDialog.open && store.getState().regenerate.open) {
    dispatch({ type: "notes/regenerate-cancelled" });
    restoreRegenerateFocus();
  }
});
els.change.addEventListener("click", () => dispatch({ type: "notes/change-toggled" }));
els.nameSpeakers.addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  dispatch({ type: "notes/name-speakers-clicked" });
});
els.openTranscript.addEventListener("click", () => dispatch({ type: "notes/open-transcript-clicked" }));
els.reload.addEventListener("click", () => dispatch({ type: "notes/choice-reload-clicked" }));
// Connections saved a model or changed a provider status: reload before a new run can start.
providerChoice.subscribe(() => dispatch({ type: "notes/provider-choice-changed" }));
meetingChanges.subscribe((detail) => {
  // Remote hints refresh history first; its authoritative context triggers the notes read.
  // Only local transcript/metadata writes need the direct notes refresh here.
  if (detail?.local && typeof detail.meetingId === "string" && detail.meetingId) {
    dispatch({ type: "notes/meeting-change-received", meetingId: detail.meetingId });
  }
});
els.language.addEventListener("change", (event) => dispatch({ type: "notes/language-chosen", language: event.currentTarget.value }));
els.provider.addEventListener("change", (event) => dispatch({ type: "notes/provider-picked", provider: event.currentTarget.value }));
els.model.addEventListener("change", (event) => dispatch({ type: "notes/model-picked", model: event.currentTarget.value }));
els.cancel.addEventListener("click", () => dispatch({ type: "notes/cancel-clicked" }));
els.selector.addEventListener("change", (event) => dispatch({
  type: "notes/revision-picked", id: event.currentTarget.value, href: window.location.href,
}));
function focusHistoryTrigger() {
  if (els.historyOpen.getClientRects().length) els.historyOpen.focus();
  else $("#meeting-tab-notes").focus();
}

function closeSavedHistory() {
  dispatch({ type: "notes/saved-history-closed", href: window.location.href });
  if (els.history.hidden) focusHistoryTrigger();
}

els.historyOpen.addEventListener("click", () => {
  if (els.historyOpen.getAttribute("aria-expanded") === "true") closeSavedHistory();
  else {
    dispatch({ type: "notes/saved-history-opened" });
    els.historySelector.focus();
  }
});
els.historySelector.addEventListener("change", (event) => {
  const { value } = event.currentTarget;
  dispatch({ type: "notes/saved-history-picked", editVersion: value === "" ? null : Number(value), href: window.location.href });
});
els.historyLink.addEventListener("click", () => dispatch({ type: "notes/saved-history-link" }));
els.historyClose.addEventListener("click", closeSavedHistory);
els.markdown.addEventListener("beforeinput", (event) => {
  const textarea = event.currentTarget;
  setData(textarea, "markdownSelectionBefore", JSON.stringify({
    anchor: textarea.selectionStart,
    focus: textarea.selectionEnd,
    length: textarea.value.length,
  }));
});
els.markdown.addEventListener("input", (event) => {
  const textarea = event.currentTarget;
  const before = JSON.parse(textarea.dataset.markdownSelectionBefore || "null");
  delete textarea.dataset.markdownSelectionBefore;
  let selection = before;
  if (before && before.anchor === before.focus && event.inputType?.startsWith("delete")) {
    const start = textarea.selectionStart;
    const end = start + before.length - textarea.value.length;
    if (start < end && start <= before.anchor && before.anchor <= end) {
      selection = { anchor: start, focus: end };
    }
  }
  dispatch({ type: "notes/draft-edited", value: textarea.value, selection });
});
function focusEditor() {
  if (!els.markdown.hidden) els.markdown.focus({ preventScroll: true });
  else if (!els.rich.hidden) els.rich.querySelector('[contenteditable="plaintext-only"]')?.focus({ preventScroll: true });
}

function richPointOffset(element, node, offset) {
  if (!node || !element.contains(node)) return null;
  const range = document.createRange();
  range.selectNodeContents(element);
  range.setEnd(node, offset);
  return range.toString().length;
}

function richSelectionOffsets(element) {
  const selection = window.getSelection();
  if (!selection?.rangeCount
    || !element.contains(selection.anchorNode) || !element.contains(selection.focusNode)) return null;
  const anchor = richPointOffset(element, selection.anchorNode, selection.anchorOffset);
  const focus = richPointOffset(element, selection.focusNode, selection.focusOffset);
  return Number.isInteger(anchor) && Number.isInteger(focus) ? { anchor, focus } : null;
}

function validRichSelection(selection, length) {
  if (!selection || !Number.isInteger(selection.anchor) || !Number.isInteger(selection.focus)
    || selection.anchor < 0 || selection.focus < 0
    || selection.anchor > length || selection.focus > length) return null;
  return { anchor: selection.anchor, focus: selection.focus };
}

function richCaretOffset(element, fallback) {
  const offsets = richSelectionOffsets(element);
  return offsets && offsets.anchor === offsets.focus ? offsets.anchor : fallback;
}

function richPointAtOffset(element, offset) {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let node;
  let remaining = Math.max(0, offset);
  let last = null;
  while ((node = walker.nextNode())) {
    last = node;
    if (remaining <= node.textContent.length) return [node, remaining];
    remaining -= node.textContent.length;
  }
  return last ? [last, last.textContent.length] : [element, 0];
}

function restoreRichSelection(element, offsets) {
  if (!offsets || !element.isConnected) return;
  const selection = window.getSelection();
  if (!selection) return;
  const [anchorNode, anchorOffset] = richPointAtOffset(element, offsets.anchor);
  const [focusNode, focusOffset] = richPointAtOffset(element, offsets.focus);
  const start = offsets.anchor <= offsets.focus
    ? [anchorNode, anchorOffset, focusNode, focusOffset]
    : [focusNode, focusOffset, anchorNode, anchorOffset];
  const range = document.createRange();
  range.setStart(start[0], start[1]);
  range.setEnd(start[2], start[3]);
  selection.removeAllRanges();
  selection.addRange(range);
  if (offsets.anchor > offsets.focus) {
    selection.collapse(start[2], start[3]);
    selection.extend(start[0], start[1]);
  }
}

function restoreRichCaret(element, offset) {
  if (Number.isInteger(offset)) restoreRichSelection(element, { anchor: offset, focus: offset });
}

function saveFromEditor() {
  const focused = document.activeElement;
  const work = selectView(store.getState()).savedWork;
  if (work.saveDisabled) return;
  dispatch({ type: "notes/save-clicked" });
  if (work.mode !== "view") {
    setData(els.savedWork, "refocusSelection", String(work.selectionId));
    setData(els.savedWork, "refocusRun", focused.dataset.richIndex || "");
  }
}

function saveShortcut(event) {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();
  saveFromEditor();
}

function updateRichRun(element, value, rejectedSelection = null, beforeSelection = null) {
  const before = element.dataset.richValue;
  const index = Number(element.dataset.richIndex);
  const caret = richCaretOffset(element, value.length);
  dispatch({
    type: "notes/rich-edited", index, before, value,
    selection: validRichSelection(beforeSelection, before.length),
  });
  const work = selectView(store.getState()).savedWork;
  const run = work.mode === "rich" && work.blocks.flatMap(blockRuns)[index];
  if (value !== before && run?.text === before) {
    element.textContent = before;
    setData(element, "richValue", before);
    restoreRichSelection(element, rejectedSelection || { anchor: before.length, focus: before.length });
    return;
  }
  restoreRichCaret(element, caret);
  if (els.rich.hidden) focusEditor();
  else if (!element.isConnected) focusEditor();
}

function insertRichText(element, text, rejectedSelection = null) {
  const selection = window.getSelection();
  if (!selection.rangeCount || !element.contains(selection.anchorNode) || !element.contains(selection.focusNode)) return;
  const range = selection.getRangeAt(0);
  const prefix = range.cloneRange();
  prefix.selectNodeContents(element);
  prefix.setEnd(range.startContainer, range.startOffset);
  const start = prefix.toString().length;
  const value = element.textContent;
  const selectedLength = range.toString().length;
  const proposed = value.slice(0, start) + text + value.slice(start + selectedLength);
  updateRichRun(element, proposed, rejectedSelection, { anchor: start, focus: start + selectedLength });
  if (element.textContent !== proposed) return;
  if (element.isConnected && !els.rich.hidden && element.firstChild) {
    selection.collapse(element.firstChild, Math.min(start + text.length, element.textContent.length));
  }
}

els.markdown.addEventListener("keydown", saveShortcut);
els.rich.addEventListener("keydown", (event) => {
  if (!event.isComposing && event.key === "Enter" && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    return;
  }
  saveShortcut(event);
});
els.rich.addEventListener("compositionstart", (event) => {
  const offsets = richSelectionOffsets(event.target);
  if (offsets) setData(event.target, "richSelectionComposition", JSON.stringify(offsets));
  event.target.setAttribute("data-composing", "");
});
els.rich.addEventListener("compositionend", (event) => {
  let rejectedSelection = null;
  try {
    rejectedSelection = JSON.parse(event.target.dataset.richSelectionComposition || "null");
  } catch {
    rejectedSelection = null;
  }
  event.target.removeAttribute("data-composing");
  delete event.target.dataset.richSelectionBefore;
  delete event.target.dataset.richSelectionComposition;
  updateRichRun(event.target, event.target.textContent, rejectedSelection, rejectedSelection);
});
els.rich.addEventListener("input", (event) => {
  if (!event.isComposing && !event.target.hasAttribute("data-composing")) {
    let rejectedSelection = null;
    let beforeSelection = null;
    try {
      beforeSelection = JSON.parse(event.target.dataset.richSelectionBefore || "null");
      rejectedSelection = beforeSelection;
    } catch {
      rejectedSelection = null;
      beforeSelection = null;
    }
    delete event.target.dataset.richSelectionBefore;
    updateRichRun(event.target, event.target.textContent, rejectedSelection, beforeSelection);
  }
});
els.rich.addEventListener("beforeinput", (event) => {
  if (!event.isComposing && !event.target.hasAttribute("data-composing")) {
    delete event.target.dataset.richSelectionBefore;
    const offsets = richSelectionOffsets(event.target);
    if (offsets) setData(event.target, "richSelectionBefore", JSON.stringify(offsets));
  }
  if (!event.isComposing && (event.inputType === "insertParagraph" || event.inputType === "insertLineBreak")) {
    delete event.target.dataset.richSelectionBefore;
    event.preventDefault();
  }
});
els.rich.addEventListener("paste", (event) => {
  event.preventDefault();
  let rejectedSelection = richSelectionOffsets(event.target);
  try {
    rejectedSelection ||= JSON.parse(event.target.dataset.richSelectionBefore || "null");
  } catch {
    // A live selection is authoritative; a malformed cached selection is only a fallback.
  }
  delete event.target.dataset.richSelectionBefore;
  insertRichText(event.target, event.clipboardData.getData("text/plain").replace(/\r\n?|\n/g, " "), rejectedSelection);
});
els.rich.addEventListener("drop", (event) => event.preventDefault());
for (const [button, mode] of [[els.modeView, "view"], [els.modeRich, "rich"], [els.modeWrite, "write"]]) {
  button.addEventListener("click", () => {
    const before = store.getState().mode;
    dispatch({ type: "notes/mode-chosen", mode });
    if (mode !== "view" && before !== mode) focusEditor();
  });
}
els.save.addEventListener("click", saveFromEditor);
els.editCancel.addEventListener("click", () => {
  dispatch({ type: "notes/edit-canceled" });
  els.modeView.focus({ preventScroll: true });
});
els.keepDraft.addEventListener("click", () => dispatch({ type: "notes/keep-draft-clicked" }));
els.useLatest.addEventListener("click", () => dispatch({ type: "notes/use-latest-clicked" }));
els.copy.addEventListener("click", () => dispatch({ type: "notes/copy-clicked" }));
els.copyRich.addEventListener("click", () => dispatch({ type: "notes/rich-preview-clicked" }));
els.copyEmail.addEventListener("click", () => dispatch({ type: "notes/email-preview-clicked" }));
els.copyConfirm.addEventListener("click", () => dispatch({ type: "notes/preview-copy-clicked" }));
els.copyClose.addEventListener("click", () => dispatch({ type: "notes/preview-closed" }));
els.copyDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  dispatch({ type: "notes/preview-closed" });
});
els.export.addEventListener("click", () => dispatch({ type: "notes/export-clicked" }));
function openSourceLink(event) {
  const link = event.target.closest("a[data-notes-source-link]");
  if (
    !link
    || event.defaultPrevented
    || event.button !== 0
    || event.metaKey
    || event.ctrlKey
    || event.shiftKey
    || event.altKey
  ) return;
  const reference = sourceReference(link.getAttribute("href"), window.location.origin, link.dataset.meetingId, link.dataset.snapshotId);
  if (!reference || reference.segmentId !== link.dataset.segmentId) return;
  event.preventDefault();
  dispatch({ type: "notes/source-opened", reference, fromClick: true });
}
els.preview.addEventListener("click", openSourceLink);
els.sourceView.addEventListener("click", openSourceLink);
els.sourceClose.addEventListener("click", () => dispatch({ type: "notes/source-closed", href: window.location.href }));
els.sourceSeek.addEventListener("click", () => dispatch({ type: "notes/source-seek-clicked" }));

dispatch({
  type: "notes/booted",
  mode: readStored(VIEW_MODE_KEY, ["local"]),
  hidden: document.hidden,
  origin: window.location.origin,
});
meetingChannel.subscribe(meetingContext);
// history.js has already published its first context by now (this module imports it).
meetingContext(meetingHistory.currentContext());
window.addEventListener("popstate", () => dispatch({ type: "notes/location-changed", href: window.location.href }));
// A reload or close drops every in-memory draft, so warn while a notes or transcript draft is unsaved.
window.addEventListener("beforeunload", (event) => {
  const unsaved = hasUnsavedDraft(store.getState()) || meetingHistory.hasUnsavedTranscriptDrafts();
  if (!unsaved) return;
  event.preventDefault();
  event.returnValue = "";
});
document.addEventListener("visibilitychange", () => dispatch({ type: "notes/visibility-changed", hidden: document.hidden }));
