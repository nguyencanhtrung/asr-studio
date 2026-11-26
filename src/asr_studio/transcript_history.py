"""Local edit history for the manual changes within a transcript generation."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def install_transcript_history(connection: sqlite3.Connection) -> None:
    """Install additive history and review storage in the caller's migration transaction."""
    connection.execute(
        "CREATE TABLE transcript_edit_events ("
        "generation_id TEXT NOT NULL REFERENCES transcript_generations(id) ON DELETE RESTRICT, "
        "edit_version INTEGER NOT NULL CHECK(edit_version >= 1), "
        "source_version INTEGER NOT NULL CHECK(source_version >= 1), "
        "kind TEXT NOT NULL CHECK(kind IN ('generated', 'legacy', 'segment_text', "
        "'speaker_added', 'speaker_renamed', 'speaker_assignment', 'speakers_merged', "
        "'speaker_merge_undone', 'segment_review')), "
        "changes_json TEXT NOT NULL CHECK(json_valid(changes_json)), "
        "request_id TEXT, request_hash TEXT, undo_of_version INTEGER, "
        "recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "CHECK((request_id IS NULL) = (request_hash IS NULL)), "
        "CHECK((kind = 'speaker_merge_undone') = (undo_of_version IS NOT NULL)), "
        "PRIMARY KEY(generation_id, edit_version), "
        "UNIQUE(generation_id, request_id), "
        "UNIQUE(generation_id, undo_of_version))"
    )
    connection.execute(
        "CREATE TABLE transcript_segment_reviews ("
        "segment_id TEXT PRIMARY KEY REFERENCES transcript_segments(id) ON DELETE RESTRICT, "
        "checked INTEGER NOT NULL CHECK(checked IN (0, 1)), "
        "review_version INTEGER NOT NULL CHECK(review_version >= 1))"
    )


def track_baseline(
    connection: sqlite3.Connection,
    generation_id: str,
    source_version: int,
    *,
    generated: bool = False,
) -> None:
    """Anchor history at publication or at the first tracked change to a legacy transcript."""
    connection.execute(
        "INSERT INTO transcript_edit_events "
        "(generation_id, edit_version, source_version, kind, changes_json) "
        "VALUES (?, 1, ?, ?, '[]') "
        "ON CONFLICT(generation_id, edit_version) DO NOTHING",
        (generation_id, source_version, "generated" if generated else "legacy"),
    )


def append_edit(
    connection: sqlite3.Connection,
    generation_id: str,
    source_version: int,
    kind: str,
    changes: list[dict[str, Any]],
    *,
    request_id: str | None = None,
    request_hash: str | None = None,
    undo_of_version: int | None = None,
) -> int:
    """Record the inverse changes in the same write transaction as the live edit."""
    previous = connection.execute(
        "SELECT max(edit_version) FROM transcript_edit_events WHERE generation_id = ?",
        (generation_id,),
    ).fetchone()[0]
    if previous is None:
        raise RuntimeError("Transcript edit history has no starting revision")
    edit_version = previous + 1
    connection.execute(
        "INSERT INTO transcript_edit_events "
        "(generation_id, edit_version, source_version, kind, changes_json, "
        "request_id, request_hash, undo_of_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            generation_id,
            edit_version,
            source_version,
            kind,
            json.dumps(changes),
            request_id,
            request_hash,
            undo_of_version,
        ),
    )
    return edit_version


def edit_history(connection: sqlite3.Connection, generation_id: str) -> list[dict[str, object]]:
    """Read revision labels without reading edited transcript text."""
    rows = connection.execute(
        "SELECT edit_version, source_version, kind, recorded_at, undo_of_version "
        "FROM transcript_edit_events "
        "WHERE generation_id = ? ORDER BY edit_version DESC",
        (generation_id,),
    )
    return [
        {**dict(row), "actor_role": "operator" if row["edit_version"] > 1 else None} for row in rows
    ]


def historical_projection(
    connection: sqlite3.Connection,
    current: dict[str, Any],
    edit_version: int,
) -> dict[str, Any] | None:
    """Reconstruct an edit revision using inverse changes and the live generation payload."""
    rows = connection.execute(
        "SELECT edit_version, source_version, kind, changes_json FROM transcript_edit_events "
        "WHERE generation_id = ? AND edit_version >= ? ORDER BY edit_version DESC",
        (current["id"], edit_version),
    ).fetchall()
    if not rows or rows[-1]["edit_version"] != edit_version:
        return None

    # Keep the current display ordering and copy only the rows this projection can edit.
    segments = {row["id"]: dict(row) for row in current["segments"]}
    speakers = {row["speaker_id"]: dict(row) for row in current["speakers"]}
    for revision in rows[:-1]:
        for change in reversed(json.loads(revision["changes_json"])):
            projection = segments if change["entity"] == "segment" else speakers
            if change["before"] is None:
                del projection[change["id"]]
            else:
                projection[change["id"]].update(change["before"])
    selected = rows[-1]
    return {
        **current,
        "source_version": selected["source_version"],
        "edit_version": edit_version,
        "segments": list(segments.values()),
        "speakers": list(speakers.values()),
    }
