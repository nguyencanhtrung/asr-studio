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
  unsavedDot: $("#notes-unsaved-dot"),
  linkWarning: $("#notes-link-warning"),
  viewDraft: $("#notes-view-draft"),
  preview: $("#notes-preview"),
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

function appendInline(parent, nodes) {
  for (const node of nodes) {
    if (node.kind === "text") {
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
    } else if (node.kind === "link") {
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
      const element = document.createElement({ strong: "strong", code: "code", em: "em" }[node.kind]);
      element.textContent = node.text;
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

// Rebuilt only when the shaped blocks change: a rebuild would drop keyboard focus on a chip or source link.
function renderBlocks(root, blocks) {
  const key = JSON.stringify(blocks);
  if (root.dataset.blocksKey === key) return;
  setData(root, "blocksKey", key);
  const children = [];
  for (const block of blocks) {
    if (block.kind === "heading") {
      const element = document.createElement(`h${block.level}`);
      appendInline(element, block.nodes);
      children.push(element);
    } else if (block.kind === "list") {
      const list = document.createElement("ul");
      for (const entry of block.items) {
        const item = document.createElement("li");
        appendInline(item, entry.nodes);
        for (const chip of entry.chips) item.append(citationChip(chip));
        list.append(item);
      }
      children.push(list);
    } else {
      const paragraph = document.createElement("p");
      block.lines.forEach((nodes, index) => {
        if (index) paragraph.append(document.createElement("br"));
        appendInline(paragraph, nodes);
      });
      children.push(paragraph);
    }
  }
  root.replaceChildren(...children);
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
  setAttr(els.modeView, "aria-pressed", String(!write));
  setAttr(els.modeWrite, "aria-pressed", String(write));
  setProp(els.preview, "hidden", write);
  setProp(els.markdown, "hidden", !write);
  if (work.hidden) {
    delete els.markdown.dataset.refocus;
    setProp(els.revisionMeta, "textContent", tr(work.meta));
    renderBlocks(els.preview, []);
    setProp(els.markdown, "value", "");
    renderLinkWarning(null);
    return;
  }
  setProp(els.markdown, "disabled", work.textareaDisabled);
  setProp(els.markdown, "value", work.draftValue);
  setProp(els.revisionMeta, "textContent", tr(work.meta));
  renderLinkWarning(work.warning);
  renderBlocks(els.preview, work.blocks);
  setProp(els.save, "disabled", work.saveDisabled);
  setProp(els.copy, "disabled", work.copyDisabled);
  setProp(els.export, "disabled", work.copyDisabled);
  setProp(els.draftStatus, "textContent", tr(work.draftStatus));
  setProp(els.unsavedDot, "hidden", !work.dirty);
  setProp(els.viewDraft, "hidden", work.viewDraftHidden);
  setAttr(els.modeWrite, "aria-label", tr(work.writeLabel));
  setProp(els.conflict, "hidden", !work.conflict);
  setProp(els.conflictMessage, "textContent", work.conflict ? tr(work.conflict.message) : "");
  setProp(els.keepDraft, "disabled", Boolean(work.conflict && work.conflict.disabled));
  setProp(els.useLatest, "disabled", Boolean(work.conflict && work.conflict.disabled));
  // Same path as the save button: the textarea was disabled while the request ran, which drops its focus.
  if (els.markdown.dataset.refocus && !els.markdown.disabled) {
    delete els.markdown.dataset.refocus;
    if (document.activeElement === document.body && !els.markdown.hidden) els.markdown.focus({ preventScroll: true });
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
  if (view.actionMessage) setProp(els.copyStatus, "textContent", tr(view.actionMessage));
  renderSource(view.source);
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
els.markdown.addEventListener("input", (event) => dispatch({ type: "notes/draft-edited", value: event.currentTarget.value }));
els.markdown.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
  event.preventDefault();
  dispatch({ type: "notes/save-clicked" });
  if (els.markdown.disabled) setData(els.markdown, "refocus", "1");
});
for (const [button, mode] of [[els.modeView, "view"], [els.modeWrite, "write"]]) {
  button.addEventListener("click", () => {
    const before = store.getState().mode;
    dispatch({ type: "notes/mode-chosen", mode });
    if (mode === "write" && before !== "write") els.markdown.focus({ preventScroll: true });
  });
}
els.save.addEventListener("click", () => dispatch({ type: "notes/save-clicked" }));
els.keepDraft.addEventListener("click", () => dispatch({ type: "notes/keep-draft-clicked" }));
els.useLatest.addEventListener("click", () => dispatch({ type: "notes/use-latest-clicked" }));
els.copy.addEventListener("click", () => dispatch({ type: "notes/copy-clicked" }));
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
