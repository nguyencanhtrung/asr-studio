"""Read transcript edit revisions and apply generation-scoped speaker and review edits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from asr_studio.tasks import (
    TaskError,
    _connect,
    _connect_read_only,
    _current_generation_for_write,
    _expected_source_version,
    _generation_payload,
    _record_transcript_source_change,
    _request_id,
)
from asr_studio.transcript_history import (
    append_edit,
    edit_history,
    historical_projection,
    track_baseline,
)


def _generation(connection: sqlite3.Connection, meeting_id: str, generation_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT generations.* FROM transcript_generations AS generations "
        "JOIN meetings ON meetings.id = generations.meeting_id "
        "WHERE generations.meeting_id = ? AND generations.id = ? "
        "AND generations.status = 'complete' AND meetings.deletion_requested_at IS NULL",
        (meeting_id, generation_id),
    ).fetchone()
    if row is None:
        raise TaskError(404, "generation_not_found", "Transcript generation not found.")
    return row


def _reviews(connection: sqlite3.Connection, generation_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT segments.id, reviews.checked, reviews.review_version "
        "FROM transcript_segments AS segments JOIN transcript_segment_reviews AS reviews "
        "ON reviews.segment_id = segments.id WHERE segments.generation_id = ?",
        (generation_id,),
    )
    return {
        row["id"]: {"checked": bool(row["checked"]), "review_version": row["review_version"]}
        for row in rows
    }


def read_transcript_edit_history(
    data_dir: Path, meeting_id: str, generation_id: str, *, edit_version: int | None = None
) -> dict[str, object]:
    if edit_version is not None and not 1 <= edit_version <= 2**63 - 1:
        raise TaskError(404, "transcript_edit_not_found", "Transcript edit revision not found.")
    with _connect_read_only(data_dir) as connection:
        connection.execute("BEGIN")
        generation = _generation(connection, meeting_id, generation_id)
        reviews = _reviews(connection, generation_id)
        if edit_version is None:
            return {
                "generation_id": generation_id,
                "revisions": edit_history(connection, generation_id),
                "reviews": reviews,
            }
        payload = _generation_payload(connection, generation)
        for segment in payload["segments"]:
            segment.update(reviews.get(segment["id"], {"checked": False, "review_version": 0}))
        historical = historical_projection(connection, payload, edit_version)
        if historical is None:
            raise TaskError(404, "transcript_edit_not_found", "Transcript edit revision not found.")
        return historical


def _speaker_rows(
    connection: sqlite3.Connection, generation_id: str, source: object, target: object
) -> tuple[str, str]:
    if (
        not isinstance(source, str)
        or not isinstance(target, str)
        or not source
        or not target
        or len(source) > 80
        or len(target) > 80
        or source == target
    ):
        raise TaskError(400, "invalid_speaker_merge", "Choose two distinct transcript speakers.")
    count = connection.execute(
        "SELECT count(*) FROM generation_speakers WHERE generation_id = ? AND speaker_id IN (?, ?)",
        (generation_id, source, target),
    ).fetchone()[0]
    if count != 2:
        raise TaskError(404, "speaker_not_found", "Transcript speaker not found.")
    return source, target


def preview_speaker_merge(
    data_dir: Path, meeting_id: str, generation_id: str, *, source: object, target: object
) -> dict[str, object]:
    with _connect_read_only(data_dir) as connection:
        connection.execute("BEGIN")
        generation = _generation(connection, meeting_id, generation_id)
        version = generation["source_version"]
        _current_generation_for_write(connection, meeting_id, generation_id, version)
        source, target = _speaker_rows(connection, generation_id, source, target)
        count = connection.execute(
            "SELECT count(*) FROM transcript_segments WHERE generation_id = ? AND speaker_id = ?",
            (generation_id, source),
        ).fetchone()[0]
        return {
            "generation_id": generation_id,
            "source_version": version,
            "source_speaker_id": source,
            "target_speaker_id": target,
            "segment_count": count,
        }


def _merge_receipt(generation_id: str, version: int, edit: int, count: int) -> dict[str, object]:
    return {
        "generation_id": generation_id,
        "source_version": version,
        "edit_version": edit,
        "segment_count": count,
    }


def _merge_replay(
    connection: sqlite3.Connection, generation_id: str, request_id: str, digest: str
) -> dict[str, object] | None:
    event = connection.execute(
        "SELECT edit_version, source_version, changes_json, request_hash "
        "FROM transcript_edit_events WHERE generation_id = ? AND request_id = ?",
        (generation_id, request_id),
    ).fetchone()
    if event is None:
        return None
    if digest != event["request_hash"]:
        raise TaskError(
            409, "request_conflict", "A different transcript edit used this request ID."
        )
    return _merge_receipt(
        generation_id,
        event["source_version"],
        event["edit_version"],
        len(json.loads(event["changes_json"])),
    )


def _move_assignments(
    connection: sqlite3.Connection,
    generation_id: str,
    changes: list[dict[str, Any]],
) -> None:
    for change in changes:
        before, after = change["before"], change["after"]
        result = connection.execute(
            "UPDATE transcript_segments SET speaker_id = ?, assignment_version = ? "
            "WHERE generation_id = ? AND id = ? AND speaker_id IS ? AND assignment_version = ?",
            (
                after["speaker_id"],
                after["assignment_version"],
                generation_id,
                change["id"],
                before["speaker_id"],
                before["assignment_version"],
            ),
        )
        if result.rowcount != 1:
            raise TaskError(
                409, "assignment_version_changed", "A speaker assignment changed. Reload to review."
            )


def _assignment_change(
    segment_id: str, previous: str, version: int, next_id: str
) -> dict[str, Any]:
    return {
        "entity": "segment",
        "id": segment_id,
        "before": {"speaker_id": previous, "assignment_version": version},
        "after": {"speaker_id": next_id, "assignment_version": version + 1},
    }


def _merge_digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).hexdigest()


def merge_transcript_speakers(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    *,
    source: object,
    target: object,
    expected_source_version: object,
    expected_count: object,
    request_id: object,
) -> dict[str, object]:
    request_key = _request_id(request_id)
    source_version = _expected_source_version(expected_source_version)
    if type(expected_count) is not int or expected_count < 1:
        raise TaskError(400, "invalid_speaker_merge", "Confirm the number of affected segments.")
    if not isinstance(source, str) or not isinstance(target, str):
        raise TaskError(400, "invalid_speaker_merge", "Choose two distinct transcript speakers.")
    digest = _merge_digest("merge", source_version, source, target, expected_count)
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _generation(connection, meeting_id, generation_id)
        replay = _merge_replay(connection, generation_id, request_key, digest)
        if replay is not None:
            return replay
        _current_generation_for_write(connection, meeting_id, generation_id, source_version)
        source, target = _speaker_rows(connection, generation_id, source, target)
        rows = connection.execute(
            "SELECT id, assignment_version FROM transcript_segments "
            "WHERE generation_id = ? AND speaker_id = ? ORDER BY sequence",
            (generation_id, source),
        ).fetchall()
        if len(rows) != expected_count:
            raise TaskError(409, "speaker_merge_changed", "Preview the affected segments again.")
        changes = [
            _assignment_change(row["id"], source, row["assignment_version"], target) for row in rows
        ]
        _move_assignments(connection, generation_id, changes)
        version, edit = _record_transcript_source_change(
            connection,
            generation_id,
            source_version,
            "speakers_merged",
            changes,
            request_id=request_key,
            request_hash=digest,
        )
        return _merge_receipt(generation_id, version, edit, len(changes))


def undo_transcript_speaker_merge(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    merge_edit_version: int,
    *,
    expected_source_version: object,
    request_id: object,
) -> dict[str, object]:
    request_key = _request_id(request_id)
    source_version = _expected_source_version(expected_source_version)
    if not 1 <= merge_edit_version <= 2**63 - 1:
        raise TaskError(404, "speaker_merge_not_found", "Transcript speaker merge not found.")
    digest = _merge_digest("undo", source_version, merge_edit_version)
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _generation(connection, meeting_id, generation_id)
        replay = _merge_replay(connection, generation_id, request_key, digest)
        if replay is not None:
            return replay
        _current_generation_for_write(connection, meeting_id, generation_id, source_version)
        event = connection.execute(
            "SELECT changes_json FROM transcript_edit_events "
            "WHERE generation_id = ? AND edit_version = ? AND kind = 'speakers_merged'",
            (generation_id, merge_edit_version),
        ).fetchone()
        if event is None:
            raise TaskError(404, "speaker_merge_not_found", "Transcript speaker merge not found.")
        already_undone = connection.execute(
            "SELECT 1 FROM transcript_edit_events WHERE generation_id = ? AND undo_of_version = ?",
            (generation_id, merge_edit_version),
        ).fetchone()
        if already_undone is not None:
            raise TaskError(
                409, "speaker_merge_already_undone", "This speaker merge was already undone."
            )
        changes = [
            _assignment_change(
                item["id"],
                item["after"]["speaker_id"],
                item["after"]["assignment_version"],
                item["before"]["speaker_id"],
            )
            for item in json.loads(event["changes_json"])
        ]
        _move_assignments(connection, generation_id, changes)
        version, edit = _record_transcript_source_change(
            connection,
            generation_id,
            source_version,
            "speaker_merge_undone",
            changes,
            request_id=request_key,
            request_hash=digest,
            undo_of_version=merge_edit_version,
        )
        return _merge_receipt(generation_id, version, edit, len(changes))


def set_transcript_segment_checked(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    segment_id: object,
    *,
    checked: object,
    expected_review_version: object,
) -> dict[str, object]:
    if not isinstance(checked, bool) or type(expected_review_version) is not int:
        raise TaskError(400, "invalid_segment_review", "Provide a review state and its version.")
    # Stored segment IDs can predate the current producer's format.
    if expected_review_version < 0 or not isinstance(segment_id, str) or not segment_id:
        raise TaskError(
            400, "invalid_segment_review", "Provide a valid segment and review version."
        )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        generation = _generation(connection, meeting_id, generation_id)
        current = connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if current is None or current["current_generation_id"] != generation_id:
            raise TaskError(409, "current_generation_changed", "The current transcript changed.")
        review = connection.execute(
            "SELECT coalesce(reviews.checked, 0) AS checked, "
            "coalesce(reviews.review_version, 0) AS review_version "
            "FROM transcript_segments AS segments LEFT JOIN transcript_segment_reviews AS reviews "
            "ON reviews.segment_id = segments.id "
            "WHERE segments.generation_id = ? AND segments.id = ?",
            (generation_id, segment_id),
        ).fetchone()
        if review is None:
            raise TaskError(404, "segment_not_found", "Transcript segment not found.")
        version, prior = review["review_version"], bool(review["checked"])
        if version != expected_review_version:
            raise TaskError(409, "segment_review_changed", "This segment review changed. Reload.")
        if prior != checked:
            connection.execute(
                "INSERT INTO transcript_segment_reviews(segment_id, checked, review_version) "
                "VALUES (?, ?, ?) ON CONFLICT(segment_id) DO UPDATE "
                "SET checked = excluded.checked, review_version = excluded.review_version",
                (segment_id, checked, version + 1),
            )
            source_version = generation["source_version"]
            track_baseline(connection, generation_id, source_version)
            append_edit(
                connection,
                generation_id,
                source_version,
                "segment_review",
                [
                    {
                        "entity": "segment",
                        "id": segment_id,
                        "before": {"checked": prior, "review_version": version},
                    }
                ],
            )
            version += 1
        return {
            "generation_id": generation_id,
            "segment_id": segment_id,
            "checked": checked,
            "review_version": version,
        }
