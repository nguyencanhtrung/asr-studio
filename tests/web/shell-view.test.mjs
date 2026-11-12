import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";

// Migrated files keep their rules by construction; these scans keep them from drifting back.
const web = (name) => readFileSync(new URL(`../../src/asr_studio/web/${name}`, import.meta.url), "utf8");
const code = (name) => web(name).replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
// Text in quotes goes; the code inside a template literal's ${…} stays, so it is scanned too.
const noStrings = (source) => source
  .replace(/`(?:[^`\\]|\\.)*`/g, (template) => [...template.matchAll(/\$\{([^}]*)\}/g)].map((match) => match[1]).join(";"))
  .replace(/"(?:[^"\\\n]|\\.)*"/g, '""');
// assert.doesNotMatch prints the whole file on failure; say only what matched.
const absent = (source, pattern, label) => assert.equal(source.match(pattern)?.[0], undefined, label);

// Every file in src/asr_studio/web/*.js is in exactly one of these lists (docs/adr/0010-frontend-reducer-core.md).
const VIEWS = ["shell.js", "connections.js", "antigravity.js", "notes.js", "history.js", "access.js"];
const CORES = ["shell-core.js", "provider-core.js", "notes-core.js", "history-core.js", "task-core.js", "transcript-core.js", "playback-core.js", "channel.js"];
// Not a view and not a pure core. `top` is every name the file may declare at its top level; `classic` marks the
// scripts that load without type="module" (they run before first paint and share their binding as a page global).
const EXCEPTIONS = {
  "store.js": { reason: "the runner: fetch, timers, storage and XHR arrive as injected dependencies", top: ["createStore", "combine", "createRunner"], classic: false },
  "audio-extract.js": { reason: "browser adapter: local metadata reads and audio extraction behind runner dependencies, loaded on the first video", top: ["fail", "TARGETS", "findBox", "movieCreationDate", "plausibleDate", "recordingDateFromFile", "readRecordingDate", "extractAudio"], classic: false },
  "i18n.js": { reason: "classic script: t() and UI_LOCALE are read-only page bindings that every module and the markup need before any module runs", top: ["t", "UI_LOCALE"], classic: true },
  "theme.js": { reason: "classic script: applies the theme before first paint, no module can run that early", top: [], classic: true },
};

// Each rule is a pattern over the view source (comments stripped); `rules` returns the labels that match.
const STATE = String.raw`(?:state|(?:\w+\.)?getState\(\)|appState)`;
const rules = (source) => {
  const found = [];
  const hit = (label, pattern) => {
    if (pattern.test(source)) found.push(label);
  };
  hit("assigns into state", new RegExp(String.raw`\b${STATE}(\.[A-Za-z_$][\w$]*|\[[^\]]+\])+\s*(=(?!=)|[-+*/]=|\+\+|--)`));
  // Aliases of the state: const s = state / store.getState(), destructuring it, Object.assign into it.
  hit("aliases or destructures the state", new RegExp(String.raw`\b(const|let|var)\s+[\w$\{\}\s,:[\]]+=\s*${STATE}\s*[;\n]`));
  hit("assigns into state with Object.assign", new RegExp(String.raw`Object\.assign\(\s*${STATE}(?![\w$])`));
  hit("deletes from state", new RegExp(String.raw`\bdelete\s+${STATE}(?![\w$])`));
  hit("calls a mutating method on state", new RegExp(String.raw`\b${STATE}(\.[A-Za-z_$][\w$]*)*\.(push|pop|shift|unshift|splice|sort|reverse|fill|set|delete|add|clear)\(`));
  hit("has a module-level let/var", /(^|\n)(let|var)\s/);
  // Quoted text (a field name another script reads) is not a counter.
  if (/\b\w*(Seq|InFlight|Timer)\b/.test(noStrings(source))) found.push("brings back a request counter or flag");
  const mutates = (name, deep = false) => new RegExp(String.raw`\b${name}(\.[A-Za-z_$][\w$]*|\[[^\]]+\])${deep ? "+" : ""}\s*(=(?!=)|[-+*/]=|\+\+|--)|\b${name}(\.[A-Za-z_$][\w$]*)*\.(push|pop|shift|unshift|splice|sort|reverse|fill|set|delete|add|clear)\(|Object\.assign\(\s*${name}\b`).test(source);
  // A module-level object or collection that the file mutates is state kept outside the store.
  for (const [, name] of source.matchAll(/(?:^|\n)(?:export\s+)?const\s+(\w+)\s*=\s*(?:\{|\[|new\s+(?:Map|Set|WeakMap|WeakSet)\b)/g)) {
    if (mutates(name)) found.push(`mutates module-level ${name}`);
  }
  // A slice of the state held under another name (const s = state.chatgpt) is the state: writing to it is too.
  for (const [, name] of source.matchAll(new RegExp(String.raw`\b(?:const|let|var)\s+(\w+)\s*=\s*${STATE}(?:\.[A-Za-z_$][\w$]*|\[[^\]]+\])*\s*[;\n]`, "g"))) {
    if (mutates(name, true)) found.push(`mutates an alias of the state ${name}`);
  }
  return found;
};

const forbidden = /\b(document|window|fetch|XMLHttpRequest|setTimeout|setInterval|clearTimeout|localStorage|sessionStorage|Date\.now|new Date|Math\.random|navigator|location|crypto|performance|history|requestAnimationFrame)\b/;

test("core files are pure: no DOM, network, timer, storage, clock or randomness", () => {
  for (const name of CORES) absent(noStrings(code(name)), forbidden, name);
});

test("views do not assign into store state or keep module-level mutable state", () => {
  for (const name of VIEWS) assert.deepEqual(rules(code(name)), [], name);
});

test("core purity names crypto, performance, history and animation frames", () => {
  for (const line of ["crypto.randomUUID()", "performance.now()", "history.replaceState(null, '', u)", "requestAnimationFrame(f)"]) {
    assert.ok(forbidden.test(noStrings(line)), line);
  }
});

test("core purity sees code inside template literals", () => {
  assert.ok(forbidden.test(noStrings("const a = `x ${document.title} y`;")));
  assert.ok(!forbidden.test(noStrings('const a = `a fetch ${1}` + "window";')));
});

test("the view rules catch the shapes they exist for", () => {
  const bad = {
    "assigns into state": "state.count = 1;",
    "aliases or destructures the state": "const s = store.getState();\ns.count = 1;",
    "assigns into state with Object.assign": "Object.assign(state, { a: 1 });",
    "deletes from state": "delete state.a;",
    "calls a mutating method on state": "state.list.push(1);",
    "has a module-level let/var": "let current = null;",
    "brings back a request counter or flag": "const loadSeq = 0;",
    "mutates module-level cache": "const cache = {};\ncache.a = 1;",
  };
  for (const [label, source] of Object.entries(bad)) assert.ok(rules(source).includes(label), label);
  assert.ok(rules("const loadSeq = 0;").includes("brings back a request counter or flag"));
  assert.ok(!rules('const d = { "xInFlight": 1 };').includes("brings back a request counter or flag"));
  assert.ok(rules("const pollTimer = 0;").includes("brings back a request counter or flag"));
  assert.ok(rules("store.getState().x = 1;").includes("assigns into state"));
  assert.ok(rules("Object.assign(store.getState(), {});").includes("assigns into state with Object.assign"));
  assert.ok(rules("delete store.getState().a;").includes("deletes from state"));
  assert.ok(rules("export const cache = {};\ncache.a = 1;").includes("mutates module-level cache"));
  assert.ok(rules("const items = [];\nitems.push(1);").includes("mutates module-level items"));
  assert.ok(rules("const s = state.chatgpt;\ns.count = 1;").includes("mutates an alias of the state s"));
  assert.ok(rules("const c = store.getState().choice;\nc.status.chatgpt = 1;").includes("mutates an alias of the state c"));
  assert.ok(rules("const c = store.getState().choice;\nc.list[0].x++;").includes("mutates an alias of the state c"));
  assert.ok(rules("const list = store.getState().items;\nlist.push(1);").includes("mutates an alias of the state list"));
  assert.ok(rules("const { a } = store.getState();").includes("aliases or destructures the state"));
  // Reading a slice and writing DOM properties is what a view does.
  assert.deepEqual(rules("const els = { a: document.body };\nels.a.hidden = true;\nconst s = state.chatgpt;\nrender(s);"), []);
});

test("views touch storage only to read the remembered choices for the boot and open actions", () => {
  for (const name of VIEWS) {
    // The runner dependencies hand storage to the storage effect; nothing else may touch it.
    const source = code(name).replace(/\n\s*storage: \{.*\},/, "");
    absent(source, /\.(setItem|removeItem|clear)\(/, `${name} writes storage outside an effect`);
    const reader = source.match(/function readStored\([^)]*\) \{[\s\S]*?\n\}/);
    const outside = reader ? source.replace(reader[0], "") : source;
    absent(outside, /localStorage|sessionStorage|getItem/, `${name} reads storage outside readStored`);
  }
});


// ---- the whole directory: classification, globals, custom events, scripts ----

const webFiles = (extension) => readdirSync(new URL("../../src/asr_studio/web/", import.meta.url)).filter((name) => name.endsWith(extension)).sort();

// What is wrong with the way `names` are sorted into the lists: an unlisted file, a file listed twice or a ghost entry.
const classify = (names, lists) => {
  const problems = [];
  for (const name of names) {
    const homes = Object.entries(lists).filter(([, members]) => members.includes(name)).map(([home]) => home);
    if (homes.length === 0) problems.push(`${name} is in no list`);
    if (homes.length > 1) problems.push(`${name} is in ${homes.join(" and ")}`);
  }
  for (const members of Object.values(lists)) for (const name of members) if (!names.includes(name)) problems.push(`${name} is listed but missing`);
  return problems;
};

// A module reaches another through import/export, never through the page: nothing is written to window or
// globalThis, and no custom event travels between modules.
const globalRules = (source) => {
  const found = [];
  const hit = (label, pattern) => {
    if (pattern.test(source)) found.push(label);
  };
  hit("assigns a window property", /\bwindow(\.[\w$]+|\[[^\]]+\])\s*(=(?!=)|[-+*/]=|\+\+|--)/);
  hit("uses globalThis", /\bglobalThis\b/);
  hit("assigns into window with Object.assign", /Object\.assign\(\s*window\b/);
  hit("defines a window property", /Object\.defineProperty\(\s*window\b/);
  hit("uses CustomEvent", /\bCustomEvent\b/);
  hit("listens for a namespaced event", /addEventListener\(\s*["'`][^"'`]*:/);
  hit("dispatches a namespaced event", /dispatchEvent\(\s*new\s+\w*Event\(\s*["'`][^"'`]*:/);
  return found;
};

// Names a file declares at column 0 (comments stripped); the body of a function or an IIFE is indented.
const topLevelNames = (source) => [...source.matchAll(/^(?:export\s+)?(?:async\s+)?(?:function\*?|const|let|var|class)\s+(\{[^}]*\}|\[[^\]]*\]|[\w$]+)/gm)]
  .flatMap((match) => match[1].match(/[\w$]+/g));

// The page's scripts: {src, module, inline}.
const scriptsOf = (html) => [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].map(([, attributes, body]) => ({
  src: attributes.match(/\bsrc="\/assets\/([\w-]+\.js)"/)?.[1] ?? null,
  module: /\btype="module"/.test(attributes),
  inline: !/\bsrc=/.test(attributes) || body.trim() !== "",
}));

// A name that used to be a window global and is now imported: a file that uses it must declare or import it.
const FORMER_GLOBALS = ["ApiError", "requestJson", "meetingIdFromPath", "formatMeetingDate", "sameOriginAudioSource", "ASRMeetingHistory", "tr", "setProp", "setAttr"];
const undeclaredFormerGlobals = (source) => FORMER_GLOBALS.filter((name) => {
  const word = new RegExp(String.raw`(?<![\w$.])${name}(?![\w$])`);
  const declared = new RegExp(String.raw`(?:function|const|let|var|class)\s+${name}\b|import\s*\{[^}]*\b${name}\b[^}]*\}`);
  return word.test(noStrings(source)) && !declared.test(source);
});

test("every web/*.js file is a view, a core or a listed exception", () => {
  assert.deepEqual(classify(webFiles(".js"), { VIEWS, CORES, EXCEPTIONS: Object.keys(EXCEPTIONS) }), []);
  for (const [name, { reason }] of Object.entries(EXCEPTIONS)) assert.ok(reason.length > 20, `${name} has a reason`);
});

test("no module writes a global or sends a custom event, in any web file", () => {
  for (const name of webFiles(".js")) assert.deepEqual(globalRules(code(name)), [], name);
});

test("no module reads a name that used to be a window global without importing it", () => {
  for (const name of webFiles(".js")) assert.deepEqual(undeclaredFormerGlobals(code(name)), [], name);
});

// What is wrong with the scripts of one page: an inline script, or a classic script that is not a listed exception.
const pageProblems = (html, classic) => scriptsOf(html).flatMap((script) => [
  ...(script.inline ? ["has an inline script"] : []),
  ...(!script.module && !script.inline && !classic.includes(script.src) ? [`loads ${script.src} as a classic script`] : []),
]);

test("pages carry no inline script; only the listed exceptions load as classic scripts", () => {
  const classic = Object.keys(EXCEPTIONS).filter((name) => EXCEPTIONS[name].classic);
  for (const page of webFiles(".html")) {
    assert.ok(scriptsOf(web(page)).length > 0, page);
    assert.deepEqual(pageProblems(web(page), classic), [], page);
  }
});

test("a classic script declares nothing at the top level but its allowlist", () => {
  for (const [name, { top }] of Object.entries(EXCEPTIONS)) assert.deepEqual([...new Set(topLevelNames(code(name)))].sort(), [...top].sort(), name);
});

test("shell.html and access.html load each view as a module, in an order that keeps imports ready", () => {
  const order = scriptsOf(web("shell.html")).filter((script) => script.src);
  const names = order.map((script) => script.src);
  assert.ok(names.indexOf("shell.js") < names.indexOf("history.js"));
  assert.ok(names.indexOf("history.js") < names.indexOf("notes.js"));
  assert.ok(names.indexOf("connections.js") < names.indexOf("antigravity.js"));
  for (const name of VIEWS.filter((view) => view !== "access.js")) assert.equal(order[names.indexOf(name)].module, true, name);
  assert.equal(scriptsOf(web("access.html")).find((script) => script.src === "access.js").module, true);
  // theme.js and i18n.js come first, before any module can paint.
  assert.deepEqual(names.slice(0, 2), ["i18n.js", "theme.js"]);
});

test("the directory gate catches the shapes it exists for", () => {
  assert.deepEqual(classify(["a.js", "b.js"], { VIEWS: ["a.js"], CORES: [], EXCEPTIONS: [] }), ["b.js is in no list"]);
  assert.deepEqual(classify(["a.js"], { VIEWS: ["a.js"], CORES: ["a.js"], EXCEPTIONS: [] }), ["a.js is in VIEWS and CORES"]);
  assert.deepEqual(classify(["a.js"], { VIEWS: ["a.js", "gone.js"], CORES: [], EXCEPTIONS: [] }), ["gone.js is listed but missing"]);
  const bad = {
    "assigns a window property": "window.ASRMeetingHistory = {};",
    "uses globalThis": "globalThis.shared = 1;",
    "assigns into window with Object.assign": "Object.assign(window, { a });",
    "defines a window property": "Object.defineProperty(window, 'a', { value: 1 });",
    "uses CustomEvent": "new CustomEvent('x');",
    "listens for a namespaced event": 'window.addEventListener("asr:thing", f);',
    "dispatches a namespaced event": 'document.dispatchEvent(new Event("asr:thing"));',
  };
  for (const [label, source] of Object.entries(bad)) assert.ok(globalRules(source).includes(label), label);
  assert.ok(globalRules("window['x'] = 1;").includes("assigns a window property"));
  assert.ok(globalRules("window.x += 1;").includes("assigns a window property"));
  // Reading window, nested properties and native events are what a view does.
  assert.deepEqual(globalRules('const a = window.location.href;\nwindow.location.href = a;\nwindow.addEventListener("popstate", f);\nselect.dispatchEvent(new Event("change"));\nif (window.x == 1) {}'), []);
  assert.deepEqual(topLevelNames("const { t, UI_LOCALE } = (() => {\n  const lang = 1;\n  function inner() {}\n})();\nexport function open() {}\nlet x;"), ["t", "UI_LOCALE", "open", "x"]);
  assert.deepEqual(scriptsOf('<script src="/assets/a.js"></script><script type="module" src="/assets/b.js"></script><script>boot()</script>'), [
    { src: "a.js", module: false, inline: false },
    { src: "b.js", module: true, inline: false },
    { src: null, module: false, inline: true },
  ]);
  assert.equal(scriptsOf('<script src="/assets/a.js">boot()</script>')[0].inline, true);
  assert.deepEqual(pageProblems('<script src="/assets/i18n.js"></script><script type="module" src="/assets/x.js"></script>', ["i18n.js"]), []);
  assert.deepEqual(pageProblems('<script src="/assets/x.js" defer></script>', ["i18n.js"]), ["loads x.js as a classic script"]);
  assert.deepEqual(pageProblems("<script>boot()</script>", ["i18n.js"]), ["has an inline script"]);
  assert.deepEqual(undeclaredFormerGlobals("const d = formatMeetingDate(x);"), ["formatMeetingDate"]);
  assert.deepEqual(undeclaredFormerGlobals('import { formatMeetingDate } from "./shell.js";\nformatMeetingDate(x);'), []);
  assert.deepEqual(undeclaredFormerGlobals("export function tr(message) { return tr(message); }\nconst other = obj.tr(x);"), []);
  assert.deepEqual(undeclaredFormerGlobals("ASRMeetingHistory.refreshMeeting(id);"), ["ASRMeetingHistory"]);
});
