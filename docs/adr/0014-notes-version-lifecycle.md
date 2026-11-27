# ADR 0014: Lifecycle states for immutable Notes Saves

Status: accepted

## Context

Notes history is immutable per revision and Save version. Users need to remove an old Save
without changing the AI original, the latest Save, citations, source snapshots or another
meeting. A browser can also keep a fixed version preview open while another tab changes its
lifecycle. Receipts must remain safe to replay after the saved body has been purged.

## Decision

Schema v24 adds an additive `notes_version_lifecycle` marker and
`notes_lifecycle_operations` receipt table beside the existing Notes history. Active versions
have no marker; a trashed version keeps a recoverable marker and a purged version keeps only a
content-free tombstone. Every mutation is one SQLite transaction and is fenced independently
from ordinary Save/publication journaling. The API validates meeting ownership and durable
revision/marker identity before receipt lookup, and exact transition IDs prevent ABA restores.

Trash is meeting-scoped and lists every eligible operator Save from every Notes revision. The
UI exposes only revision/version identity, timestamps, fixed links and transition-bound restore
actions. Moving one Save and emptying the exact current set both require confirmation. Empty
Trash sends a digest plus identity set and the server rechecks both atomically. Fixed copy,
formatted copy, email preview and export re-read the selected version at action time; a
trashed/purged response cannot use cached preview bytes.

Whole-meeting deletion removes lifecycle markers and receipt children in its existing cleanup
transaction. Recordings, transcript sources, downloaded copies, backups and SQLite WAL data
are outside logical-purge guarantees.

## Compatibility and rollback

The migration is additive and keeps foreign keys; verification uses disposable database
copies. A pre-v24 binary refuses a
schema-v24 database. The named compatible rollback build retains the lifecycle-aware read
paths, hidden/purged responses, normal Save/publication journaling and lifecycle tables, while
fencing lifecycle mutations. Re-enabling the feature requires a current schema and an explicit
operator decision; a UI hide flag is not a rollback.

Set `ASR_NOTES_LIFECYCLE_FENCE=1` on every app process in the schema-compatible
fallback runtime before admitting lifecycle changes. The server rejects new lifecycle
mutations; reads, ordinary Save and Notes publication retain their version journal.
Keep schema v24, saved versions, markers, receipts and lifecycle-aware fixed reads/exports.
Do not lower the schema marker or run an unmodified older binary against this database.
A binary rollback needs these compatibility changes backported and verified in that build;
the fence on the current reviewed build is a maintenance fallback, not proof that an older
release is compatible. Roll forward to a verified compatible build before explicitly
removing the fence. Permanently purged bodies are not restored by either operation.

## Consequences

The browser has a small content-free lifecycle state in `notes-core.js` and renders Trash rows
in `notes.js`. Rows can be restored independently across revisions without loading Markdown.
The server owns eligibility, protection, scope, conflict, replay and fault atomicity. Retained
legacy snapshots remain honestly labelled and do not acquire invented history.

Active legacy fixed views retain the revision's already-known citation map when no per-Save
map was captured. This is a read fallback, not a history backfill: origin and actor stay legacy,
missing versions remain missing, and an absent revision map stays empty. Citation rendering
still requires matching saved text. Trashed and purged views remain content-free and never
reach this fallback. New AI/operator Saves continue to use their own captured map.
