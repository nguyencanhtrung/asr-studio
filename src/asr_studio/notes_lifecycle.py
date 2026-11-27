"""Lifecycle state and exact-set operations for immutable Notes versions.

The Notes revision and saved-version tables remain the source of truth for
content.  This module adds a small, content-free lifecycle layer beside them:
active versions have no lifecycle row, trashed versions have a recoverable
marker, and purged versions retain only a tombstone.  Every mutation is one
SQLite transaction and every successful operation is replayable without
reapplying the transition.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from asr_studio.tasks import TaskError

LIFECYCLE_FENCE_ENV = "ASR_NOTES_LIFECYCLE_FENCE"
LIFECYCLE_FAULT_ENV = "ASR_NOTES_LIFECYCLE_FAULT"
TRASHED = "trashed"
PURGED = "purged"
ACTIVE = "active"

_OPERATIONS = ("trash", "restore", "empty")
_DEFAULT_REPLAY_MESSAGE = "This lifecycle operation was previously rejected."


def _database_path(data_dir: Path) -> Path:
    return Path(data_dir) / "app.sqlite3"


@contextmanager
def _connect(data_dir: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(_database_path(data_dir), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def install_notes_lifecycle(connection: sqlite3.Connection) -> None:
    """Install the additive lifecycle tables and v24 history capture columns."""
    connection.execute("DROP TRIGGER IF EXISTS notes_saved_versions_update_guard")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(notes_saved_versions)")}
    if "citations_json" not in columns:
        connection.execute("ALTER TABLE notes_saved_versions ADD COLUMN citations_json TEXT")
    if "actor_role" not in columns:
        connection.execute(
            "ALTER TABLE notes_saved_versions ADD COLUMN actor_role TEXT NOT NULL DEFAULT 'legacy'"
        )
    connection.execute(
        "UPDATE notes_saved_versions SET actor_role = CASE origin "
        "WHEN 'ai' THEN 'ai' WHEN 'operator' THEN 'operator' ELSE 'legacy' END "
        "WHERE actor_role IS NULL OR actor_role = 'legacy'"
    )
    # The old compatibility installer could only store the current projection.  A
    # known AI/operator row can safely inherit the revision's immutable citation
    # set; an unknown legacy snapshot must remain content-agnostic.
    connection.execute(
        "UPDATE notes_saved_versions AS saved SET citations_json = ("
        "SELECT revisions.citations_json FROM notes_revisions AS revisions "
        "WHERE revisions.id = saved.note_id"
        ") WHERE saved.citations_json IS NULL AND saved.origin IN ('ai', 'operator')"
    )
    for trigger in (
        "notes_saved_versions_after_insert",
        "notes_saved_versions_after_save",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.execute(
        "CREATE TRIGGER notes_saved_versions_after_insert "
        "AFTER INSERT ON notes_revisions WHEN NEW.edit_version = 1 BEGIN "
        "INSERT INTO notes_saved_versions(note_id, edit_version, markdown, source_id, origin, "
        "saved_at, citations_json, actor_role) VALUES (NEW.id, NEW.edit_version, NEW.markdown, "
        "NEW.source_snapshot_id, 'ai', NEW.created_at, NEW.citations_json, 'ai'); END"
    )
    connection.execute(
        "CREATE TRIGGER notes_saved_versions_after_save "
        "AFTER UPDATE OF edit_version ON notes_revisions "
        "WHEN NEW.edit_version = OLD.edit_version + 1 BEGIN "
        "INSERT INTO notes_saved_versions(note_id, edit_version, markdown, source_id, origin, "
        "saved_at, citations_json, actor_role) VALUES (NEW.id, NEW.edit_version, NEW.markdown, "
        "NEW.source_snapshot_id, 'operator', NEW.updated_at, NEW.citations_json, 'operator'); END"
    )
    connection.execute(
        "CREATE TRIGGER notes_saved_versions_update_guard "
        "BEFORE UPDATE ON notes_saved_versions "
        "BEGIN SELECT RAISE(ABORT, 'notes_saved_versions_are_immutable'); END"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notes_version_lifecycle ("
        "note_id TEXT NOT NULL REFERENCES notes_revisions(id) ON DELETE CASCADE, "
        "edit_version INTEGER NOT NULL CHECK(edit_version >= 1), "
        "status TEXT NOT NULL CHECK(status IN ('trashed', 'purged')), "
        "transition_id TEXT NOT NULL CHECK(length(transition_id) = 32), "
        "source_id TEXT NOT NULL REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "origin TEXT NOT NULL CHECK(origin IN ('ai', 'operator', 'legacy')), "
        "actor_role TEXT NOT NULL, "
        "saved_at TEXT NOT NULL, "
        "trashed_at TEXT NOT NULL, "
        "purged_at TEXT, "
        "PRIMARY KEY(note_id, edit_version), UNIQUE(transition_id), "
        "CHECK((status = 'trashed' AND purged_at IS NULL) OR "
        "(status = 'purged' AND purged_at IS NOT NULL))"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS notes_version_lifecycle_meeting_idx "
        "ON notes_version_lifecycle(note_id, edit_version, status)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notes_lifecycle_operations ("
        "operation_id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "operation_kind TEXT NOT NULL CHECK(operation_kind IN ('trash', 'restore', 'empty')), "
        "payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64), "
        "outcome TEXT NOT NULL CHECK(outcome IN ('succeeded', 'rejected')), "
        "status_code INTEGER NOT NULL, "
        "code TEXT, "
        "note_id TEXT, "
        "edit_version INTEGER, "
        "transition_id TEXT, "
        "item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS notes_lifecycle_operations_meeting_idx "
        "ON notes_lifecycle_operations(meeting_id, created_at, operation_id)"
    )


def _task_error(status_code: int, code: str, message: str) -> TaskError:
    return TaskError(status_code, code, message)


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise _task_error(400, "invalid_operation_id", "Provide a valid lifecycle operation ID.")
    return value


def _edit_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise _task_error(400, "invalid_edit_version", "Provide a valid saved Notes version.")
    return value


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fenced() -> bool:
    return os.environ.get(LIFECYCLE_FENCE_ENV, "").casefold() in {"1", "true", "yes", "on"}


def _fault(phase: str) -> None:
    if os.environ.get(LIFECYCLE_FAULT_ENV) == phase:
        raise RuntimeError(f"Injected Notes lifecycle failure at {phase}")


def _meeting(connection: sqlite3.Connection, meeting_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, title, deletion_requested_at FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if row is None:
        raise _task_error(404, "meeting_not_found", "Meeting not found.")
    if row["deletion_requested_at"] is not None:
        raise _task_error(
            409,
            "meeting_deletion_pending",
            "This meeting is being deleted. Wait for cleanup to finish.",
        )
    return row


def _check_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    kind: str,
    payload_hash: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM notes_lifecycle_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    if row["operation_kind"] != kind or row["payload_hash"] != payload_hash:
        raise _task_error(
            409,
            "idempotency_conflict",
            "This operation_id was already used with different lifecycle details.",
        )
    return row


def _current_state(
    connection: sqlite3.Connection, note_id: str, edit_version: int
) -> dict[str, object]:
    row = connection.execute(
        "SELECT status, transition_id, source_id, origin, actor_role, saved_at, trashed_at, "
        "purged_at FROM notes_version_lifecycle WHERE note_id = ? AND edit_version = ?",
        (note_id, edit_version),
    ).fetchone()
    if row is None:
        return {
            "status": ACTIVE,
            "content_available": True,
            "can_restore": False,
            "transition_id": None,
        }
    return {
        "status": row["status"],
        "content_available": False,
        "can_restore": row["status"] == TRASHED,
        "transition_id": row["transition_id"],
        "source_id": row["source_id"],
        "origin": row["origin"],
        "actor_role": row["actor_role"],
        "saved_at": row["saved_at"],
        "trashed_at": row["trashed_at"],
        "purged_at": row["purged_at"],
    }


def _result(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    note_id: str | None = None,
    edit_version: int | None = None,
) -> dict[str, object]:
    target_note = note_id if note_id is not None else receipt["note_id"]
    target_version = edit_version if edit_version is not None else receipt["edit_version"]
    current_state = None
    if target_note is not None and target_version is not None:
        current_state = _current_state(connection, target_note, target_version)
    return {
        "operation_id": receipt["operation_id"],
        "operation": receipt["operation_kind"],
        "historical_outcome": {
            "status": receipt["outcome"],
            "code": receipt["code"],
            "at": receipt["completed_at"],
        },
        "target": {
            "note_id": target_note,
            "edit_version": target_version,
            "item_count": receipt["item_count"],
        },
        "current_state": current_state,
    }


def _replay_or_none(
    connection: sqlite3.Connection,
    operation_id: str,
    kind: str,
    payload_hash: str,
) -> dict[str, object] | None:
    receipt = _check_operation(connection, operation_id, kind, payload_hash)
    if receipt is None:
        return None
    if receipt["outcome"] != "succeeded":
        raise _task_error(
            int(receipt["status_code"]),
            receipt["code"] or "lifecycle_operation_rejected",
            _DEFAULT_REPLAY_MESSAGE,
        )
    return _result(connection, receipt)


def _insert_receipt(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    meeting_id: str,
    kind: str,
    payload_hash: str,
    note_id: str | None,
    edit_version: int | None,
    transition_id: str | None,
    item_count: int,
    outcome: str = "succeeded",
    status_code: int = 200,
    code: str | None = None,
) -> sqlite3.Row:
    connection.execute(
        "INSERT INTO notes_lifecycle_operations(operation_id, meeting_id, operation_kind, "
        "payload_hash, outcome, status_code, code, note_id, edit_version, transition_id, "
        "item_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            meeting_id,
            kind,
            payload_hash,
            outcome,
            status_code,
            code,
            note_id,
            edit_version,
            transition_id,
            item_count,
        ),
    )
    row = connection.execute(
        "SELECT * FROM notes_lifecycle_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    assert row is not None
    return row


def _reject(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    meeting_id: str,
    kind: str,
    payload_hash: str,
    error: TaskError,
    note_id: str | None,
    edit_version: int | None,
    transition_id: str | None,
    item_count: int,
) -> None:
    _insert_receipt(
        connection,
        operation_id=operation_id,
        meeting_id=meeting_id,
        kind=kind,
        payload_hash=payload_hash,
        note_id=note_id,
        edit_version=edit_version,
        transition_id=transition_id,
        item_count=item_count,
        outcome="rejected",
        status_code=error.status_code,
        code=error.code,
    )
    connection.commit()
    raise error


def _saved_row(
    connection: sqlite3.Connection, note_id: str, edit_version: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT saved.note_id, saved.edit_version, saved.markdown, saved.source_id, saved.origin, "
        "saved.saved_at, saved.citations_json, saved.actor_role, revisions.meeting_id, "
        "revisions.edit_version AS latest_edit_version, lifecycle.status, lifecycle.transition_id, "
        "lifecycle.trashed_at, lifecycle.purged_at "
        "FROM notes_saved_versions AS saved "
        "JOIN notes_revisions AS revisions ON revisions.id = saved.note_id "
        "LEFT JOIN notes_version_lifecycle AS lifecycle ON lifecycle.note_id = saved.note_id "
        "AND lifecycle.edit_version = saved.edit_version "
        "WHERE saved.note_id = ? AND saved.edit_version = ?",
        (note_id, edit_version),
    ).fetchone()


def _validate_version_scope(
    connection: sqlite3.Connection, meeting_id: str, note_id: str, edit_version: int
) -> sqlite3.Row:
    """Validate ownership and durable version identity before receipt lookup.

    A purged version has no saved body, so the lifecycle marker is deliberately
    sufficient for replay scope.  A revision from another meeting is treated as
    ordinary absence and cannot disclose an operation receipt.
    """
    row = connection.execute(
        "SELECT revisions.meeting_id, revisions.id, saved.edit_version AS saved_version, "
        "lifecycle.edit_version AS lifecycle_version FROM notes_revisions AS revisions "
        "LEFT JOIN notes_saved_versions AS saved ON saved.note_id = revisions.id "
        "AND saved.edit_version = ? LEFT JOIN notes_version_lifecycle AS lifecycle "
        "ON lifecycle.note_id = revisions.id AND lifecycle.edit_version = ? "
        "WHERE revisions.id = ?",
        (edit_version, edit_version, note_id),
    ).fetchone()
    if (
        row is None
        or row["meeting_id"] != meeting_id
        or (row["saved_version"] is None and row["lifecycle_version"] is None)
    ):
        raise _task_error(404, "notes_version_not_found", "Saved Notes version not found.")
    return row


def _validate_item_scopes(
    connection: sqlite3.Connection, meeting_id: str, items: Sequence[dict[str, object]]
) -> None:
    for item in items:
        _validate_version_scope(
            connection,
            meeting_id,
            str(item["note_id"]),
            int(item["edit_version"]),
        )


def _eligible(row: sqlite3.Row | None) -> bool:
    return bool(
        row is not None
        and row["origin"] == "operator"
        and row["edit_version"] > 1
        and row["edit_version"] < row["latest_edit_version"]
    )


def _trash_rows(connection: sqlite3.Connection, meeting_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT lifecycle.note_id, lifecycle.edit_version, lifecycle.transition_id, "
        "lifecycle.source_id, lifecycle.origin, lifecycle.actor_role, lifecycle.saved_at, "
        "lifecycle.trashed_at FROM notes_version_lifecycle AS lifecycle "
        "JOIN notes_revisions AS revisions ON revisions.id = lifecycle.note_id "
        "JOIN notes_saved_versions AS saved ON saved.note_id = lifecycle.note_id "
        "AND saved.edit_version = lifecycle.edit_version "
        "WHERE revisions.meeting_id = ? AND lifecycle.status = 'trashed' "
        "AND saved.origin = 'operator' AND lifecycle.edit_version > 1 "
        "AND lifecycle.edit_version < revisions.edit_version "
        "ORDER BY lifecycle.note_id, lifecycle.edit_version",
        (meeting_id,),
    ).fetchall()


def _item(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    return {
        "note_id": row["note_id"],
        "edit_version": row["edit_version"],
        "transition_id": row["transition_id"],
        "source_id": row["source_id"],
        "origin": row["origin"],
        "actor_role": row["actor_role"],
        "saved_at": row["saved_at"],
        "trashed_at": row["trashed_at"],
    }


def _digest(items: Sequence[dict[str, object]]) -> str:
    identities = [
        {
            "note_id": item["note_id"],
            "edit_version": item["edit_version"],
            "transition_id": item["transition_id"],
        }
        for item in items
    ]
    encoded = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _task_error(400, "invalid_trash_confirmation", "Provide the confirmed trash set.")
    items: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"note_id", "edit_version", "transition_id"}:
            raise _task_error(
                400, "invalid_trash_confirmation", "The confirmed trash set is invalid."
            )
        note_id = raw["note_id"]
        transition_id = raw["transition_id"]
        version = _edit_version(raw["edit_version"])
        if (
            not isinstance(note_id, str)
            or not note_id
            or not isinstance(transition_id, str)
            or not transition_id
        ):
            raise _task_error(
                400, "invalid_trash_confirmation", "The confirmed trash set is invalid."
            )
        key = (note_id, version, transition_id)
        if key in seen:
            raise _task_error(
                400, "invalid_trash_confirmation", "The confirmed trash set contains duplicates."
            )
        seen.add(key)
        items.append({"note_id": note_id, "edit_version": version, "transition_id": transition_id})
    return sorted(
        items,
        key=lambda item: (
            str(item["note_id"]),
            int(item["edit_version"]),
            str(item["transition_id"]),
        ),
    )


def list_trash(data_dir: Path, meeting_id: str) -> dict[str, object]:
    with _connect(data_dir) as connection:
        meeting = _meeting(connection, meeting_id)
        rows = _trash_rows(connection, meeting_id)
        items = [_item(row) for row in rows]
    return {
        "meeting_id": meeting_id,
        "meeting_title": meeting["title"],
        "count": len(items),
        "digest": _digest(items),
        "items": items,
    }


def trash_version(
    data_dir: Path,
    meeting_id: str,
    *,
    operation_id: object,
    note_id: object,
    edit_version: object,
) -> dict[str, object]:
    operation_id = _operation_id(operation_id)
    if not isinstance(note_id, str) or not note_id:
        raise _task_error(400, "invalid_note_id", "Provide a Notes revision ID.")
    edit_version = _edit_version(edit_version)
    payload = {"meeting_id": meeting_id, "note_id": note_id, "edit_version": edit_version}
    payload_hash = _payload_hash(payload)
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _meeting(connection, meeting_id)
        _validate_version_scope(connection, meeting_id, note_id, edit_version)
        replay = _replay_or_none(connection, operation_id, "trash", payload_hash)
        if replay is not None:
            return replay
        if _fenced():
            error = _task_error(
                503, "notes_lifecycle_fenced", "Notes lifecycle changes are temporarily fenced."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="trash",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=None,
                item_count=1,
            )
        row = _saved_row(connection, note_id, edit_version)
        if row is None or row["meeting_id"] != meeting_id:
            error = _task_error(404, "notes_version_not_found", "Saved Notes version not found.")
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="trash",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=None,
                item_count=1,
            )
        if row["origin"] == "legacy":
            error = _task_error(
                409,
                "notes_version_not_eligible",
                "This legacy Notes snapshot has no confirmed Save history.",
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="trash",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=None,
                item_count=1,
            )
        if row["edit_version"] == 1 or row["edit_version"] == row["latest_edit_version"]:
            error = _task_error(
                409, "notes_version_protected", "The AI original and latest Save are protected."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="trash",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=None,
                item_count=1,
            )
        if row["status"] is not None:
            error = _task_error(
                409,
                "notes_version_already_lifecycle_marked",
                "This Notes version already has a lifecycle state.",
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="trash",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=row["transition_id"],
                item_count=1,
            )
        transition_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO notes_version_lifecycle(note_id, edit_version, status, transition_id, "
            "source_id, origin, actor_role, saved_at, trashed_at) VALUES "
            "(?, ?, 'trashed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                note_id,
                edit_version,
                transition_id,
                row["source_id"],
                row["origin"],
                row["actor_role"],
                row["saved_at"],
            ),
        )
        _fault("after_trash_marker")
        receipt = _insert_receipt(
            connection,
            operation_id=operation_id,
            meeting_id=meeting_id,
            kind="trash",
            payload_hash=payload_hash,
            note_id=note_id,
            edit_version=edit_version,
            transition_id=transition_id,
            item_count=1,
        )
        _fault("before_commit")
        connection.commit()
        return _result(connection, receipt, note_id=note_id, edit_version=edit_version)


def restore_version(
    data_dir: Path,
    meeting_id: str,
    *,
    operation_id: object,
    note_id: object,
    edit_version: object,
    transition_id: object,
) -> dict[str, object]:
    operation_id = _operation_id(operation_id)
    if (
        not isinstance(note_id, str)
        or not note_id
        or not isinstance(transition_id, str)
        or not transition_id
    ):
        raise _task_error(
            400, "invalid_restore_target", "Provide the Notes version and trash transition."
        )
    edit_version = _edit_version(edit_version)
    payload = {
        "meeting_id": meeting_id,
        "note_id": note_id,
        "edit_version": edit_version,
        "transition_id": transition_id,
    }
    payload_hash = _payload_hash(payload)
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _meeting(connection, meeting_id)
        _validate_version_scope(connection, meeting_id, note_id, edit_version)
        replay = _replay_or_none(connection, operation_id, "restore", payload_hash)
        if replay is not None:
            return replay
        if _fenced():
            error = _task_error(
                503, "notes_lifecycle_fenced", "Notes lifecycle changes are temporarily fenced."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="restore",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=transition_id,
                item_count=1,
            )
        row = _saved_row(connection, note_id, edit_version)
        lifecycle = connection.execute(
            "SELECT * FROM notes_version_lifecycle WHERE note_id = ? AND edit_version = ?",
            (note_id, edit_version),
        ).fetchone()
        if lifecycle is None:
            error = _task_error(
                409, "notes_version_not_trashed", "This Notes version is not currently in Trash."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="restore",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=transition_id,
                item_count=1,
            )
        if lifecycle["status"] == PURGED:
            error = _task_error(
                410, "notes_version_purged", "This Notes version was permanently purged."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="restore",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=transition_id,
                item_count=1,
            )
        if row is None or row["meeting_id"] != meeting_id:
            error = _task_error(
                410, "notes_version_purged", "This Notes version was permanently purged."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="restore",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=transition_id,
                item_count=1,
            )
        if lifecycle["transition_id"] != transition_id:
            error = _task_error(
                409, "notes_transition_changed", "This Trash transition is no longer current."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="restore",
                payload_hash=payload_hash,
                error=error,
                note_id=note_id,
                edit_version=edit_version,
                transition_id=transition_id,
                item_count=1,
            )
        connection.execute(
            "DELETE FROM notes_version_lifecycle WHERE note_id = ? AND edit_version = ? "
            "AND status = 'trashed' AND transition_id = ?",
            (note_id, edit_version, transition_id),
        )
        _fault("after_restore_marker")
        receipt = _insert_receipt(
            connection,
            operation_id=operation_id,
            meeting_id=meeting_id,
            kind="restore",
            payload_hash=payload_hash,
            note_id=note_id,
            edit_version=edit_version,
            transition_id=transition_id,
            item_count=1,
        )
        _fault("before_commit")
        connection.commit()
        return _result(connection, receipt, note_id=note_id, edit_version=edit_version)


def empty_trash(
    data_dir: Path,
    meeting_id: str,
    *,
    operation_id: object,
    confirmation_digest: object,
    items: object,
) -> dict[str, object]:
    operation_id = _operation_id(operation_id)
    if not isinstance(confirmation_digest, str) or len(confirmation_digest) != 64:
        raise _task_error(400, "invalid_trash_confirmation", "Provide the confirmed Trash digest.")
    normalized_items = _normalize_items(items)
    payload = {
        "meeting_id": meeting_id,
        "confirmation_digest": confirmation_digest,
        "items": normalized_items,
    }
    payload_hash = _payload_hash(payload)
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _meeting(connection, meeting_id)
        _validate_item_scopes(connection, meeting_id, normalized_items)
        replay = _replay_or_none(connection, operation_id, "empty", payload_hash)
        if replay is not None:
            return replay
        if _fenced():
            error = _task_error(
                503, "notes_lifecycle_fenced", "Notes lifecycle changes are temporarily fenced."
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="empty",
                payload_hash=payload_hash,
                error=error,
                note_id=None,
                edit_version=None,
                transition_id=None,
                item_count=len(normalized_items),
            )
        if _digest(normalized_items) != confirmation_digest:
            error = _task_error(
                409,
                "trash_confirmation_stale",
                "Confirm the current Trash set again before emptying it.",
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="empty",
                payload_hash=payload_hash,
                error=error,
                note_id=None,
                edit_version=None,
                transition_id=None,
                item_count=len(normalized_items),
            )
        current = [_item(row) for row in _trash_rows(connection, meeting_id)]
        current_keys = [
            {key: item[key] for key in ("note_id", "edit_version", "transition_id")}
            for item in current
        ]
        if current_keys != normalized_items or _digest(current) != confirmation_digest:
            error = _task_error(
                409,
                "trash_confirmation_stale",
                "The Trash list changed. Review and confirm it again.",
            )
            _reject(
                connection,
                operation_id=operation_id,
                meeting_id=meeting_id,
                kind="empty",
                payload_hash=payload_hash,
                error=error,
                note_id=None,
                edit_version=None,
                transition_id=None,
                item_count=len(normalized_items),
            )
        for item in normalized_items:
            row = _saved_row(connection, str(item["note_id"]), int(item["edit_version"]))
            lifecycle = connection.execute(
                "SELECT status, transition_id FROM notes_version_lifecycle "
                "WHERE note_id = ? AND edit_version = ?",
                (item["note_id"], item["edit_version"]),
            ).fetchone()
            if (
                not _eligible(row)
                or lifecycle is None
                or lifecycle["status"] != TRASHED
                or lifecycle["transition_id"] != item["transition_id"]
            ):
                error = _task_error(
                    409,
                    "trash_confirmation_stale",
                    "The Trash list changed. Review and confirm it again.",
                )
                _reject(
                    connection,
                    operation_id=operation_id,
                    meeting_id=meeting_id,
                    kind="empty",
                    payload_hash=payload_hash,
                    error=error,
                    note_id=None,
                    edit_version=None,
                    transition_id=None,
                    item_count=len(normalized_items),
                )
        for index, item in enumerate(normalized_items):
            connection.execute(
                "DELETE FROM notes_saved_versions WHERE note_id = ? AND edit_version = ?",
                (item["note_id"], item["edit_version"]),
            )
            connection.execute(
                "UPDATE notes_version_lifecycle SET status = 'purged', "
                "purged_at = CURRENT_TIMESTAMP WHERE note_id = ? AND edit_version = ? "
                "AND status = 'trashed' AND transition_id = ?",
                (item["note_id"], item["edit_version"], item["transition_id"]),
            )
            _fault(f"after_purge_item_{index}")
        receipt = _insert_receipt(
            connection,
            operation_id=operation_id,
            meeting_id=meeting_id,
            kind="empty",
            payload_hash=payload_hash,
            note_id=None,
            edit_version=None,
            transition_id=None,
            item_count=len(normalized_items),
        )
        _fault("before_commit")
        connection.commit()
        return _result(connection, receipt)


def lifecycle_for_version(
    connection: sqlite3.Connection, note_id: str, edit_version: int
) -> sqlite3.Row | None:
    """Return lifecycle metadata for a version while the caller owns its read transaction."""
    return connection.execute(
        "SELECT * FROM notes_version_lifecycle WHERE note_id = ? AND edit_version = ?",
        (note_id, edit_version),
    ).fetchone()


def lifecycle_rows_for_note(connection: sqlite3.Connection, note_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM notes_version_lifecycle WHERE note_id = ? ORDER BY edit_version",
        (note_id,),
    ).fetchall()


def saved_version_for_read(
    connection: sqlite3.Connection, note_id: str, edit_version: int
) -> sqlite3.Row | None:
    """Return saved content plus immutable per-save metadata for Notes reads."""
    return _saved_row(connection, note_id, edit_version)
