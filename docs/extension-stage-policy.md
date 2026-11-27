# Extension stage policy (#267, #277)

Status: maintained design and compatibility contract. The #277 backend implementation
includes additive storage, the authenticated versioned stage-preference API, capability
format knowledge, the persisted fail-closed admission gate, and bounded operator cutover
commands. Runtime execution remains fenced until the compatible worker/scheduler cutover
is complete; a disabled gate is not a runtime-readiness claim.

The current baseline is the #277 backend source checkpoint (`d8e33e9`). The contract is refreshed
against the current implementation of [Extension ingest](../src/asr_studio/extension_ingest.py),
[processing](../src/asr_studio/processing.py), [Notes](../src/asr_studio/notes.py), and
[transcript history/review](../src/asr_studio/transcript_edits.py). The current
manual diarization capability is the #261 rerun contract in
[`routes.py`](../src/asr_studio/routes.py); it is not automatic Extension admission.

## Current behavior and proposed boundary

| Input or workflow | Current behavior on main | Future policy after #277 |
| --- | --- | --- |
| File import, auto-ASR checked | The import flow saves media and hands off ASR once, using the meeting model/language. It does not create an automatic Extension Notes workflow. | Unchanged; Extension policy has no authority over file import. |
| File import, auto-ASR unchecked | Media is saved and manual Start remains available. | Unchanged, even when a future Extension ASR choice is on. |
| Browser choice and meeting options | `asr-studio:auto-transcribe`, the selected ASR model, and meeting language remain the import inputs. | Preserve the key, saved choice, model, and language exactly. |
| Existing Extension sessions, old clients, and existing workflows | Existing all-on stages, pinned recipes, attempts, epochs, source fences, and waiting states remain in force. | Decode absent policy fields as legacy all-on; never rewrite a running snapshot from current preferences. |
| New capable Extension recording | A capable create may persist only `stage_policy: {format_version: 1}`; stage booleans are not client input. | Choose and persist the current preference recipe only at the ready handoff described below. The v1 gate currently reports `disabled / execution_not_ready`. |

The future default recipe is `{asr: true, diarization: true, notes: true}`. It is
captured once for a new capable Extension recording; preference edits never silently
rewrite an existing workflow. The policy owns stages only. It does not own the browser
checkbox, ASR model, language, Notes provider/account, or credentials.

## Capability negotiation and reverse compatibility

The existing server advertises `resumable_upload` and `raw_receipt` in the capability
response. The backend now adds `features.stage_policy` with separate format knowledge
and admission:

```json
{
  "contract_version": 1,
  "format_version": 1,
  "supported": true,
  "admission": {
    "state": "disabled",
    "reason": "execution_not_ready",
    "format_version": 1
  }
}
```

The authenticated operator endpoint is `GET`/`PUT
/api/processing-stage-policy/v1`. Its PUT object is strictly exactly
`expected_preference_version`, `format_version`, `asr`, `diarization`, and `notes`.
The response keeps `format_version`, `preference_version`, `execution_epoch` (on a
workflow), and admission as separate concepts. Existing processing Preferences PUT
and PATCH field sets remain unchanged. `extension_ingest._normalized_create_payload`
accepts the optional exact create intent `stage_policy: {format_version: 1}` while an
omitted field retains the old request fingerprint and legacy all-on meaning.

The future handshake must preserve the legacy create shape and pairing/token schema:

1. A valid capability response may advertise a stage-policy feature and a supported
   format, for example `stage_policy: {format_version: 1, admission: "enabled"}`.
   A policy-aware client records the negotiated state in a durable, non-credential
   create intent **before the first create POST**. The persisted intent includes the
   immutable client recording identity, capture epoch, selected compatibility state,
   and the exact request shape to replay. It never stores or changes pairing/token data.
2. A new client may use the legacy shape only when a valid predecessor capability
   response is successfully parsed and is known to lack the feature. A transport
   error, timeout, malformed response, API mismatch, or capability fetch that cannot
   be classified is `unknown`, not feature absence; it must not silently downgrade.
3. After the first POST, a lost ACK, reconnect, or changed capability response replays
   the same persisted identity and exact JSON shape. It does not renegotiate and add or
   remove policy fields. Same identity and payload returns the durable session; a
   conflicting payload remains an idempotency conflict.
4. An old client talking to a new server sends no policy field and is treated as an
   immutable legacy all-on session. The server must not read current stage preferences
   into that old request.
5. A policy-aware rollback server advertises format knowledge separately from admission,
   for example `format_version: 1, admission: "disabled"`. That is `known-but-disabled`,
   not `absent`: new captures wait or show attention and cannot silently become all-on.
   The rollback gate must remain visible to reconciliation, admission, retry, and claim.

Future protocol tests must cover a valid predecessor response with no feature, a valid
format-known/disabled response, malformed or transport-failed capability lookup, lost
create ACK and reconnect after capability change, exact replay identity, and the
existing unknown-field rejection. They must prove no new field is sent to a predecessor
server and no pairing/token schema is changed.

## Operator cutover, drain, and rollback

