# Automatic extension-recording processing

Automatic processing starts when an extension recording reaches the ready handoff. The workflow keeps its own transcript and notes steps. A waiting workflow is not the same as a running manual task, and it does not silently choose another provider.

## Settings

Open Settings, then Devices, then Automatic processing. The settings are explicit:

- transcript language (`en` or `vi`)
- optional notes provider (`chatgpt` or `antigravity`)
- notes output language

The ASR default belongs to the ASR Model panel. The notes model and reasoning effort belong to
the selected provider panel. Automatic Processing reports those committed owner values and does
not render duplicate model or effort selectors. A missing owner value remains unconfigured. A
disconnected provider remains disconnected. The UI does not choose a provider or model.

When no notes provider is selected, the UI explains that the transcript can finish while notes wait. A disconnected provider remains disconnected. The UI does not fall back to another provider or silently choose a model.

The settings read uses `GET /api/processing-preferences`. The legacy `PUT /api/processing-preferences`
contract still accepts exactly these six fields: `asr_choice`, `asr_language`, `notes_provider`,
`notes_model`, `notes_reasoning_effort`, and `notes_output_language`.

The Automatic Processing form uses the additive `PATCH /api/processing-preferences` contract with
`expected_version`, `asr_language`, `notes_provider`, and `notes_output_language`. The server reads
the selected provider's committed model and effort under one provider-store snapshot. It stores
that pair in the workflow defaults without changing provider stores or existing pinned workflows.
A stale version returns an explicit conflict. The response version is retained and is used when a
meeting adopts current settings.

Stage policy is a separate, versioned contract. The authenticated operator reads
`GET /api/processing-stage-policy/v1` and writes exactly
`expected_preference_version`, `format_version`, `asr`, `diarization`, and `notes` with
`PUT /api/processing-stage-policy/v1`. The response exposes policy format,
`preference_version`, and admission separately. The current backend gate is
`disabled` with reason `execution_not_ready`; this is intentionally not a runtime
readiness claim. Existing six-field Preferences PUT and four-field Preferences PATCH
remain unchanged.

The extension capability response exposes the same format knowledge under
`features.stage_policy`, but does not expose Settings booleans. A capable extension
pins only the exact create intent `stage_policy: {format_version: 1}` before its first
recording-session POST. An omitted intent retains the legacy create fingerprint and
legacy all-on semantics; malformed or unsupported intent is rejected rather than
downgraded.

The local operator cutover is separate from the extension preference API. Use the current-source
commands `python -m asr_studio.cli stage-policy status --json`, `disable --reason ...`, and
`enable --acknowledge-compatible-processes-stopped`; `rollback --reason ...` is the disable path
for binary replacement. Stop the old app, scheduler, and worker first, disable and wait for the
reported `drained: true`, then start the compatible current-source processes and enable with the
explicit acknowledgement. Status reports accepted/running/stopping stage work and the actual
processing-slot owner. There is no persisted `draining` mode, and an unknown gate remains
fail-closed. Worker/model/provider readiness is a separate waiting condition after admission;
the gate command does not claim inference readiness or silently choose a fallback.

## Meeting status

The meeting page shows a compact automatic-processing panel with separate Transcript and Notes steps. It translates durable waiting and failure reasons such as worker offline, unavailable model, busy slot, unconfigured provider, unavailable provider, changed recipe, and source confirmation required.

The workflow supports these public actions:

- `POST /api/meetings/{id}/processing/cancel` with `request_id` and `expected_pipeline_epoch`
- `POST /api/meetings/{id}/processing/retry-notes` with `request_id`, `expected_pipeline_epoch`, `source_snapshot_id`, and `confirmed_recipe_version`
- `POST /api/meetings/{id}/processing/apply-current-settings` with `expected_pipeline_epoch` and `expected_preferences_version`
- `POST /api/meetings/{id}/processing/use-current-source` with `expected_pipeline_epoch`, the paired `expected_generation_id`/`expected_source_version` (both present for an existing transcript, or both `null` before the first transcript), `expected_metadata_version`, and `expected_notes_source_version`

The UI reads the current workflow and preference/source versions immediately before adoption. Notes retry creates only a notes attempt. It does not rerun ASR. Cancel fences the workflow and does not restart it. Current-source adoption is explicit and cannot overwrite unconfirmed transcript or metadata edits.

When no current transcript generation exists, current-source adoption is the explicit confirmation of the meeting metadata and Notes-source revisions. It starts a fresh ASR attempt against the original recording, resets the waiting transcription and Notes steps, and creates no Notes snapshot until ASR publishes a real transcript. Existing-transcript adoption keeps the established Notes-only behavior and does not rerun ASR.

A saved prefix of an interrupted capture (`capture_complete: false`) creates its workflow in `needs_attention` with waiting reason `interrupted_recording`. The scheduler skips it until the operator adopts the current source; the UI labels that action **Transcribe recorded part**. The recording keeps `recording.interrupted = {code}` after processing starts.

Automatic ASR records the transcript generation, transcript source, notes-source, and meeting-metadata revisions observed at handoff. It checks those revisions again before admission and publication; a change pauses the workflow for an explicit source choice. After the bounded automatic-admission budget is reached, the scheduler waits for a free processing slot and gives the GUI grace window before admitting another automatic task.

## Frontend state boundary

`processing-core.js` and `processing-preferences-core.js` contain pure reducers. They return data-only effects. The view renders selectors, dispatches user actions, and owns only DOM focus. Request IDs, workflow epochs, meeting IDs, load IDs, and poll IDs make stale replies inert.

The meeting page accepts the additive `processing` object on meeting responses. During rollout, if that object is absent, the core reads the same durable state from `GET /api/meetings/{id}/processing`. If the endpoint returns 404, the meeting remains a legacy/manual meeting and the automatic panel stays hidden.

Polling runs only while an active workflow is visible. Navigation, hidden-page transitions, cancellation, and terminal workflow states cancel or stop the poll. A completed workflow asks the existing meeting-history boundary to refresh the current meeting. The refresh keeps the current page and uses the existing transcript merge rules, so it does not replace local edits with an older response.

When a meeting still has a manual task, a manual transcript generation, or manual Notes status/revisions, the existing history status strip remains authoritative for the transcript and Notes labels. A canceled automatic workflow remains visible as a cancellation warning only when none of those manual results exists; succeeded and deletion-terminal workflows do not take over the strip.

All user-facing core text remains translation data. The UI is tested in Vietnamese and English, with light and dark themes, failed and successful actions, status updates, and focus recovery.

Metadata-only confirmation keeps the processing panel in place while ASR is queued. Its success feedback survives polling for the same workflow epoch and clears when the epoch changes or the user navigates. This avoids replacing the focused control through an unnecessary meeting refresh before any transcript exists.
