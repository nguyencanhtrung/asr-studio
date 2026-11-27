# Extension stage policy (#277)

Status: maintained design and compatibility contract. The #277 Settings controls and
the Extension's durable capability/create boundary now implement the format-1 client
contract described below. Final joint runtime and native acceptance still belongs to
the integrated backend lane; a saved choice or create receipt never claims processing
success while admission is disabled or uncertain.

The current baseline is main at PR289 (`19ee38f`, #266). The contract is refreshed
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
| New capable Extension recording | Current server has no stage-policy admission. | Choose and persist a recipe only for a future recording, at the ready handoff described below. |

The future default recipe is `{asr: true, diarization: true, notes: true}`. It is
captured once for a new capable Extension recording; preference edits never silently
rewrite an existing workflow. The policy owns stages only. It does not own the browser
checkbox, ASR model, language, Notes provider/account, or credentials.

## Capability negotiation and reverse compatibility

The existing server advertises `resumable_upload` and `raw_receipt` in the capability
response. `extensions/chrome/protocol.js::stagePolicyFromCapabilities` validates the
stage-policy feature separately from endpoint identity. Only a successfully parsed
response with no feature permits legacy admission. Malformed, transport-failed,
unsupported, or known-disabled responses remain fenced knowledge; `draining` is a
known fenced admission, not legacy absence. Contract and format versions must equal
1; future values are unsupported.

The handshake preserves the legacy create shape and pairing/token schema:

1. A valid capability response may advertise a stage-policy feature and a supported
   format, for example `stage_policy: {supported: true, contract_version: 1,
   format_version: 1, admission: {format_version: 1, state: "enabled", reason: null}}`.
   A policy-aware client records the negotiated state in a durable, non-credential
   create intent **before the first create POST**. The persisted intent includes the
   immutable client recording identity, capture epoch, selected compatibility state,
   and the exact request shape to replay. A capable request adds only
   `stage_policy: {format_version: 1}`; the three booleans never cross the create
   boundary. It never stores or changes pairing/token data.
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

Protocol tests cover a valid predecessor response with no feature, a valid
format-known/disabled response, malformed or transport-failed capability lookup, lost
create ACK and reconnect after capability change, exact replay identity, and the
existing unknown-field rejection. They prove no new field is sent to a predecessor
server and no pairing/token schema is changed. The server response for a capable create
must acknowledge `stage_policy: {format_version: 1, intent: {format_version: 1}}`; a
legacy response may omit the field or return explicit format 0 with null intent.

## Recipe, precedence, and truth matrix

Store all three strict booleans even when ASR-off makes downstream choices ineffective.
Keep separate: policy `format_version` (schema meaning), preference `version` (optimistic
concurrency), and workflow `epoch` (execution/source fence). A future additive API may
expose these values, but the existing `/api/processing-preferences` PUT/PATCH contracts
remain unchanged and are not a stage-policy endpoint.

`Run` below means eligible after the existing readiness, source, slot, and epoch checks;
it does not guarantee provider or worker success. `Skip` is a persisted terminal state
projected from the captured recipe; it is distinct from a disabled/unknown admission
fence and never claims generated output.

| ASR | Diarization | Notes | ASR | Diarization | Notes |
| --- | --- | --- | --- | --- | --- |
| off | off | off | Skip: `disabled_by_recipe` | Skip: `dependency_asr_disabled` | Skip: `dependency_asr_disabled` |
| off | off | on | Skip: `disabled_by_recipe` | Skip: `dependency_asr_disabled` | Skip: `dependency_asr_disabled` |
| off | on | off | Skip: `disabled_by_recipe` | Skip: `dependency_asr_disabled` | Skip: `dependency_asr_disabled` |
| off | on | on | Skip: `disabled_by_recipe` | Skip: `dependency_asr_disabled` | Skip: `dependency_asr_disabled` |
| on | off | off | Run no-speaker transcript* | Skip: `disabled_by_recipe` | Skip: `disabled_by_recipe` |
| on | off | on | Run no-speaker transcript* | Skip: `disabled_by_recipe` | Run from that immutable transcript* |
| on | on | off | Run speaker transcript | Run Community-1 | Skip: `disabled_by_recipe` |
| on | on | on | Existing full workflow | Run Community-1 | Run from immutable transcript |

ASR-off is media-only: no automatic ASR or Notes task is created, and a later manual
transcript does not implicitly start Notes. An incomplete capture still requires explicit
source confirmation before media-only completion. Diarization-off is metadata on the
captured ASR mode, not cancellation of ASR. Notes depends on the transcript, not speaker
labels. A future automatic ASR-on/diarization-off path needs its own capability and
selected-model readiness; #261's manual `off.ready` does not establish it.

`Skip` must be distinguished from waiting, failure, cancellation, and capability rejection:

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

This issue makes no schema change. #277 may add canonical policy JSON, policy format and
revision to preferences, plus immutable policy data and terminal reasons to workflows or
steps. Preserve existing ASR/Notes recipe fields and preference revisions. Existing rows
with absent policy fields decode as legacy all-on; malformed or unknown policy versions
fence execution and require attention rather than defaulting to all-on.

Rollback is a scheduling contract, not an attention label. Before replacing binaries,
disable new non-all-on writes and ready admission, stop scheduler/requeue/source-adoption
paths from creating or rearming incompatible work, and fence non-all-on claim/publication
on both app and worker. Drain or cancel accepted/running work with epoch/attempt fences;
release only its slot. A compatible rollback build keeps the additive schema, snapshots,
terminal reasons, format knowledge, and persisted gate, and schedules known all-on work
only. A pre-migration binary that rejects the newer schema is not a rollback target.

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

#277 must add disposable fixtures for all eight recipes, policy persistence and stale writes,
old/new client capability negotiation, receipt pause versus derivative readiness, replay and
lost tabs, interrupted source confirmation, provider-unset/disconnected/no-fallback behavior,
worker/model outage, source/review fences, and rollback drain/cancel/claim gates. Browser
evidence may use native Chrome with fixture HTTPS and synthetic provider responses only.
Those fixtures do not establish real LAN, Tailscale, tunnel, cloud-provider, real-inference,
deployment, or production-network coverage. New toggles must not be presented as successful
until their #277 implementation and acceptance evidence exist.