The persisted admission states are only `enabled` and `disabled`. `draining` is not a
supported persisted mode: drain is the observed combination of active stage tasks and the
processing slot, so an operator cannot accidentally leave a claimable half-draining gate.
Unknown persisted formats or admission values remain disabled and cannot be enabled by the
operator command.

The commands use the current source's `ASR_DATA_DIR` and do not initialize a missing database:

```text
python -m asr_studio.cli stage-policy status --json
python -m asr_studio.cli stage-policy disable --reason binary-replacement --json
python -m asr_studio.cli stage-policy enable --acknowledge-compatible-processes-stopped --json
python -m asr_studio.cli stage-policy rollback --reason incompatible-rollback --json
```

`status` reports `accepted`, `running`, and `stopping` stage tasks, the task occupying the
processing slot and whether that slot belongs to stage-policy work, plus `drained`. `disable`
persists the fence before reconciling accepted/running work: accepted work is canceled and its
own slot is released; running work is asked to stop and remains visible as `stopping` until the
worker settles it. The command never reports `drained: true` while a stage task or occupied slot
remains. Wait for `drained: true` before replacing binaries or cancel only the reported stage
task through the existing worker/operator lifecycle.

The safe ordering is: stop the old app, scheduler, and worker processes; disable the gate and
wait for the actual drain; install/start the compatible current-source app, scheduler, and
worker; then enable with the explicit acknowledgement flag. The acknowledgement is an operator
assertion about process compatibility, not an automatic process probe, and enabling never patches
or claims work for an old binary. `rollback` is the same disable-and-reconcile fence with a
rollback reason. The compatible maintenance runtime is named
`asr-studio-stage-policy-maintenance-v1`; a pre-migration binary that rejects schema v25 is not a
rollback target.

An invalid preference format captured by a capable format-1 session is a durable policy
quarantine, not a legacy all-on workflow and not a valid all-off recipe. Gate toggles, retries,
source/settings adoption, task claim, publication, scheduler admission, and worker recovery
preserve that quarantine. Explicit legacy sessions with create format 0 retain their all-on
compatibility semantics.

## Recipe, precedence, and truth matrix

Store all three strict booleans even when ASR-off makes downstream choices ineffective.
Keep separate: policy `format_version` (schema meaning), preference `version` (optimistic
concurrency), and workflow `epoch` (execution/source fence). A future additive API may
expose these values, but the existing `/api/processing-preferences` PUT/PATCH contracts
remain unchanged and are not a stage-policy endpoint.

`Run` below means eligible after the existing readiness, source, slot, and epoch checks;
it does not guarantee provider or worker success. `Skip` is projected from the
persisted terminal reason in the processing-state response; execution remains fenced
when the admission gate is disabled.

| ASR | Diarization | Notes | Result |
| --- | --- | --- | --- |
| off | off | off | ASR and Notes are skipped; media only |
| off | off | on | ASR and Notes are skipped; media only |
| off | on | off | ASR and Notes are skipped; media only |
| off | on | on | ASR and Notes are skipped; media only |
| on | off | off | ASR-only no-speaker transcript |
| on | off | on | ASR-only transcript, then Notes |
| on | on | off | ASR plus Community-1 transcript |
| on | on | on | Existing full workflow |

ASR-off is media-only: no automatic ASR or Notes task is created, and a later manual
transcript does not implicitly start Notes. An incomplete capture still requires explicit
source confirmation before media-only completion. Diarization-off is metadata on the
captured ASR mode, not cancellation of ASR. Notes depends on the transcript, not speaker
labels. A future automatic ASR-on/diarization-off path needs its own capability and
selected-model readiness; #261's manual `off.ready` does not establish it.

The API projects disabled terminal step reasons as `skipped`; the durable step row
retains its terminal state and explicit `skipped_*` reason. `Skip` must be
distinguished from waiting, failure, cancellation, and capability rejection:

| State | Meaning | Required behavior |
| --- | --- | --- |
| waiting | A captured enabled stage lacks a worker, model, slot, source, or provider. | Keep the captured recipe and retry when the same prerequisite recovers. |
| needs attention | Capability is unsupported/unknown, capture is interrupted, or rollback admission is fenced. | Require explicit confirmation or compatible deployment; do not infer all-on. |
| failed | An accepted attempt or media operation failed. | Preserve receipt/output and use the existing bounded retry; no provider/model fallback. |
| skipped | A captured recipe intentionally disabled the stage. | Create no task and show no generated-output success claim. |
| canceled | Operator cancellation or deletion fence. | Fence late publication and do not rearm the canceled epoch automatically. |

Missing or disconnected selected Notes provider is `provider_unconfigured` or
`provider_unavailable` and remains waiting with zero alternate-provider calls. A
provider error never chooses another provider, model, account, or language.

## Receipt, ready handoff, and idempotency

The ready boundary is intentionally later than a client ACK:

1. Finalize records immutable intent and manifest identity. A `202` or receipt ACK means
   the request is durable or being archived; it is not processing-ready media.
