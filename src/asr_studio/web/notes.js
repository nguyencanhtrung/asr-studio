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
  run: $("#notes-run"),
  status: $("#notes-status"),
  start: $("#notes-start"),
  cancel: $("#notes-cancel"),
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

function setData(element, name, value) {
  if (element.dataset[name] !== value) element.dataset[name] = value;
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

// Until notes exist the action is the primary one on the tab; afterwards it is one secondary tool in the
// notes toolbar. The whole group moves, so its choice and reminder stay beside the button.
function placeRunControls(inToolbar) {
  const host = inToolbar ? els.toolbar : els.create;
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

function renderTask(view) {
  const task = view.task;
  setProp(els.details, "hidden", !task);
  setProp(els.status, "textContent", tr(view.status));
  if (!task) {
    setProp(els.cancel, "hidden", true);
    return;
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

function renderSavedWork(work) {
  if (els.selector.dataset.optionsKey !== work.selectorKey) {
    els.selector.replaceChildren(...work.options.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = tr(item.text);
      return option;
    }));
    setData(els.selector, "optionsKey", work.selectorKey);
  }
  setProp(els.selector, "value", work.selected);
  setProp(els.savedWork, "hidden", work.hidden);
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
  if (!state.meeting) renderCopyPreview(null);
  if (!state.meeting) return; // the tab is hidden and keeps what it last showed
  const view = selectView(state);
  setProp(els.empty, "hidden", !view.needed);
  setProp(els.create, "hidden", view.needed);
  setProp(els.run, "hidden", view.needed); // it sits in the toolbar once notes exist
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
  // A run copies the speaker names as they stand, so say which ones are still the system's own.
  const names = view.reminder.names;
  setProp(els.reminder, "hidden", !names.length || view.reminder.blocked);
  setProp(els.reminderNames, "textContent", tr(speakerReminder(names)));
  renderTask(view);
  setProp(els.error, "hidden", !view.error);
  setProp(els.errorMessage, "textContent", tr(view.error));
  renderSavedWork(view.savedWork);
  // After the saved-work block has settled, so a focused control is not moved while it is hidden.
  placeRunControls(view.run.inToolbar);
  setProp(els.copyStatus, "textContent", tr(view.actionMessage));
  renderSource(view.source);
  renderCopyPreview(state.activeTab === "notes" ? view.copyPreview : null);
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
    "history.markNotesSource": meetingHistory.markNotesSource,
    "history.showNotesSourceTab": meetingHistory.showNotesSourceTab,
    "history.focusSpeakerCatalog": meetingHistory.focusSpeakerCatalog,
    "history.refreshMeeting": meetingHistory.refreshMeeting,
    "history.seekNotesSource": async (source) => meetingHistory.seekNotesSource(source),
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
  else dispatch({ type: "notes/start-clicked", requestId: newRequestId() });
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
els.selector.addEventListener("change", (event) => dispatch({ type: "notes/revision-picked", id: event.currentTarget.value }));
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
