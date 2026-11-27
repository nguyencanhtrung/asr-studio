# UX v2: behavior, contracts and availability

The handoff supplies the visual direction. Server capabilities, persisted data and the existing recording workflow define what the application can actually do. The delivery preserves the Settings modal and its provider pages, the compact centered Settings/GitHub footer with the build version underneath, and existing meeting/import navigation. Merging these changes does not update an already running deployment.

Source paths below are relative to `src/asr_studio/` unless stated otherwise.

## Delivered surfaces and compatibility boundaries

| Surface | Source and contract | Availability and refactor boundary | Delivery |
| --- | --- | --- | --- |
| Sidebar, calendar, search, recent meetings and date navigation | `web/shell.html`, `web/shell.js`, `web/shell-core.js`; existing meeting/calendar routes | [EXISTING]: mini calendar remains reachable; full calendar retains meeting chips and day detail. Search, selected date, back history, undated meetings, locale and theme remain available. | #251 / PR #264; visual correction #282 / PR #285 |
| Provider settings | `chatgpt.py`, `antigravity.py`, provider routes in `routes.py`, `notes_routes.py`, `web/connections.js`, `web/antigravity.js`, `web/notes.js` | [EXISTING]: login/relogin/disconnect, account and usage links, provider-specific model/effort, Save and synthetic test remain distinct. Authentication, reachability and the default selection are different facts. Callback controls use the actual backend flow; no invented exchange endpoint or retry. | #252 / PR #268 |
| ASR model catalog and installation | `asr_models.py`, `model_install.py`, `model_installer.py`, existing model routes | [EXISTING]: readiness and compatibility come from the server. Installation is a server job with actual state, cancellation and retry. Existing deployments keep their model defaults and volume paths. Whisper small is approved fresh-install guidance; it does not silently change an existing deployment. | #253 / PR #269; #262 / PR #280 |
| Diarization | Existing Community-1 runtime; transcript task/run snapshot | [EXISTING]: old flows retain Community-1. Ready ASR-only reruns can opt out without rewriting old generations. Additional models are [PLANNED_NO_BACKEND]; no invented catalog, language or hardware support. | #260 / PR #278; #261 / PR #279 |
| Pairing and capture | `extension_auth.py`, `extension_routes.py`, `extension_ingest.py`, `extensions/chrome/` | [EXISTING]: endpoint/code/lifetime, paired-device state, last seen, re-pair and revoke remain server-owned. Mic/Tab and Start/Stop/Save remain the capture workflow. A durable raw receipt is distinct from derivative readiness and processing completion. | #254 / PR #270 |
| New Meeting import | Existing upload routes and `web/shell*` create flow | [EXISTING]: popup, accepted types, title/date/language, auto-ASR checkbox and ready-model selection remain intact. Progress reflects real transfer/checking, with cancel/retry. Import settings are separate from Extension stage controls. | #255 / PR #271 |
| Notes editing, generation and history | `notes.py`, `notes_routes.py`, `notes_history.py`, `notes_lifecycle.py`, `web/notes*` | [EXISTING]: WYSIWYG Save/Cancel, HTML/plain/email copy, export and provenance remain available. Regeneration creates a new revision. Fixed saved-version links and version-specific copy/export preserve selected content. Old history displays only data actually retained. | #256 / PR #272; #259 / PR #276; #265 / PR #288 |
| Notes Trash | `notes_lifecycle.py`, lifecycle routes and Notes history view | [EXISTING]: eligible older operator Saves can be trashed/restored. Meeting-scoped Empty Trash confirms an exact set before permanent content removal. AI originals and latest Saves are protected. Fixed purged identities remain tombstones. Downloaded copies, backups and storage-level remnants follow server retention policy. | #275 / PR #291 |
| Transcript workspace and history | `transcript_edits.py`, `transcript_history.py`, `web/transcript-core.js` and meeting view | [EXISTING]: seek, search, inline edit, speaker operations, review flags and generation selection remain available. Speaker merge Undo checks affected assignments in the same generation, allowing unrelated safe edits. Review status is separate from automatic flags and source identity. Rerun preserves earlier edits. | #257 / PR #273; #266 / PR #289; visual correction #283 / PR #286 |
| Automatic workflow defaults | `processing.py`, `processing_routes.py`, `web/processing-preferences-core.js` | [EXISTING]: model/effort defaults belong to provider/ASR owners; per-run overrides do not change those defaults. Existing six-field PUT and four-field PATCH remain compatible. Each accepted workflow pins its recipe and source. | #258 / PR #274; stage design #267 / PR #290 |
| Settings layout and footer/About | `web/shell*`, `routes.py` build/config substitution | [EXISTING]: modal navigation, advanced diagnostics, keyboard focus, locale/theme, actual repository/release links and build version are retained. Changing sections must not lose navigation or preserve an unrelated scroll position. | visual correction #284 / PR #287 |

