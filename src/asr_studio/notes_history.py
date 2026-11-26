"""Additive immutable storage for Notes saved versions.

The installer runs inside the caller's migration transaction. It does not import
the application storage module and it does not commit. The existing Notes row
remains the latest projection. SQLite triggers capture generated inserts and
successful legacy saves until the application adds direct history-aware writes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_COLUMNS = "note_id, edit_version, markdown, source_id, origin, saved_at"


def install_notes_history(connection: sqlite3.Connection) -> None:
    """Install the additive table, backfill, and compatibility capture triggers.

    The caller owns the transaction. Each statement uses ``execute`` so a
    migration can roll back the complete install and backfill as one unit.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notes_saved_versions ("
        "note_id TEXT NOT NULL REFERENCES notes_revisions(id) ON DELETE CASCADE, "
        "edit_version INTEGER NOT NULL CHECK(edit_version >= 1), "
        "markdown TEXT NOT NULL, "
        "source_id TEXT NOT NULL REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "origin TEXT NOT NULL CHECK(origin IN ('ai', 'operator', 'legacy')), "
        "saved_at TEXT NOT NULL, "
        "PRIMARY KEY(note_id, edit_version)"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS notes_saved_versions_source_idx "
        "ON notes_saved_versions(source_id, note_id, edit_version)"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS notes_saved_versions_update_guard "
        "BEFORE UPDATE ON notes_saved_versions "
        "BEGIN SELECT RAISE(ABORT, 'notes_saved_versions_are_immutable'); END"
    )
    connection.execute(
        "INSERT INTO notes_saved_versions ("
        f"{_COLUMNS}"
        ") SELECT id, edit_version, markdown, source_snapshot_id, "
        "CASE WHEN edit_version = 1 THEN 'ai' ELSE 'legacy' END, "
        "CASE WHEN edit_version = 1 THEN created_at ELSE updated_at END "
        "FROM notes_revisions AS revisions "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM notes_saved_versions AS saved "
        "WHERE saved.note_id = revisions.id "
        "AND saved.edit_version = revisions.edit_version"
        ")"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS notes_saved_versions_after_insert "
        "AFTER INSERT ON notes_revisions "
        "WHEN NEW.edit_version = 1 "
        "BEGIN "
        "INSERT INTO notes_saved_versions ("
        f"{_COLUMNS}"
        ") VALUES (NEW.id, NEW.edit_version, NEW.markdown, NEW.source_snapshot_id, "
        "'ai', NEW.created_at); "
        "END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS notes_saved_versions_after_save "
        "AFTER UPDATE OF edit_version ON notes_revisions "
        "WHEN NEW.edit_version = OLD.edit_version + 1 "
        "BEGIN "
        "INSERT INTO notes_saved_versions ("
        f"{_COLUMNS}"
        ") VALUES (NEW.id, NEW.edit_version, NEW.markdown, NEW.source_snapshot_id, "
        "'operator', NEW.updated_at); "
        "END"
    )


def _validate_note_id(note_id: object) -> str:
    if not isinstance(note_id, str) or not note_id:
        raise ValueError("note_id must be a non-empty string")
    return note_id


def _validate_edit_version(edit_version: object) -> int:
    if isinstance(edit_version, bool) or not isinstance(edit_version, int) or edit_version < 1:
        raise ValueError("edit_version must be a positive integer")
    return edit_version


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if not isinstance(row, tuple):
        raise TypeError("connection row_factory must return a row or tuple")
    return dict(zip((column[0] for column in cursor.description or ()), row, strict=True))


def list_saved_versions(
    connection: sqlite3.Connection, note_id: object, *, include_markdown: bool = True
) -> list[dict[str, Any]]:
    """List immutable versions, with a metadata-only view for the browser history control."""
    normalized_note_id = _validate_note_id(note_id)
    columns = _COLUMNS if include_markdown else "note_id, edit_version, source_id, origin, saved_at"
    cursor = connection.execute(
        f"SELECT {columns} FROM notes_saved_versions WHERE note_id = ? ORDER BY edit_version",
        (normalized_note_id,),
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def get_saved_version(
    connection: sqlite3.Connection, note_id: object, edit_version: object
) -> dict[str, Any] | None:
    """Return one immutable Notes version, or ``None`` when it is not present."""
    normalized_note_id = _validate_note_id(note_id)
    normalized_edit_version = _validate_edit_version(edit_version)
    cursor = connection.execute(
        f"SELECT {_COLUMNS} FROM notes_saved_versions WHERE note_id = ? AND edit_version = ?",
        (normalized_note_id, normalized_edit_version),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)