2. The archive lane verifies and persists the raw receipt (`raw_key`, byte count, SHA-256,
   receipt time) before derivative conversion. A durable receipt may survive derivative
   failure and cannot start ASR by itself.
3. Only after derivative metadata is ready does the caller transaction publish derivative
   state, session readiness, the captured recipe/workflow snapshot, and its outbox event.
   This is the `publish_ready_derivative` → `ready_handoff_tx` boundary. Conversion,
   model preparation, worker execution, provider calls, and network work stay outside it.
4. A ready replay keyed by the durable session identity creates at most one workflow and
   one ready event. A conflicting recording identity, finalize manifest, or policy payload
   is rejected without overwriting the receipt or snapshot.
5. Lost tabs and reconnects resume the persisted create intent, chunks, receipt, and
   derivative attempt. They never generate a new client identity, reset an epoch, or
   start processing from an ACK before derivative readiness.
6. Interrupted capture preserves the saved prefix and interruption reason. It waits for
   explicit Save/Cancel/source confirmation; it does not silently convert the prefix to a
   complete all-on workflow. The captured recipe remains the recipe after confirmation.

Existing running workflows retain their pinned ASR/Notes recipe, attempts, epoch, source
snapshot, and task ownership. A policy change can apply only to a new eligible recording
or an explicit, separately fenced operator adoption.

## Transcript, Notes, and review-version boundaries

The automatic source fence must retain the current semantics delivered by #265 and #266:

- Notes saved history keeps each saved revision's fixed identity, immutable Markdown/source
  snapshot, and source citations. A retry reuses the pinned source snapshot instead of
  silently reading a newer transcript.
- Text or speaker edits change transcript `source_version` and keep older Notes snapshots
  immutable. A meeting-date change after a Notes source snapshot is frozen may mark the
  published revision stale, but it does not replace the pinned snapshot.
- The #266 manual Reviewed mark has its own `review_version`. It is review metadata, not a
  transcript text/speaker change and must not increment `source_version` or invalidate a
  Notes source. Automatic publication must not claim that a manual review mark proves a
  new transcript source.

Future admission and publication tests must exercise a date edit between source freeze and
Notes admission, an active Notes task's edit lock, a text/speaker edit, and a review-only
edit. They must assert the correct source/review versions and immutable citation source.

## Additive migration and rollback

Migration v25 is additive. `processing_preferences` stores the policy format and three
booleans; `processing_cutover_gate` stores the format-aware admission state; workflows
and extension sessions store the immutable format marker, and capable sessions retain
the exact validated create-intent JSON. Preserve existing ASR/Notes recipe fields and
preference revisions. Existing rows with absent policy fields decode as legacy all-on;
malformed or unknown policy versions fence execution and require attention rather than
defaulting to all-on. The current persisted gate is `disabled` with reason
`execution_not_ready`.

Rollback is a scheduling contract, not an attention label. Before replacing binaries,
disable new non-all-on writes and ready admission, stop scheduler/requeue/source-adoption
paths from creating or rearming incompatible work, and fence non-all-on claim/publication
on both app and worker. Drain or cancel accepted/running work with epoch/attempt fences;
release only its slot. A compatible rollback build keeps the additive schema, snapshots,
terminal reasons, format knowledge, and persisted gate, and schedules known all-on work
only. A pre-migration binary that rejects the newer schema is not a rollback target.

The disposable migration evidence names the current-source compatible maintenance runtime
`asr-studio-stage-policy-maintenance-v1`: it opens a v24-shaped copy, upgrades it to v25,
retains v1 format knowledge, and leaves admission `disabled / execution_not_ready`. This
name is not an old released binary and does not claim that one can open the newer schema;
the old-source refusal is tested separately after the upgrade.

## Compatibility fixtures and evidence limits

The maintained compatibility baseline is disposable and synthetic:

- `tests/test_extension_ingest.py` and `tests/test_extension_lifecycle.py` cover exact
  create/finalize fields, chunk replay, durable receipt before derivative readiness,
  receipt replay, recovery, and idempotency conflicts.
- `tests/test_extension_interrupted_capture.py` covers a saved prefix waiting for explicit
  start. `tests/test_processing.py` covers legacy preferences, pinned workflows, source
  snapshots, waiting providers, scheduler slot ownership, and Notes publication.
- `tests/test_browser_create_flow.py` covers the import checkbox on/off behavior, remembered
  browser choice, model and language handoff. These tests do not exercise Extension stage
  switches or create Extension workflows.

#277 backend evidence now includes disposable fixtures for all eight recipes, policy persistence and stale writes,
old/new client capability negotiation, receipt pause versus derivative readiness, replay and
lost tabs, interrupted source confirmation, provider-unset/disconnected/no-fallback behavior,
worker/model outage, source/review fences, and rollback drain/cancel/claim gates. Browser
evidence may use native Chrome with fixture HTTPS and synthetic provider responses only.
Those fixtures do not establish real LAN, Tailscale, tunnel, cloud-provider, real-inference,
deployment, or production-network coverage. New toggles must not be presented as successful
until their #277 implementation and acceptance evidence exist.