The Extension stage implementation in [#277](https://github.com/nguyencanhtrung/asr-studio/issues/277)
adds a separate authenticated stage endpoint with a shared preference-version check. The
updated client sends only a durably stored negotiated format intent; the server captures the
stage choices and owner defaults once at derivative readiness, after the raw receipt. ASR Off
keeps media and skips dependent work; Notes Off keeps the transcript; diarization Off uses
the server ASR-only path. Unknown capabilities and failed intent persistence block unsafe
creates while preserving local audio. Once a create may have been attempted, its identity and
exact request body remain pinned. Maintained contract/runtime, browser and native capture tests
cover these boundaries; the delivery PR records the measured candidate and test evidence.
Updated Extension code and explicit compatible server activation are both required. Older
clients remain legacy even after saved stage choices change.

## Server and storage boundaries

The application uses FastAPI, SQLite and browser JavaScript modules; the Chrome Extension has its own service worker/offscreen capture and durable local upload state. `auth.py` handles the existing application session; provider account/login state belongs to `chatgpt.py` and `antigravity.py`. Provider routes retain the real `/api/providers/{provider}/login-sessions` lifecycle. ChatGPT callback submission uses `callback_url`; the OMP input route uses its actual login input contract. These are different flows.

`compose.yaml` retains named `data` and `auth` volumes. App/worker model mounts stay read-only at the configured `ASR_MODEL_HOST_PATH`; the dedicated installer writes that host model directory. Installation must occur on the server. The UX delivery does not recreate volumes, move credentials or download model weights into the browser. `processing.py`, `tasks.py`, `worker.py` and the existing installer services retain the durable workflow/task boundaries rather than replacing them with frontend timers.

## Deliberate differences from the prototype

- Provider controls and Settings retain the existing modal workflow and full forms. A compact connected badge cannot replace account, model, effort, test, reauthentication or usage actions.
- Model installation is supported only where the server has a real installer/runtime. Additional diarization catalogs and uninstall are outside this delivery. There is no simulated production download or client-hardware inference.
- Existing import validation remains authoritative: supported `wav/mp3/m4a/mp4/mov/webm/ogg/opus/flac`, maximum 4 GiB and four hours, title maximum 200, optional date and language hint semantics. Deployment settings can impose actual server limits; the frontend must display those rather than silently substitute prototype values.
- Notes and transcript history use retained revision/generation identities. Regeneration does not overwrite operator work. Known legacy citations remain readable even when old per-Save provenance was never journaled.
- The original compact footer layout is intentional. Website/repository/release targets and the version are real application metadata, not design placeholders.

New stage admission starts disabled. The compatible operator CLI reports real task and slot state; maintenance disables admission and waits for worker cancellation to settle before replacement. First upgrade requires the old release's work to drain or be canceled and its processes to stop before new startup performs the additive migration. The acknowledgement flag asserts process compatibility; it does not discover old processes or prove model readiness. No published older binary is claimed as a rollback target when it rejects the new schema.

## Verification and deployment boundaries

Repository fixtures can verify authentication errors, callback state, pairing/revoke behavior, server installation job contracts, upload cancellation, revisions and actual Chrome capture with synthetic audio/provider outputs. They cannot certify real account quota, model quality or downloads, GPU capacity, a physical microphone, or an operator's LAN/Tailscale/public TLS setup. Such checks require the corresponding deployment and consent; synthetic HTTPS is not evidence of a public tunnel working.

The CI Settings demo artifact contains synthetic provider display fixtures; it does not certify
a real provider connection or quota. Its generated PNGs are separate from the raw backend and
native execution evidence.

Generated screenshots, logs, browser traces and SQLite rehearsal copies stay outside Git. Issue/PR verification summaries identify the measured candidate and CI results. Earlier screenshots and measurements remain historical unless unchanged-input equivalence is explicitly verified.

Known optional limitation from #275: an opaque proxy 5xx on a lifecycle POST can display a generic failure rather than a distinct uncertain-outcome state. Reload retrieves server truth; identity, replay, transition and exact-set guards still prevent duplicate lifecycle effects. Do not infer a successful mutation from that error display.
