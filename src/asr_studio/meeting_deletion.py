"""Restartable removal of one meeting and its application-managed artifacts."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from asr_studio.extension_ingest import (
    cleanup_extension_session_artifacts,
    session_lock,
)
from asr_studio.storage import database_path

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_TEMP_NAME_RE = re.compile(r"([0-9a-f]{32})-([0-9a-f]{16})\.m4a\Z")
_ACTIVE_TASK_SQL = "'accepted', 'running', 'stopping'"
_DELETE_GUIDANCE = (
    "This meeting has a task that is still active. Cancel it and wait for it to reach a terminal "
    "state before deleting the meeting."
)
_CLEANUP_GUIDANCE = (
    "The meeting could not be deleted yet. The cleanup request is saved and will be retried on "
    "restart. Fix the managed storage issue, then retry deletion."
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOGGER = logging.getLogger(__name__)


class MeetingDeletionError(RuntimeError):
    """An API-safe failure while marking or completing a meeting deletion."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@contextmanager
def _connect(data_dir: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path(data_dir), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def _active_task(connection: sqlite3.Connection, meeting_id: str) -> bool:
    return (
        connection.execute(
            f"SELECT 1 FROM tasks WHERE meeting_id = ? AND status IN ({_ACTIVE_TASK_SQL}) LIMIT 1",
            (meeting_id,),
        ).fetchone()
        is not None
    )


def _mark_meeting_for_deletion(data_dir: Path, meeting_id: str) -> None:
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        meeting = connection.execute(
            "SELECT deletion_requested_at FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise MeetingDeletionError(404, "meeting_not_found", "Meeting not found.")
        if _active_task(connection, meeting_id):
            raise MeetingDeletionError(409, "meeting_task_active", _DELETE_GUIDANCE)
        if meeting["deletion_requested_at"] is None:
            connection.execute(
                "UPDATE meetings SET deletion_requested_at = CURRENT_TIMESTAMP, "
                "deletion_error_code = NULL WHERE id = ? AND deletion_requested_at IS NULL",
                (meeting_id,),
            )
        extension_rows = connection.execute(
            "SELECT sessions.owner_id, sessions.client_recording_id, sessions.id, "
            "sessions.payload_fingerprint FROM recordings "
            "JOIN extension_recording_sessions AS sessions "
            "ON sessions.id = recordings.extension_session_id "
            "WHERE recordings.meeting_id = ?",
            (meeting_id,),
        ).fetchall()
        for row in extension_rows:
            connection.execute(
                "INSERT OR IGNORE INTO extension_recording_tombstones("
                "owner_id, client_recording_id, session_id, payload_fingerprint, state) "
                "VALUES (?, ?, ?, ?, 'deleted')",
                (
                    row["owner_id"],
                    row["client_recording_id"],
                    row["id"],
                    row["payload_fingerprint"],
                ),
            )
            connection.execute(
                "UPDATE extension_recording_sessions SET state = 'canceled', "
                "error_code = 'recording_deleted', "
                "error_message = 'Recording deleted by operator.', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND receipt_at IS NULL",
                (row["id"],),
            )


def _managed_upload_ids(data_dir: Path, meeting_id: str) -> list[str]:
    with _connect(data_dir) as connection:
        meeting = connection.execute(
            "SELECT deletion_requested_at FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise MeetingDeletionError(404, "meeting_not_found", "Meeting not found.")
        if meeting["deletion_requested_at"] is None:
            raise MeetingDeletionError(
                409,
                "meeting_deletion_not_marked",
                "Meeting deletion has not been marked.",
            )
        rows = connection.execute(
            "SELECT uploads.id, uploads.raw_key, uploads.managed_key, recordings.storage_key "
            "FROM recordings JOIN uploads ON uploads.id = recordings.upload_id "
            "WHERE recordings.meeting_id = ?",
            (meeting_id,),
        ).fetchall()
    if not rows:
        with _connect(data_dir) as connection:
            extension_row = connection.execute(
                "SELECT 1 FROM recordings WHERE meeting_id = ? "
                "AND extension_session_id IS NOT NULL LIMIT 1",
                (meeting_id,),
            ).fetchone()
        if extension_row is not None:
            return []
        raise RuntimeError("A marked meeting has no owned recording row")
    upload_ids: list[str] = []
    for row in rows:
        upload_id = row["id"]
        if not isinstance(upload_id, str) or _UPLOAD_ID_RE.fullmatch(upload_id) is None:
            raise RuntimeError("A meeting references an invalid upload id")
        if row["raw_key"] != f"uploads/{upload_id}.part":
            raise RuntimeError("A meeting upload has an unexpected raw key")
        expected_audio_key = f"recordings/{upload_id}.m4a"
        if row["managed_key"] != expected_audio_key or row["storage_key"] != expected_audio_key:
            raise RuntimeError("A meeting recording has an unexpected managed key")
        upload_ids.append(upload_id)
    return upload_ids


def _managed_extension_session_ids(data_dir: Path, meeting_id: str) -> list[str]:
    with _connect(data_dir) as connection:
        meeting = connection.execute(
            "SELECT deletion_requested_at FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise MeetingDeletionError(404, "meeting_not_found", "Meeting not found.")
        if meeting["deletion_requested_at"] is None:
            raise MeetingDeletionError(
                409,
                "meeting_deletion_not_marked",
                "Meeting deletion has not been marked.",
            )
        rows = connection.execute(
            "SELECT extension_session_id FROM recordings WHERE meeting_id = ? "
            "AND extension_session_id IS NOT NULL",
            (meeting_id,),
        ).fetchall()
    session_ids: list[str] = []
    for row in rows:
        session_id = row["extension_session_id"]
        if not isinstance(session_id, str) or _UPLOAD_ID_RE.fullmatch(session_id) is None:
            raise RuntimeError("A meeting references an invalid extension session id")
        session_ids.append(session_id)
    return session_ids


def _open_child_directory(parent_fd: int, name: str) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None


def _open_directory_chain(path: Path) -> int:
    """Resolve the configured root, then open its components without following symlinks."""
    absolute = Path(os.path.realpath(path))
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unlink_owned_files(data_dir: Path, upload_ids: list[str]) -> None:
    root_fd = _open_directory_chain(data_dir)
    directory_fds: dict[str, int | None] = {}
    changed: set[int] = set()
    try:
        # Open all managed directories before unlinking anything. O_NOFOLLOW prevents a
        # substituted directory from redirecting cleanup outside the application data root.
        for directory in ("uploads", "recordings", ".recording-tmp"):
            directory_fds[directory] = _open_child_directory(root_fd, directory)
        uploads_fd = directory_fds["uploads"]
        recordings_fd = directory_fds["recordings"]
        temp_fd = directory_fds[".recording-tmp"]

        for upload_id in upload_ids:
            for directory_fd, filename in (
                (uploads_fd, f"{upload_id}.part"),
                (recordings_fd, f"{upload_id}.m4a"),
            ):
                if directory_fd is None:
                    continue
                try:
                    os.unlink(filename, dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                changed.add(directory_fd)

            if temp_fd is not None:
                for filename in os.listdir(temp_fd):
                    match = _TEMP_NAME_RE.fullmatch(filename)
                    if match is None or match.group(1) != upload_id:
                        continue
                    try:
                        os.unlink(filename, dir_fd=temp_fd)
                    except FileNotFoundError:
                        continue
                    changed.add(temp_fd)

        for descriptor in changed:
            os.fsync(descriptor)
    finally:
        for descriptor in directory_fds.values():
            if descriptor is not None:
                os.close(descriptor)
        os.close(root_fd)


def _record_cleanup_failure(data_dir: Path, meeting_id: str) -> None:
    try:
        with _connect(data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE meetings SET deletion_error_code = 'cleanup_failed' "
                "WHERE id = ? AND deletion_requested_at IS NOT NULL",
                (meeting_id,),
            )
    except sqlite3.Error:
        # The committed deletion marker remains the retry record even if its diagnostic
        # cannot be updated during a concurrent database failure.
        return


def _is_sqlite_busy_or_locked(error: sqlite3.Error) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        return (error_code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).casefold()
    return message.startswith(
        (
            "database is busy",
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def _finalize_database_delete(data_dir: Path, meeting_id: str) -> None:
    with _connect(data_dir) as connection:
        # There is a deliberate FK cycle: notes task -> notes snapshot -> transcript generation
        # -> task. Disable checks only inside this immediate transaction, delete every dependent
        # row below, and verify the whole database before committing the final meeting removal.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            meeting = connection.execute(
                "SELECT deletion_requested_at FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            if meeting is None:
                connection.commit()
                return
            if meeting["deletion_requested_at"] is None:
                raise RuntimeError("Meeting deletion mark disappeared before finalization")
            if _active_task(connection, meeting_id):
                raise MeetingDeletionError(409, "meeting_task_active", _DELETE_GUIDANCE)

            recording_rows = connection.execute(
                "SELECT upload_id, extension_session_id FROM recordings WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchall()
            upload_ids = [
                row["upload_id"] for row in recording_rows if row["upload_id"] is not None
            ]
            extension_session_ids = [
                row["extension_session_id"]
                for row in recording_rows
                if row["extension_session_id"] is not None
            ]
            connection.execute("DELETE FROM notes_revisions WHERE meeting_id = ?", (meeting_id,))
            connection.execute("DELETE FROM notes_sources WHERE meeting_id = ?", (meeting_id,))
            connection.execute(
                "DELETE FROM transcript_segments WHERE generation_id IN "
                "(SELECT id FROM transcript_generations WHERE meeting_id = ?)",
                (meeting_id,),
            )
            connection.execute(
                "DELETE FROM generation_speakers WHERE generation_id IN "
                "(SELECT id FROM transcript_generations WHERE meeting_id = ?)",
                (meeting_id,),
            )
            connection.execute(
                "UPDATE meetings SET current_generation_id = NULL WHERE id = ?", (meeting_id,)
            )
            connection.execute(
                "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE active_task_id IN (SELECT id FROM tasks WHERE meeting_id = ?)",
                (meeting_id,),
            )
            connection.execute(
                "DELETE FROM transcript_generations WHERE meeting_id = ?", (meeting_id,)
            )
            connection.execute("DELETE FROM tasks WHERE meeting_id = ?", (meeting_id,))
            connection.execute("DELETE FROM recordings WHERE meeting_id = ?", (meeting_id,))
            connection.executemany(
                "DELETE FROM uploads WHERE id = ?", [(upload_id,) for upload_id in upload_ids]
            )
            connection.executemany(
                "DELETE FROM extension_recording_chunks WHERE session_id = ?",
                [(session_id,) for session_id in extension_session_ids],
            )
            connection.executemany(
                "DELETE FROM extension_recording_reservations WHERE session_id = ?",
                [(session_id,) for session_id in extension_session_ids],
            )
            connection.executemany(
                "DELETE FROM extension_recording_sessions WHERE id = ?",
                [(session_id,) for session_id in extension_session_ids],
            )
            connection.execute(
                "DELETE FROM meetings WHERE id = ? AND deletion_requested_at IS NOT NULL",
                (meeting_id,),
            )
            if (
                connection.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
                is not None
            ):
                raise RuntimeError("Meeting row remained after final deletion")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Meeting deletion would leave foreign-key violations")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")


def _finish_marked_deletion(data_dir: Path, meeting_id: str) -> dict[str, str]:
    try:
        upload_ids = _managed_upload_ids(data_dir, meeting_id)
        extension_session_ids = _managed_extension_session_ids(data_dir, meeting_id)
        if extension_session_ids:
            if len(extension_session_ids) != 1:
                raise RuntimeError("A meeting has more than one extension recording")
            with session_lock(data_dir, extension_session_ids[0]):
                _unlink_owned_files(data_dir, upload_ids)
                cleanup_extension_session_artifacts(data_dir, extension_session_ids[0])
                _finalize_database_delete(data_dir, meeting_id)
        else:
            _unlink_owned_files(data_dir, upload_ids)
            _finalize_database_delete(data_dir, meeting_id)
    except MeetingDeletionError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as error:
        _record_cleanup_failure(data_dir, meeting_id)
        raise MeetingDeletionError(
            503, "meeting_deletion_cleanup_failed", _CLEANUP_GUIDANCE
        ) from error
    return {"meeting_id": meeting_id, "status": "deleted"}


def delete_meeting(data_dir: Path, meeting_id: str) -> dict[str, str]:
    """Mark and synchronously clean one meeting, retaining its marker on any failure."""
    if not isinstance(meeting_id, str) or not meeting_id:
        raise MeetingDeletionError(400, "invalid_meeting_id", "Provide a meeting id.")
    _mark_meeting_for_deletion(data_dir, meeting_id)
    return _finish_marked_deletion(data_dir, meeting_id)


def recover_pending_meeting_deletions(data_dir: Path) -> list[str]:
    """Retry every committed deletion marker after application startup."""
    try:
        with _connect(data_dir) as connection:
            meeting_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM meetings WHERE deletion_requested_at IS NOT NULL ORDER BY id"
                ).fetchall()
            ]
    except sqlite3.Error as error:
        if not _is_sqlite_busy_or_locked(error):
            raise
        _LOGGER.warning(
            "Deferring pending meeting deletion recovery because SQLite is busy or locked: %s",
            error,
        )
        return []
    completed: list[str] = []
    for meeting_id in meeting_ids:
        try:
            _mark_meeting_for_deletion(data_dir, meeting_id)
            _finish_marked_deletion(data_dir, meeting_id)
            completed.append(meeting_id)
        except MeetingDeletionError as error:
            if error.code != "meeting_task_active":
                _record_cleanup_failure(data_dir, meeting_id)
        except sqlite3.Error as error:
            if not _is_sqlite_busy_or_locked(error):
                raise
            _LOGGER.warning(
                "Deferring pending meeting deletion recovery for %s because SQLite is busy "
                "or locked: %s",
                meeting_id,
                error,
            )
    return completed
