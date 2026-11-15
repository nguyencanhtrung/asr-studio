"""Durable production ingest for the authenticated ``asr-extension-v1`` API.

The extension session tables own artifact lifecycle and error state.  The
``recordings`` row is only the source-aware library link and derivative
metadata; this module is the single writer for extension artifact transitions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from asr_studio.config import (
    EXTENSION_MAX_CHUNK_BYTES,
    EXTENSION_MAX_CHUNKS,
    EXTENSION_MAX_RECORDING_BYTES,
    EXTENSION_MEDIA_DISK_RESERVE_BYTES,
    EXTENSION_MEDIA_OUTPUT_BYTES,
    EXTENSION_MEDIA_WALL_SECONDS,
    Settings,
    extension_finalize_body_bytes,
)
from asr_studio.extension_auth import ExtensionSessionIdentity
from asr_studio.media import (
    OUTPUT_CONTENT_TYPE,
    MediaProcessingError,
    normalize_extension_media,
)
from asr_studio.service_runtime import acquire_service_lock, write_service_heartbeat
from asr_studio.storage import database_path

SESSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SEQUENCE_RE = re.compile(r"[0-9]+\Z")
_CLIENT_ID_BYTES = 128
_CAPTURE_EPOCH_BYTES = 128
_TITLE_CHARS = 200
_METADATA_BYTES = 16 * 1024
_CHUNK_ERROR_MESSAGE = "The chunk could not be stored safely. Retry the same chunk."
_ALLOWED_MIME = "audio/webm;codecs=opus"
_ALLOWED_METADATA = frozenset(
    {
        "sources",
        "mix",
        "channels",
        "sample_rate",
        "echo_cancellation",
        "noise_suppression",
        "auto_gain_control",
        "tab_monitor",
        "microphone_monitor",
        "mime_type",
        "local_byte_cap",
    }
)
_FORBIDDEN_METADATA_KEYS = frozenset(
    {"device_id", "group_id", "tab_url", "url", "token", "credential", "secret"}
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_WORKER_IDLE_WAIT_SECONDS = 0.25
_WORKER_SQL_RETRY_INITIAL_SECONDS = 0.25
_WORKER_SQL_RETRY_MAX_SECONDS = 2.0
_WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0
LOGGER = logging.getLogger(__name__)


class ExtensionIngestError(RuntimeError):
    """An API-safe failure in session creation, ingest, or publication."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


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


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ExtensionIngestError(
            400, "invalid_request", "The JSON fields are invalid."
        ) from error


def _safe_text(value: object, *, name: str, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(character.isspace() for character in value)
    ):
        raise ExtensionIngestError(400, "invalid_request", f"{name} is invalid.")
    return value


def _safe_sha(value: object, *, name: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ExtensionIngestError(400, "invalid_request", f"{name} must be lowercase SHA-256.")
    return value


def _validate_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _TITLE_CHARS:
        raise ExtensionIngestError(400, "invalid_title", "Title must contain 1 to 200 characters.")
    return value.strip()


def _walk_metadata(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key in _FORBIDDEN_METADATA_KEYS:
                raise ExtensionIngestError(
                    400,
                    "invalid_metadata",
                    "Device, tab, credential, and URL metadata is not accepted.",
                )
            _walk_metadata(child)
    elif isinstance(value, list):
        for child in value:
            _walk_metadata(child)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ExtensionIngestError(400, "invalid_metadata", "The metadata is invalid.")


def _validate_metadata(value: object) -> dict[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _ALLOWED_METADATA:
        raise ExtensionIngestError(400, "invalid_metadata", "The metadata fields are invalid.")
    _walk_metadata(value)
    encoded = _canonical_json(value)
    if len(encoded) > _METADATA_BYTES:
        raise ExtensionIngestError(413, "request_too_large", "The metadata is too large.")
    return value


def _normalized_create_payload(payload: object) -> dict[str, object]:
    required = {"client_recording_id", "capture_epoch", "mime_type", "title", "metadata"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or set(payload) - (required | {"language_hint"})
    ):
        raise ExtensionIngestError(400, "invalid_request", "The JSON fields are invalid.")
    client_recording_id = _safe_text(
        payload["client_recording_id"], name="client_recording_id", max_bytes=_CLIENT_ID_BYTES
    )
    capture_epoch = _safe_text(
        payload["capture_epoch"], name="capture_epoch", max_bytes=_CAPTURE_EPOCH_BYTES
    )
    if payload["mime_type"] != _ALLOWED_MIME:
        raise ExtensionIngestError(
            415, "unsupported_media", "Only negotiated WebM/Opus media is accepted."
        )
    language_hint_omitted = "language_hint" not in payload
    language_hint = payload.get("language_hint")
    if not language_hint_omitted and (
        not isinstance(language_hint, str) or language_hint not in {"en", "vi"}
    ):
        raise ExtensionIngestError(400, "invalid_language_hint", "language_hint must be en or vi.")
    return {
        "client_recording_id": client_recording_id,
        "capture_epoch": capture_epoch,
        "mime_type": _ALLOWED_MIME,
        "title": _validate_title(payload["title"]),
        "metadata": _validate_metadata(payload["metadata"]),
        "language_hint": language_hint,
        "language_hint_omitted": language_hint_omitted,
    }


def _request_fingerprint(payload: dict[str, object], *, language_hint: str) -> str:
    immutable = {
        "client_recording_id": payload["client_recording_id"],
        "capture_epoch": payload["capture_epoch"],
        "mime_type": payload["mime_type"],
        "title": payload["title"],
        "metadata": payload["metadata"],
        "language_hint": language_hint,
        "language_hint_omitted": payload["language_hint_omitted"],
    }
    return hashlib.sha256(_canonical_json(immutable)).hexdigest()


def extension_limits(settings: Settings) -> dict[str, int]:
    max_recording_bytes = min(settings.max_upload_bytes, EXTENSION_MAX_RECORDING_BYTES)
    max_local_bytes = min(max_recording_bytes, settings.extension_max_staging_bytes)
    return {
        "max_chunk_bytes": EXTENSION_MAX_CHUNK_BYTES,
        "max_recording_bytes": max_recording_bytes,
        "max_local_bytes": max_local_bytes,
        "max_chunks": EXTENSION_MAX_CHUNKS,
        "max_duration_seconds": settings.max_recording_seconds,
        "max_finalize_body_bytes": extension_finalize_body_bytes(),
        "max_active_sessions": settings.extension_max_active_sessions,
        "max_global_staging_bytes": max_local_bytes,
        "media_wall_seconds": EXTENSION_MEDIA_WALL_SECONDS,
        "media_output_bytes": EXTENSION_MEDIA_OUTPUT_BYTES,
        "disk_reserve_bytes": EXTENSION_MEDIA_DISK_RESERVE_BYTES,
    }


def _extension_root(data_dir: Path) -> Path:
    root = Path(data_dir) / "extension-staging"
    return root


def _ensure_private_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ExtensionIngestError(507, "storage_unavailable", "Extension storage is unavailable.")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise OSError("managed directory is not private")
        os.chmod(path, 0o700)
    except OSError as error:
        raise ExtensionIngestError(
            507, "storage_unavailable", "Extension storage is unavailable."
        ) from error


def ensure_extension_storage(data_dir: Path) -> None:
    root = Path(data_dir)
    _ensure_private_directory(root)
    for relative in (
        "extension-staging",
        "extension-staging/chunks",
        "extension-staging/.tmp",
        "extension-raw",
        "extension-raw/.tmp",
        "extension-derivatives",
        "extension-derivatives/.tmp",
        "extension-locks",
    ):
        _ensure_private_directory(root / relative)


def _session_dir(data_dir: Path, session_id: str) -> Path:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
    ensure_extension_storage(data_dir)
    path = _extension_root(data_dir) / "chunks" / session_id
    _ensure_private_directory(path)
    return path


def _chunk_path(data_dir: Path, session_id: str, sequence: int) -> Path:
    if isinstance(sequence, bool) or sequence < 0 or SEQUENCE_RE.fullmatch(str(sequence)) is None:
        raise ExtensionIngestError(400, "invalid_sequence", "The chunk sequence is invalid.")
    return _session_dir(data_dir, session_id) / f"{sequence}.chunk"


def _raw_path(data_dir: Path, session_id: str) -> Path:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
    ensure_extension_storage(data_dir)
    return Path(data_dir) / "extension-raw" / f"{session_id}.webm"


def _derivative_path(data_dir: Path, session_id: str) -> Path:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
    ensure_extension_storage(data_dir)
    return Path(data_dir) / "extension-derivatives" / f"{session_id}.m4a"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_directory_chain(path: Path) -> int:
    """Resolve the configured root, then reject symlinks in managed children."""
    absolute = Path(os.path.realpath(os.fspath(path)))
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


def _open_directory_child(parent_descriptor: int, name: str) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError as error:
        raise RuntimeError(f"Missing managed extension directory: {name}") from error


@contextmanager
def _managed_extension_directories(data_dir: Path, session_id: str) -> Iterator[dict[str, int]]:
    root_descriptor = _open_directory_chain(data_dir)
    descriptors = [root_descriptor]
    try:
        staging_descriptor = _open_directory_child(root_descriptor, "extension-staging")
        descriptors.append(staging_descriptor)
        chunks_descriptor = _open_directory_child(staging_descriptor, "chunks")
        descriptors.append(chunks_descriptor)
        session_descriptor = _open_directory_child(chunks_descriptor, session_id)
        descriptors.append(session_descriptor)
        raw_descriptor = _open_directory_child(root_descriptor, "extension-raw")
        descriptors.append(raw_descriptor)
        raw_tmp_descriptor = _open_directory_child(raw_descriptor, ".tmp")
        descriptors.append(raw_tmp_descriptor)
        derivatives_descriptor = _open_directory_child(root_descriptor, "extension-derivatives")
        descriptors.append(derivatives_descriptor)
        derivatives_tmp_descriptor = _open_directory_child(derivatives_descriptor, ".tmp")
        descriptors.append(derivatives_tmp_descriptor)
        yield {
            "chunks": chunks_descriptor,
            "session": session_descriptor,
            "raw": raw_descriptor,
            "raw_tmp": raw_tmp_descriptor,
            "derivatives": derivatives_descriptor,
            "derivatives_tmp": derivatives_tmp_descriptor,
        }
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


@contextmanager
def _managed_extension_temp_directories(data_dir: Path) -> Iterator[dict[str, int]]:
    root_descriptor = _open_directory_chain(data_dir)
    descriptors = [root_descriptor]
    try:
        raw_descriptor = _open_directory_child(root_descriptor, "extension-raw")
        descriptors.append(raw_descriptor)
        raw_tmp_descriptor = _open_directory_child(raw_descriptor, ".tmp")
        descriptors.append(raw_tmp_descriptor)
        derivatives_descriptor = _open_directory_child(root_descriptor, "extension-derivatives")
        descriptors.append(derivatives_descriptor)
        derivatives_tmp_descriptor = _open_directory_child(derivatives_descriptor, ".tmp")
        descriptors.append(derivatives_tmp_descriptor)
        yield {"raw_tmp": raw_tmp_descriptor, "derivatives_tmp": derivatives_tmp_descriptor}
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _reconcile_extension_temp_artifacts(
    data_dir: Path,
) -> dict[str, dict[str, set[str]]]:
    """Enumerate managed temp files once, grouped by their validated session ID."""
    artifacts: dict[str, dict[str, set[str]]] = {}
    with _managed_extension_temp_directories(data_dir) as directories:
        for kind, key in (("raw", "raw_tmp"), ("derivative", "derivatives_tmp")):
            for name in os.listdir(directories[key]):
                if len(name) <= 33 or not name.startswith(".") or name[33] != "-":
                    continue
                session_id = name[1:33]
                if SESSION_ID_RE.fullmatch(session_id) is None:
                    continue
                artifacts.setdefault(session_id, {}).setdefault(kind, set()).add(name)
    return artifacts


def _unlink_directory_entry(directory_descriptor: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return False
    return True


def _remove_directory_entries(
    directory_descriptor: int,
    predicate: Callable[[str], bool],
) -> bool:
    changed = False
    for name in os.listdir(directory_descriptor):
        if predicate(name):
            changed = _unlink_directory_entry(directory_descriptor, name) or changed
    return changed


def _remove_enumerated_extension_temp_artifacts(
    directories: dict[str, int], artifacts: dict[str, set[str]]
) -> None:
    for directory_key, artifact_key in (("raw_tmp", "raw"), ("derivatives_tmp", "derivative")):
        changed = False
        for name in artifacts.get(artifact_key, set()):
            changed = _unlink_directory_entry(directories[directory_key], name) or changed
        if changed:
            os.fsync(directories[directory_key])


def _safe_entry_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
        and Path(value).name == value
    )


def _unlink(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _hash_file(path: Path, *, expected_length: int | None = None) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExtensionIngestError(
            409, "chunk_corrupt", "A stored chunk is unavailable."
        ) from error
    digest = hashlib.sha256()
    total = 0
    try:
        file_stat = os.fstat(descriptor)
        if not os.path.isfile(path) or file_stat.st_size < 1:
            raise ExtensionIngestError(409, "chunk_corrupt", "A stored chunk is unavailable.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                digest.update(block)
                if expected_length is not None and total > expected_length:
                    raise ExtensionIngestError(409, "chunk_corrupt", "A stored chunk is invalid.")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if expected_length is not None and total != expected_length:
        raise ExtensionIngestError(409, "chunk_corrupt", "A stored chunk is invalid.")
    return total, digest.hexdigest()


def _open_session_lock(data_dir: Path, session_id: str, *, blocking: bool = True) -> int:
    import fcntl

    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
    ensure_extension_storage(data_dir)
    path = Path(data_dir) / "extension-locks" / f"{session_id}.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _close_session_lock(descriptor: int) -> None:
    import fcntl

    with suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    with suppress(OSError):
        os.close(descriptor)


@contextmanager
def session_lock(data_dir: Path, session_id: str) -> Iterator[None]:
    descriptor = _open_session_lock(data_dir, session_id)
    try:
        yield
    finally:
        _close_session_lock(descriptor)


def _session_query(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT sessions.*, recordings.id AS recording_id, recordings.meeting_id, "
        "recordings.storage_key AS recording_storage_key, "
        "recordings.content_type AS recording_content_type, "
        "recordings.duration_seconds AS recording_duration_seconds, meetings.deletion_requested_at "
        "FROM extension_recording_sessions AS sessions "
        "LEFT JOIN recordings ON recordings.extension_session_id = sessions.id "
        "LEFT JOIN meetings ON meetings.id = recordings.meeting_id "
        "WHERE sessions.id = ?",
        (session_id,),
    ).fetchone()


def _deleted_error() -> ExtensionIngestError:
    return ExtensionIngestError(
        410,
        "recording_deleted",
        "This recording session was deleted and cannot be recreated.",
    )


def _owned_session(
    connection: sqlite3.Connection,
    identity: ExtensionSessionIdentity,
    session_id: str,
) -> sqlite3.Row:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
    row = _session_query(connection, session_id)
    if row is not None:
        if row["owner_id"] != identity.owner_id:
            raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")
        tombstone = connection.execute(
            "SELECT 1 FROM extension_recording_tombstones WHERE session_id = ? AND owner_id = ?",
            (session_id, identity.owner_id),
        ).fetchone()
        if tombstone is not None or row["deletion_requested_at"] is not None:
            raise _deleted_error()
        return row
    tombstone = connection.execute(
        "SELECT 1 FROM extension_recording_tombstones WHERE session_id = ? AND owner_id = ?",
        (session_id, identity.owner_id),
    ).fetchone()
    if tombstone is not None:
        raise _deleted_error()
    raise ExtensionIngestError(404, "recording_not_found", "Recording session not found.")


def _open_owned_session_lock(
    data_dir: Path, identity: ExtensionSessionIdentity, session_id: str
) -> int:
    # Invalid/foreign identifiers must not create persistent lock files. Callers
    # repeat the ownership/deletion check after acquiring the lifecycle lock.
    with _connect(data_dir) as connection:
        _owned_session(connection, identity, session_id)
    try:
        return _open_session_lock(data_dir, session_id, blocking=False)
    except BlockingIOError:
        raise ExtensionIngestError(
            503, "session_busy", "The recording is busy. Retry this request shortly."
        ) from None


def _session_summary(row: sqlite3.Row, *, backend_id: str, settings: Settings) -> dict[str, object]:
    deleted = row["deletion_requested_at"] is not None
    meeting_url = None
    if row["meeting_id"] is not None and not deleted:
        meeting_url = f"{settings.public_url.rstrip('/')}/meetings/{row['meeting_id']}"
    receipt = None
    if row["receipt_at"] is not None:
        receipt = {
            # The client binds this identity to upload.serverSessionId.  The
            # library recording row is deliberately a separate identity and
            # remains exposed as recording.id below.
            "recording_id": row["id"],
            "backend_id": backend_id,
            "epoch": row["epoch"],
            "chunk_count": row["finalize_chunk_count"],
            "total_bytes": row["raw_bytes"],
            "manifest_hash": row["manifest_hash"],
            "archive_sha256": row["archive_sha256"],
        }
    error = None
    if row["error_code"] is not None:
        error = {"code": row["error_code"], "message": row["error_message"]}
    return {
        "id": row["id"],
        "backend_id": backend_id,
        "state": row["state"],
        "epoch": row["epoch"],
        "next_sequence": row["next_sequence"],
        "next_offset": row["next_offset"],
        "limits": extension_limits(settings),
        "receipt": receipt,
        "meeting_url": meeting_url,
        "processing": {
            "transcript": "waiting",
            "notes": "waiting",
            "waiting_reason": "artifact_not_ready"
            if row["state"] != "ready"
            else "workflow_not_started",
        },
        "error": error,
        "recording": {
            "id": row["recording_id"],
            "state": row["state"],
            "duration_seconds": row["recording_duration_seconds"],
            "content_type": row["recording_content_type"],
            "audio_url": (
                f"/api/meetings/{row['meeting_id']}/audio"
                if row["state"] == "ready" and row["meeting_id"] is not None and not deleted
                else None
            ),
        },
    }


def _retry_unavailable(message: str) -> ExtensionIngestError:
    return ExtensionIngestError(409, "recording_retry_unavailable", message)


def _complete_raw_archive(data_dir: Path, row: sqlite3.Row) -> None:
    """Verify the immutable receipt still names the complete raw archive."""
    session_id = str(row["id"])
    expected_key = f"extension-raw/{session_id}.webm"
    if (
        row["receipt_at"] is None
        or row["raw_key"] != expected_key
        or row["raw_bytes"] is None
        or row["archive_sha256"] is None
    ):
        raise _retry_unavailable("A complete raw recording archive is required before media retry.")
    try:
        actual_bytes, actual_sha256 = _hash_file(
            _raw_path(data_dir, session_id), expected_length=int(row["raw_bytes"])
        )
    except ExtensionIngestError as error:
        raise _retry_unavailable(
            "A complete raw recording archive is required before media retry."
        ) from error
    if actual_bytes != int(row["raw_bytes"]) or actual_sha256 != row["archive_sha256"]:
        raise _retry_unavailable("A complete raw recording archive is required before media retry.")


def retry_recording_session(
    data_dir: Path,
    meeting_id: str,
    *,
    enqueue: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Requeue failed extension media using only its verified raw archive.

    This is an operator-only seam.  The HTTP route is protected by the normal
    web cookie and same-origin middleware; extension bearer routes never call
    this function.  The session lock is shared with publication and deletion,
    so the state is re-read after the raw-file check before being requeued.
    """
    if not isinstance(meeting_id, str) or not meeting_id:
        raise ExtensionIngestError(404, "meeting_not_found", "Meeting not found.")
    with _connect(data_dir) as connection:
        meeting_row = connection.execute(
            "SELECT recordings.extension_session_id "
            "FROM meetings JOIN recordings ON recordings.meeting_id = meetings.id "
            "WHERE meetings.id = ?",
            (meeting_id,),
        ).fetchone()
    if meeting_row is None:
        raise ExtensionIngestError(404, "meeting_not_found", "Meeting not found.")
    session_id = meeting_row["extension_session_id"]
    if session_id is None:
        raise _retry_unavailable("Only extension recordings with a raw archive can be retried.")

    should_enqueue = False
    retry_requested = False
    with session_lock(data_dir, session_id):
        with _connect(data_dir) as connection:
            row = _session_query(connection, session_id)
        if row is None or row["meeting_id"] != meeting_id:
            raise _retry_unavailable("The recording session is outside this meeting scope.")
        if row["deletion_requested_at"] is not None:
            raise _deleted_error()
        state = str(row["state"])
        if state == "canceled":
            raise _retry_unavailable("Canceled recordings cannot be retried.")
        if state == "ready":
            return {
                "meeting_id": meeting_id,
                "recording_state": state,
                "retry_requested": False,
            }
        if state == "failed":
            _complete_raw_archive(data_dir, row)
            should_enqueue = True
            retry_requested = True
            next_state = "finalizing"
        elif state in {"finalizing", "archived"} and row["receipt_at"] is not None:
            _complete_raw_archive(data_dir, row)
            should_enqueue = True
            next_state = state
        else:
            raise _retry_unavailable("The recording is not complete enough for media retry.")

        with _connect(data_dir) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _session_query(connection, session_id)
            if current is None or current["meeting_id"] != meeting_id:
                connection.rollback()
                raise _retry_unavailable("The recording session is outside this meeting scope.")
            if current["deletion_requested_at"] is not None:
                connection.rollback()
                raise _deleted_error()
            if current["state"] == "canceled":
                connection.rollback()
                raise _retry_unavailable("Canceled recordings cannot be retried.")
            if current["state"] == "ready":
                connection.commit()
                return {
                    "meeting_id": meeting_id,
                    "recording_state": "ready",
                    "retry_requested": False,
                }
            receipt_fields = ("raw_key", "raw_bytes", "archive_sha256", "receipt_at", "epoch")
            if any(current[field] != row[field] for field in receipt_fields):
                raise _retry_unavailable("The raw archive changed while retry was being prepared.")
            if current["state"] == "failed":
                # The lock prevents the worker/deleter from changing the file
                # between verification and this durable state transition. Do
                # not hash audio while holding SQLite's global write lock.
                connection.execute(
                    "UPDATE extension_recording_sessions SET state = 'finalizing', "
                    "error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ?",
                    (_now_text(), session_id),
                )
                next_state = "finalizing"
                retry_requested = True
                should_enqueue = True
            elif current["state"] in {"finalizing", "archived"}:
                next_state = str(current["state"])
                should_enqueue = True
            else:
                connection.rollback()
                raise _retry_unavailable("The recording is not complete enough for media retry.")
            connection.commit()
    if should_enqueue and enqueue is not None:
        enqueue(session_id)
    return {
        "meeting_id": meeting_id,
        "recording_state": next_state,
        "retry_requested": retry_requested,
    }


def _check_disk_budget(data_dir: Path, required_bytes: int) -> None:
    try:
        free = shutil.disk_usage(data_dir).free
    except OSError as error:
        raise ExtensionIngestError(
            507, "storage_unavailable", "Extension storage is unavailable."
        ) from error
    if free < required_bytes + EXTENSION_MEDIA_DISK_RESERVE_BYTES:
        raise ExtensionIngestError(
            507,
            "storage_quota",
            "There is not enough local storage to retain this recording safely.",
        )


def _retained_staging_bytes(connection: sqlite3.Connection) -> int:
    chunks = connection.execute(
        "SELECT COALESCE(SUM(length_bytes), 0) FROM extension_recording_chunks"
    ).fetchone()[0]
    reservations = connection.execute(
        "SELECT COALESCE(SUM(reserved_bytes), 0) FROM extension_recording_reservations"
    ).fetchone()[0]
    return int(chunks or 0) + int(reservations or 0)


def create_recording_session(
    data_dir: Path,
    *,
    identity: ExtensionSessionIdentity,
    payload: object,
    backend_id: str,
    settings: Settings,
) -> tuple[dict[str, object], int]:
    normalized = _normalized_create_payload(payload)
    ensure_extension_storage(data_dir)
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tombstone = connection.execute(
            "SELECT 1 FROM extension_recording_tombstones "
            "WHERE owner_id = ? AND client_recording_id = ?",
            (identity.owner_id, normalized["client_recording_id"]),
        ).fetchone()
        if tombstone is not None:
            raise _deleted_error()
        existing = connection.execute(
            "SELECT * FROM extension_recording_sessions "
            "WHERE owner_id = ? AND client_recording_id = ?",
            (identity.owner_id, normalized["client_recording_id"]),
        ).fetchone()
        if existing is not None:
            selected_language = (
                str(existing["language_hint"])
                if normalized["language_hint_omitted"]
                else normalized["language_hint"]
            )
            fingerprint = _request_fingerprint(normalized, language_hint=str(selected_language))
            if fingerprint != existing["payload_fingerprint"]:
                raise ExtensionIngestError(
                    409,
                    "idempotency_conflict",
                    "This client recording id was already used with different details.",
                )
            row = _session_query(connection, existing["id"])
            assert row is not None
            connection.commit()
            return get_recording_session(
                data_dir,
                identity=identity,
                session_id=str(row["id"]),
                backend_id=backend_id,
                settings=settings,
            ), 200

        limits = extension_limits(settings)
        active_count = connection.execute(
            "SELECT COUNT(*) FROM extension_recording_sessions "
            "WHERE state IN ('receiving', 'finalizing', 'archiving', 'archived')"
        ).fetchone()[0]
        if active_count >= limits["max_active_sessions"]:
            raise ExtensionIngestError(
                413,
                "session_limit",
                "The extension has reached its active recording-session limit.",
            )
        if _retained_staging_bytes(connection) >= limits["max_global_staging_bytes"]:
            raise ExtensionIngestError(
                413,
                "staging_quota",
                "The server has reached its retained extension staging quota.",
            )
        _check_disk_budget(data_dir, 1)
        session_id = os.urandom(16).hex()
        meeting_id = os.urandom(16).hex()
        recording_id = os.urandom(16).hex()
        selected_language = (
            "vi" if normalized["language_hint_omitted"] else str(normalized["language_hint"])
        )
        fingerprint = _request_fingerprint(normalized, language_hint=selected_language)
        created_at = _now_text()
        connection.execute(
            "INSERT INTO extension_recording_sessions("
            "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, language_hint, "
            "language_hint_omitted, mime_type, state, epoch, next_sequence, next_offset, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'receiving', 1, 0, 0, ?, ?)",
            (
                session_id,
                identity.owner_id,
                normalized["client_recording_id"],
                normalized["capture_epoch"],
                fingerprint,
                selected_language,
                int(bool(normalized["language_hint_omitted"])),
                normalized["mime_type"],
                created_at,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO meetings(id, title, meeting_date, language_hint, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                meeting_id,
                normalized["title"],
                datetime.now(UTC).date().isoformat(),
                selected_language,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, upload_id, extension_session_id, "
            "storage_key, content_type, duration_seconds, created_at) "
            "VALUES (?, ?, NULL, ?, NULL, NULL, NULL, ?)",
            (recording_id, meeting_id, session_id, created_at),
        )
        row = _session_query(connection, session_id)
        assert row is not None
        connection.commit()
        return _session_summary(row, backend_id=backend_id, settings=settings), 201


def _release_reservation(data_dir: Path, reservation_id: str | None) -> None:
    if reservation_id is None:
        return
    try:
        with _connect(data_dir) as connection, connection:
            connection.execute(
                "DELETE FROM extension_recording_reservations WHERE id = ?",
                (reservation_id,),
            )
    except sqlite3.Error:
        # The durable reservation remains visible to recovery if the request is
        # interrupted while SQLite is unavailable.
        return


@dataclass(slots=True)
class ChunkLease:
    data_dir: Path
    session_id: str
    sequence: int
    offset: int
    sha256: str
    declared_length: int | None
    max_chunk_bytes: int
    max_recording_bytes: int
    max_local_bytes: int
    reservation_id: str | None
    reserved_bytes: int
    identity: ExtensionSessionIdentity
    lock_descriptor: int
    temporary_path: Path | None
    stream: BinaryIO | None
    replay_ack: dict[str, object] | None
    bytes_written: int = 0
    closed: bool = False
    _digest: object = field(default_factory=hashlib.sha256, repr=False)

    def write(self, block: bytes) -> None:
        if self.closed or self.replay_ack is not None:
            return
        if not isinstance(block, bytes):
            block = bytes(block)
        next_size = self.bytes_written + len(block)
        if next_size > self.max_chunk_bytes or (
            self.declared_length is not None and next_size > self.declared_length
        ):
            raise ExtensionIngestError(
                413, "chunk_too_large", "The chunk exceeds its negotiated limit."
            )
        assert self.stream is not None
        self.stream.write(block)
        self._digest.update(block)  # type: ignore[attr-defined]
        self.bytes_written = next_size

    def commit(self) -> dict[str, object]:
        if self.closed:
            raise ExtensionIngestError(409, "chunk_interrupted", _CHUNK_ERROR_MESSAGE)
        if self.replay_ack is not None:
            self.close()
            return self.replay_ack
        if self.stream is None or self.temporary_path is None:
            self.abort()
            raise ExtensionIngestError(409, "chunk_interrupted", _CHUNK_ERROR_MESSAGE)
        if self.bytes_written < 1 or (
            self.declared_length is not None and self.bytes_written != self.declared_length
        ):
            self.abort()
            raise ExtensionIngestError(400, "chunk_length_mismatch", _CHUNK_ERROR_MESSAGE)
        if self._digest.hexdigest() != self.sha256:  # type: ignore[attr-defined]
            self.abort()
            raise ExtensionIngestError(
                400, "chunk_hash_mismatch", "The chunk digest does not match X-Chunk-SHA256."
            )
        try:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.stream = None
            final_path = _chunk_path(self.data_dir, self.session_id, self.sequence)
            os.replace(self.temporary_path, final_path)
            self.temporary_path = None
            _fsync_directory(final_path.parent)
            with _connect(self.data_dir) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _owned_session_connection(connection, self.identity, self.session_id)
                if row["state"] != "receiving" or row["epoch"] != self._epoch:
                    raise ExtensionIngestError(
                        409, "writer_epoch_conflict", "The recording writer epoch changed."
                    )
                if row["next_sequence"] != self.sequence or row["next_offset"] != self.offset:
                    raise ExtensionIngestError(
                        409, "chunk_position_conflict", "The chunk position is no longer current."
                    )
                if row["next_offset"] + self.bytes_written > self.max_recording_bytes:
                    raise ExtensionIngestError(
                        413, "recording_too_large", "The recording exceeds its byte limit."
                    )
                if row["next_offset"] + self.bytes_written > self.max_local_bytes:
                    raise ExtensionIngestError(
                        413, "local_byte_limit", "The recording exceeds its local byte limit."
                    )
                reservation = connection.execute(
                    "SELECT 1 FROM extension_recording_reservations "
                    "WHERE id = ? AND session_id = ? AND sequence = ?",
                    (self.reservation_id, self.session_id, self.sequence),
                ).fetchone()
                if reservation is None:
                    raise ExtensionIngestError(409, "chunk_interrupted", _CHUNK_ERROR_MESSAGE)
                connection.execute(
                    "INSERT INTO extension_recording_chunks("
                    "session_id, sequence, offset_bytes, length_bytes, sha256, storage_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.session_id,
                        self.sequence,
                        self.offset,
                        self.bytes_written,
                        self.sha256,
                        f"extension-staging/chunks/{self.session_id}/{self.sequence}.chunk",
                    ),
                )
                connection.execute(
                    "DELETE FROM extension_recording_reservations WHERE id = ?",
                    (self.reservation_id,),
                )
                connection.execute(
                    "UPDATE extension_recording_sessions SET next_sequence = ?, next_offset = ?, "
                    "updated_at = ? WHERE id = ? AND state = 'receiving' AND epoch = ?",
                    (
                        self.sequence + 1,
                        self.offset + self.bytes_written,
                        _now_text(),
                        self.session_id,
                        self._epoch,
                    ),
                )
                connection.commit()
                ack = {
                    "sequence": self.sequence,
                    "next_sequence": self.sequence + 1,
                    "next_offset": self.offset + self.bytes_written,
                    "epoch": self._epoch,
                }
            self.close()
            return ack
        except BaseException:
            if self.stream is not None:
                with suppress(OSError):
                    self.stream.close()
                self.stream = None
            if self.temporary_path is not None:
                _unlink(self.temporary_path)
                self.temporary_path = None
            else:
                _unlink(_chunk_path(self.data_dir, self.session_id, self.sequence))
            _release_reservation(self.data_dir, self.reservation_id)
            self.close()
            raise

    @property
    def _epoch(self) -> int:
        return int(self._session_epoch)

    _session_epoch: int = 1

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.stream is not None:
            with suppress(OSError):
                self.stream.close()
            self.stream = None
        _close_session_lock(self.lock_descriptor)

    def abort(self) -> None:
        if self.closed:
            return
        if self.stream is not None:
            with suppress(OSError):
                self.stream.close()
            self.stream = None
        if self.temporary_path is not None:
            _unlink(self.temporary_path)
            self.temporary_path = None
        _release_reservation(self.data_dir, self.reservation_id)
        self.close()


def _owned_session_connection(
    connection: sqlite3.Connection, identity: ExtensionSessionIdentity, session_id: str
) -> sqlite3.Row:
    row = _owned_session(connection, identity, session_id)
    return row


def begin_chunk(
    data_dir: Path,
    *,
    identity: ExtensionSessionIdentity,
    session_id: str,
    sequence: int,
    offset: int,
    sha256: str,
    epoch: int,
    declared_length: int | None,
    settings: Settings,
) -> ChunkLease:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ExtensionIngestError(400, "invalid_sequence", "The chunk sequence is invalid.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ExtensionIngestError(400, "invalid_offset", "The chunk offset is invalid.")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ExtensionIngestError(400, "invalid_epoch", "The writer epoch is invalid.")
    sha256 = _safe_sha(sha256, name="X-Chunk-SHA256")
    limits = extension_limits(settings)
    if sequence >= limits["max_chunks"]:
        raise ExtensionIngestError(413, "chunk_count_limit", "The recording has too many chunks.")
    if declared_length is not None and (
        declared_length < 1 or declared_length > limits["max_chunk_bytes"]
    ):
        raise ExtensionIngestError(
            413, "chunk_too_large", "The chunk exceeds its negotiated limit."
        )
    lock_descriptor = _open_owned_session_lock(data_dir, identity, session_id)
    try:
        with _connect(data_dir) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _owned_session(connection, identity, session_id)
            if row["epoch"] != epoch:
                raise ExtensionIngestError(
                    409, "writer_epoch_conflict", "The recording writer epoch changed."
                )
            if row["receipt_at"] is not None:
                raise ExtensionIngestError(
                    409, "session_state_conflict", "The recording is no longer receiving chunks."
                )
            existing = connection.execute(
                "SELECT offset_bytes, length_bytes, sha256, storage_key "
                "FROM extension_recording_chunks WHERE session_id = ? AND sequence = ?",
                (session_id, sequence),
            ).fetchone()
            if existing is not None:
                if (
                    existing["offset_bytes"] != offset
                    or existing["sha256"] != sha256
                    or (declared_length is not None and existing["length_bytes"] != declared_length)
                ):
                    raise ExtensionIngestError(
                        409,
                        "chunk_replay_conflict",
                        "The chunk replay does not match its durable ACK.",
                    )
                stored_path = _chunk_path(data_dir, session_id, sequence)
                actual_length, actual_sha = _hash_file(
                    stored_path, expected_length=existing["length_bytes"]
                )
                if actual_length != existing["length_bytes"] or actual_sha != existing["sha256"]:
                    raise ExtensionIngestError(
                        409, "chunk_corrupt", "The acknowledged chunk is corrupt or missing."
                    )
                lease = ChunkLease(
                    data_dir,
                    session_id,
                    sequence,
                    offset,
                    sha256,
                    declared_length,
                    limits["max_chunk_bytes"],
                    limits["max_recording_bytes"],
                    limits["max_local_bytes"],
                    None,
                    0,
                    identity,
                    lock_descriptor,
                    None,
                    None,
                    {
                        "sequence": sequence,
                        "next_sequence": row["next_sequence"],
                        "next_offset": row["next_offset"],
                        "epoch": row["epoch"],
                    },
                    _session_epoch=int(row["epoch"]),
                )
                return lease
            if row["state"] != "receiving":
                raise ExtensionIngestError(
                    409, "session_state_conflict", "The recording is no longer receiving chunks."
                )
            if row["next_sequence"] != sequence or row["next_offset"] != offset:
                raise ExtensionIngestError(
                    409,
                    "chunk_position_conflict",
                    "Resume from the durable next_sequence and next_offset.",
                    details={
                        "next_sequence": row["next_sequence"],
                        "next_offset": row["next_offset"],
                    },
                )
            requested = declared_length or limits["max_chunk_bytes"]
            if row["next_offset"] + requested > limits["max_recording_bytes"]:
                raise ExtensionIngestError(
                    413, "recording_too_large", "The recording exceeds its byte limit."
                )
            if _retained_staging_bytes(connection) + requested > limits["max_global_staging_bytes"]:
                raise ExtensionIngestError(
                    413, "local_byte_limit", "The recording exceeds its local byte limit."
                )
            _check_disk_budget(data_dir, max(requested, 1))
            session_dir = _session_dir(data_dir, session_id)
            descriptor, name = tempfile.mkstemp(prefix=f".{sequence}-", dir=session_dir)
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "wb")
            reservation_id = os.urandom(16).hex()
            try:
                connection.execute(
                    "INSERT INTO extension_recording_reservations("
                    "id, session_id, sequence, reserved_bytes, temp_name) VALUES (?, ?, ?, ?, ?)",
                    (
                        reservation_id,
                        session_id,
                        sequence,
                        requested,
                        Path(name).name,
                    ),
                )
            except BaseException:
                stream.close()
                _unlink(Path(name))
                raise
            connection.commit()
            return ChunkLease(
                data_dir,
                session_id,
                sequence,
                offset,
                sha256,
                declared_length,
                limits["max_chunk_bytes"],
                limits["max_recording_bytes"],
                limits["max_local_bytes"],
                reservation_id,
                requested,
                identity,
                lock_descriptor,
                Path(name),
                stream,
                None,
                _session_epoch=int(row["epoch"]),
            )
    except BaseException:
        _close_session_lock(lock_descriptor)
        raise


def get_recording_session(
    data_dir: Path,
    *,
    identity: ExtensionSessionIdentity,
    session_id: str,
    backend_id: str,
    settings: Settings,
) -> dict[str, object]:
    with _connect(data_dir) as connection:
        row = _owned_session(connection, identity, session_id)
    if row["receipt_at"] is not None:
        # Historical receipt fields stay immutable. Only verified surviving
        # bytes authorize a client to discard its last local copy on replay.
        # Hash outside SQLite transactions and without waiting for media work.
        try:
            _complete_raw_archive(data_dir, row)
        except ExtensionIngestError:
            raise ExtensionIngestError(
                409,
                "raw_corrupt",
                "The archived audio is missing or corrupt. Keep the local recording.",
            ) from None
        with _connect(data_dir) as connection:
            current = _owned_session(connection, identity, session_id)
        receipt_fields = ("raw_key", "raw_bytes", "archive_sha256", "receipt_at", "epoch")
        if any(current[field] != row[field] for field in receipt_fields):
            raise ExtensionIngestError(409, "receipt_conflict", "The archive receipt changed.")
        row = current
    return _session_summary(row, backend_id=backend_id, settings=settings)


def _validate_finalize_payload(
    payload: object,
    *,
    row: sqlite3.Row,
    settings: Settings,
) -> tuple[dict[str, object], str, str]:
    required = {
        "finalize_id",
        "capture_epoch",
        "epoch",
        "capture_complete",
        "total_bytes",
        "chunk_count",
        "manifest_hash",
        "chunks",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ExtensionIngestError(400, "invalid_request", "The finalize fields are invalid.")
    finalize_id = _safe_text(payload["finalize_id"], name="finalize_id", max_bytes=128)
    if payload["capture_epoch"] != row["capture_epoch"]:
        raise ExtensionIngestError(
            409, "capture_epoch_conflict", "The capture epoch does not match the session."
        )
    if isinstance(payload["epoch"], bool) or not isinstance(payload["epoch"], int):
        raise ExtensionIngestError(400, "invalid_epoch", "The writer epoch is invalid.")
    if payload["epoch"] != row["epoch"]:
        raise ExtensionIngestError(
            409, "writer_epoch_conflict", "The recording writer epoch changed."
        )
    if payload["capture_complete"] is not True:
        raise ExtensionIngestError(
            409, "capture_incomplete", "Only a complete capture can be finalized."
        )
    total_bytes = payload["total_bytes"]
    chunk_count = payload["chunk_count"]
    if (
        isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes < 1
        or total_bytes > extension_limits(settings)["max_recording_bytes"]
        or isinstance(chunk_count, bool)
        or not isinstance(chunk_count, int)
        or chunk_count < 1
        or chunk_count > extension_limits(settings)["max_chunks"]
    ):
        raise ExtensionIngestError(
            413, "finalize_limit", "The finalize manifest exceeds its negotiated limits."
        )
    manifest_hash = _safe_sha(payload["manifest_hash"], name="manifest_hash")
    chunks = payload["chunks"]
    if not isinstance(chunks, list) or len(chunks) != chunk_count:
        raise ExtensionIngestError(409, "manifest_conflict", "The finalize chunk count is invalid.")
    normalized_chunks: list[dict[str, object]] = []
    lines = ["v1\n"]
    next_offset = 0
    for expected_sequence, item in enumerate(chunks):
        if not isinstance(item, dict) or set(item) != {"sequence", "offset", "length", "sha256"}:
            raise ExtensionIngestError(
                400, "invalid_manifest", "The finalize manifest entries are invalid."
            )
        sequence = item["sequence"]
        offset = item["offset"]
        length = item["length"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != expected_sequence
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset != next_offset
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
            or length > extension_limits(settings)["max_chunk_bytes"]
        ):
            raise ExtensionIngestError(
                409, "manifest_conflict", "The finalize manifest is not contiguous."
            )
        sha256 = _safe_sha(item["sha256"], name="manifest sha256")
        normalized_chunks.append(
            {"sequence": sequence, "offset": offset, "length": length, "sha256": sha256}
        )
        lines.append(f"{sequence}:{offset}:{length}:{sha256}\n")
        next_offset += length
    if next_offset != total_bytes:
        raise ExtensionIngestError(409, "manifest_conflict", "The finalize total_bytes is invalid.")
    computed_manifest_hash = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    if computed_manifest_hash != manifest_hash:
        raise ExtensionIngestError(
            409, "manifest_conflict", "The manifest hash does not match its entries."
        )
    normalized = {
        "finalize_id": finalize_id,
        "capture_epoch": row["capture_epoch"],
        "epoch": row["epoch"],
        "capture_complete": True,
        "total_bytes": total_bytes,
        "chunk_count": chunk_count,
        "manifest_hash": manifest_hash,
        "chunks": normalized_chunks,
    }
    payload_hash = hashlib.sha256(_canonical_json(normalized)).hexdigest()
    return normalized, payload_hash, manifest_hash


def finalize_recording_session(
    data_dir: Path,
    *,
    identity: ExtensionSessionIdentity,
    session_id: str,
    payload: object,
    backend_id: str,
    settings: Settings,
    enqueue: Callable[[str], None] | None = None,
) -> dict[str, object]:
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _owned_session(connection, identity, session_id)
        if row["state"] == "canceled":
            raise ExtensionIngestError(
                409, "too_late", "The recording was canceled before finalization."
            )
        normalized, payload_hash, manifest_hash = _validate_finalize_payload(
            payload, row=row, settings=settings
        )
        if row["finalize_payload_hash"] is not None:
            if row["finalize_payload_hash"] != payload_hash:
                raise ExtensionIngestError(
                    409,
                    "finalize_conflict",
                    "The finalize request differs from the immutable session intent.",
                )
            # Replay reports the original outcome. Only the operator retry
            # route may restart media work after a durable receipt.
            connection.commit()
        else:
            stored_chunks = connection.execute(
                "SELECT sequence, offset_bytes, length_bytes, sha256 "
                "FROM extension_recording_chunks WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
            expected = [
                (item["sequence"], item["offset"], item["length"], item["sha256"])
                for item in normalized["chunks"]
            ]
            actual = [
                (item["sequence"], item["offset_bytes"], item["length_bytes"], item["sha256"])
                for item in stored_chunks
            ]
            if actual != expected:
                raise ExtensionIngestError(
                    409,
                    "manifest_conflict",
                    "The finalize manifest does not match durable chunk metadata.",
                )
            connection.execute(
                "UPDATE extension_recording_sessions SET finalize_id = ?, "
                "finalize_payload_hash = ?, finalize_total_bytes = ?, "
                "finalize_chunk_count = ?, finalize_capture_complete = 1, "
                "manifest_hash = ?, state = 'finalizing', error_code = NULL, "
                "error_message = NULL, "
                "updated_at = ? WHERE id = ? AND state = 'receiving'",
                (
                    normalized["finalize_id"],
                    payload_hash,
                    normalized["total_bytes"],
                    normalized["chunk_count"],
                    manifest_hash,
                    _now_text(),
                    session_id,
                ),
            )
            connection.commit()
    if enqueue is not None:
        enqueue(session_id)
    return get_recording_session(
        data_dir,
        identity=identity,
        session_id=session_id,
        backend_id=backend_id,
        settings=settings,
    )


def cancel_recording_session(
    data_dir: Path,
    *,
    identity: ExtensionSessionIdentity,
    session_id: str,
    epoch: object,
    backend_id: str,
    settings: Settings,
) -> dict[str, object]:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ExtensionIngestError(400, "invalid_epoch", "The writer epoch is invalid.")
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _owned_session(connection, identity, session_id)
        if row["epoch"] != epoch:
            raise ExtensionIngestError(
                409, "writer_epoch_conflict", "The recording writer epoch changed."
            )
        if row["receipt_at"] is not None or row["state"] in {"archived", "ready"}:
            raise ExtensionIngestError(
                409, "too_late", "The recording archive receipt already exists."
            )
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'canceled', "
            "error_code = 'canceled', "
            "error_message = 'Recording canceled before archive receipt.', updated_at = ? "
            "WHERE id = ? AND epoch = ? AND receipt_at IS NULL",
            (_now_text(), session_id, epoch),
        )
        connection.commit()
    return get_recording_session(
        data_dir,
        identity=identity,
        session_id=session_id,
        backend_id=backend_id,
        settings=settings,
    )


def _set_session_state(
    data_dir: Path,
    session_id: str,
    *,
    state: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _session_query(connection, session_id)
        if row is None:
            connection.rollback()
            return
        if row["deletion_requested_at"] is not None or row["state"] == "canceled":
            connection.rollback()
            return
        connection.execute(
            "UPDATE extension_recording_sessions SET state = ?, error_code = ?, error_message = ?, "
            "updated_at = ? WHERE id = ?",
            (state, error_code, error_message, _now_text(), session_id),
        )
        connection.commit()


def _session_should_cancel(data_dir: Path, session_id: str, stop_event: threading.Event) -> bool:
    if stop_event.is_set():
        return True
    try:
        with _connect(data_dir) as connection:
            row = connection.execute(
                "SELECT state, meetings.deletion_requested_at "
                "FROM extension_recording_sessions AS sessions "
                "LEFT JOIN recordings ON recordings.extension_session_id = sessions.id "
                "LEFT JOIN meetings ON meetings.id = recordings.meeting_id WHERE sessions.id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return (
        row is None
        or row["state"] in {"canceled", "ready"}
        or row["deletion_requested_at"] is not None
    )


def _copy_archive(
    data_dir: Path,
    session_id: str,
    *,
    chunk_rows: list[sqlite3.Row],
    expected_total: int,
    stop_event: threading.Event,
) -> tuple[Path, int, str]:
    _check_disk_budget(data_dir, expected_total)
    raw_dir = Path(data_dir) / "extension-raw"
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{session_id}-", suffix=".webm", dir=raw_dir / ".tmp"
    )
    os.fchmod(temporary_fd, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(temporary_fd, "wb") as output:
            temporary_fd = -1
            for row in chunk_rows:
                if _session_should_cancel(data_dir, session_id, stop_event):
                    raise MediaProcessingError("media_processing_cancelled")
                chunk_path = _chunk_path(data_dir, session_id, int(row["sequence"]))
                actual_length, actual_sha = _hash_file(
                    chunk_path, expected_length=int(row["length_bytes"])
                )
                if actual_length != row["length_bytes"] or actual_sha != row["sha256"]:
                    raise ExtensionIngestError(
                        409, "chunk_corrupt", "An acknowledged chunk is corrupt or missing."
                    )
                with chunk_path.open("rb") as chunk:
                    while True:
                        block = chunk.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        total += len(block)
            if total != expected_total:
                raise ExtensionIngestError(
                    409,
                    "archive_size_mismatch",
                    "The durable archive size does not match its manifest.",
                )
            output.flush()
            os.fsync(output.fileno())
        final_path = _raw_path(data_dir, session_id)
        os.replace(temporary_name, final_path)
        temporary_name = ""
        _fsync_directory(raw_dir)
        return final_path, total, digest.hexdigest()
    finally:
        if temporary_fd >= 0:
            with suppress(OSError):
                os.close(temporary_fd)
        if temporary_name:
            _unlink(Path(temporary_name))


def _commit_archive_receipt(
    data_dir: Path,
    session_id: str,
    *,
    raw_key: str,
    total_bytes: int,
    archive_sha256: str,
) -> bool:
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _session_query(connection, session_id)
        if row is None or row["deletion_requested_at"] is not None or row["state"] == "canceled":
            connection.rollback()
            return False
        if row["receipt_at"] is not None:
            connection.commit()
            return True
        if row["state"] not in {"archiving", "finalizing"}:
            connection.rollback()
            return False
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'archived', raw_key = ?, "
            "raw_bytes = ?, archive_sha256 = ?, receipt_at = ?, error_code = NULL, "
            "error_message = NULL, updated_at = ? "
            "WHERE id = ? AND receipt_at IS NULL",
            (raw_key, total_bytes, archive_sha256, _now_text(), _now_text(), session_id),
        )
        connection.commit()
        return True


def _release_receipted_staging(
    data_dir: Path, session_id: str, *, raw_verified: bool = False
) -> bool:
    """Release acknowledged chunks only after the immutable raw receipt is durable."""
    with _connect(data_dir) as connection:
        session = _session_query(connection, session_id)
        if session is None or session["receipt_at"] is None:
            return False
        chunk_rows = connection.execute(
            "SELECT sequence, storage_key FROM extension_recording_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    for row in chunk_rows:
        expected_key = f"extension-staging/chunks/{session_id}/{int(row['sequence'])}.chunk"
        if row["storage_key"] != expected_key:
            raise RuntimeError("An extension chunk has an unexpected storage key")
    if not raw_verified:
        try:
            _complete_raw_archive(data_dir, session)
        except ExtensionIngestError:
            return False

    _session_dir(data_dir, session_id)
    with _managed_extension_directories(data_dir, session_id) as directories:
        staging_changed = _remove_directory_entries(directories["session"], lambda name: True)
        if staging_changed:
            os.fsync(directories["session"])
        with suppress(FileNotFoundError):
            os.rmdir(session_id, dir_fd=directories["chunks"])
        os.fsync(directories["chunks"])
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM extension_recording_reservations WHERE session_id = ?", (session_id,)
        )
        connection.execute(
            "DELETE FROM extension_recording_chunks WHERE session_id = ?", (session_id,)
        )
        connection.commit()
    return True


def publish_ready_derivative(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    derivative_key: str,
    duration_seconds: float,
) -> bool:
    """Publish ready metadata inside the caller-owned SQLite transaction.

    This helper deliberately does not begin or commit a transaction and does
    not touch media or the network.  The caller must retain ownership of the
    session publication/deletion boundary and decide whether to commit.
    """
    row = _session_query(connection, session_id)
    if row is None or row["deletion_requested_at"] is not None or row["state"] == "canceled":
        return False
    if row["state"] not in {"archived", "finalizing"}:
        return row["state"] == "ready"
    connection.execute(
        "UPDATE recordings SET storage_key = ?, content_type = ?, duration_seconds = ? "
        "WHERE extension_session_id = ?",
        (derivative_key, OUTPUT_CONTENT_TYPE, duration_seconds, session_id),
    )
    connection.execute(
        "UPDATE extension_recording_sessions SET state = 'ready', error_code = NULL, "
        "error_message = NULL, updated_at = ? "
        "WHERE id = ? AND state IN ('archived', 'finalizing')",
        (_now_text(), session_id),
    )
    return True


def _commit_ready_derivative(
    data_dir: Path,
    session_id: str,
    *,
    derivative_key: str,
    duration_seconds: float,
) -> bool:
    with _connect(data_dir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        published = publish_ready_derivative(
            connection,
            session_id,
            derivative_key=derivative_key,
            duration_seconds=duration_seconds,
        )
        if published:
            connection.commit()
        else:
            connection.rollback()
        return published


def _mark_media_failure(data_dir: Path, session_id: str, error: MediaProcessingError) -> None:
    _set_session_state(
        data_dir,
        session_id,
        state="failed",
        error_code=error.code,
        error_message=(
            "The raw recording was archived, but media finalization failed. Retry normalization."
        ),
    )


def _mark_staging_cleanup_failure(data_dir: Path, session_id: str) -> None:
    _set_session_state(
        data_dir,
        session_id,
        state="failed",
        error_code="staging_cleanup_failed",
        error_message=(
            "The raw recording was archived, but staging cleanup failed. Retry recording."
        ),
    )


def _process_archive_session(
    data_dir: Path,
    session_id: str,
    *,
    settings: Settings,
    stop_event: threading.Event,
) -> None:
    with session_lock(data_dir, session_id):
        with _connect(data_dir) as connection:
            row = _session_query(connection, session_id)
        if row is None or row["deletion_requested_at"] is not None or row["state"] == "canceled":
            return
        if row["receipt_at"] is None and row["state"] in {"finalizing", "archiving"}:
            _set_session_state(data_dir, session_id, state="archiving")
            with _connect(data_dir) as connection:
                current = _session_query(connection, session_id)
                if current is None:
                    return
                chunk_rows = connection.execute(
                    "SELECT sequence, offset_bytes, length_bytes, sha256, storage_key "
                    "FROM extension_recording_chunks WHERE session_id = ? ORDER BY sequence",
                    (session_id,),
                ).fetchall()
            if len(chunk_rows) != int(current["finalize_chunk_count"] or 0):
                _set_session_state(
                    data_dir,
                    session_id,
                    state="failed",
                    error_code="manifest_conflict",
                    error_message="The durable chunk set no longer matches the finalize manifest.",
                )
                return
            try:
                raw_path, total_bytes, archive_sha256 = _copy_archive(
                    data_dir,
                    session_id,
                    chunk_rows=chunk_rows,
                    expected_total=int(current["finalize_total_bytes"] or 0),
                    stop_event=stop_event,
                )
            except MediaProcessingError:
                return
            except ExtensionIngestError as error:
                if not stop_event.is_set():
                    _set_session_state(
                        data_dir,
                        session_id,
                        state="failed",
                        error_code=error.code,
                        error_message=error.message,
                    )
                return
            except OSError:
                if not stop_event.is_set():
                    _set_session_state(
                        data_dir,
                        session_id,
                        state="failed",
                        error_code="storage_failed",
                        error_message="The raw archive could not be durably stored.",
                    )
                return
            committed = _commit_archive_receipt(
                data_dir,
                session_id,
                raw_key=f"extension-raw/{session_id}.webm",
                total_bytes=total_bytes,
                archive_sha256=archive_sha256,
            )
            if not committed:
                _unlink(raw_path)
                return
        with _connect(data_dir) as connection:
            row = _session_query(connection, session_id)
        if row is None or row["deletion_requested_at"] is not None or row["state"] == "canceled":
            return
        if row["receipt_at"] is not None:
            try:
                if not _release_receipted_staging(data_dir, session_id):
                    # Retain staging when the immutable raw receipt cannot be
                    # verified. A terminal state keeps this session out of both
                    # lanes instead of repeatedly selecting the same failed work.
                    _mark_media_failure(data_dir, session_id, MediaProcessingError("raw_corrupt"))
                    return
            except (OSError, RuntimeError):
                # A failed cleanup must leave a visible terminal state. Failed
                # sessions are not selected again until an operator retries them.
                _mark_staging_cleanup_failure(data_dir, session_id)
                return
            except sqlite3.Error:
                # A transient database failure must be retried by the archive
                # lane. Do not turn a recoverable cleanup error into a false
                # terminal state or let it kill the worker loop.
                raise


def _process_media_session(
    data_dir: Path,
    session_id: str,
    *,
    settings: Settings,
    stop_event: threading.Event,
) -> None:
    with session_lock(data_dir, session_id):
        with _connect(data_dir) as connection:
            row = _session_query(connection, session_id)
            if row is not None and row["receipt_at"] is not None:
                has_staging = connection.execute(
                    "SELECT 1 FROM extension_recording_chunks WHERE session_id = ? "
                    "UNION ALL "
                    "SELECT 1 FROM extension_recording_reservations WHERE session_id = ? "
                    "LIMIT 1",
                    (session_id, session_id),
                ).fetchone()
            else:
                has_staging = None
        if (
            row is None
            or row["deletion_requested_at"] is not None
            or row["state"] == "canceled"
            or row["receipt_at"] is None
            or row["state"] not in {"archived", "finalizing"}
            or has_staging is not None
        ):
            return
        if _session_should_cancel(data_dir, session_id, stop_event):
            return
        try:
            _complete_raw_archive(data_dir, row)
        except ExtensionIngestError:
            _set_session_state(
                data_dir,
                session_id,
                state="failed",
                error_code="raw_corrupt",
                error_message="The archived audio is missing or corrupt.",
            )
            return
        _set_session_state(data_dir, session_id, state="finalizing")
        raw_path = _raw_path(data_dir, session_id)
        derivative_dir = Path(data_dir) / "extension-derivatives"
        limits = extension_limits(settings)
        try:
            _check_disk_budget(data_dir, limits["media_output_bytes"])
        except ExtensionIngestError as error:
            if not stop_event.is_set():
                _set_session_state(
                    data_dir,
                    session_id,
                    state="failed",
                    error_code=error.code,
                    error_message=error.message,
                )
            return
        temporary_fd = -1
        temporary_name = ""
        try:
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{session_id}-", suffix=".m4a", dir=derivative_dir / ".tmp"
            )
            os.fchmod(temporary_fd, 0o600)
            os.close(temporary_fd)
            temporary_fd = -1
            normalized = normalize_extension_media(
                raw_path,
                Path(temporary_name),
                max_duration_seconds=settings.max_recording_seconds,
                max_output_bytes=limits["media_output_bytes"],
                timeout_seconds=limits["media_wall_seconds"],
                should_cancel=lambda: _session_should_cancel(data_dir, session_id, stop_event),
                remux_prefix=f".{session_id}-",
            )
            with Path(temporary_name).open("rb") as output:
                os.fsync(output.fileno())
            if Path(temporary_name).stat().st_size > limits["media_output_bytes"]:
                raise MediaProcessingError("media_output_limit")
            final_path = _derivative_path(data_dir, session_id)
            os.replace(temporary_name, final_path)
            temporary_name = ""
            _fsync_directory(derivative_dir)
            if not _commit_ready_derivative(
                data_dir,
                session_id,
                derivative_key=f"extension-derivatives/{session_id}.m4a",
                duration_seconds=normalized.duration_seconds,
            ):
                _unlink(final_path)
        except MediaProcessingError as error:
            if not stop_event.is_set():
                _mark_media_failure(data_dir, session_id, error)
        except (OSError, ExtensionIngestError):
            if not stop_event.is_set():
                _set_session_state(
                    data_dir,
                    session_id,
                    state="failed",
                    error_code="storage_failed",
                    error_message="The normalized recording could not be durably published.",
                )
        finally:
            if temporary_fd >= 0:
                with suppress(OSError):
                    os.close(temporary_fd)
            if temporary_name:
                _unlink(Path(temporary_name))


def _process_session(
    data_dir: Path,
    session_id: str,
    *,
    settings: Settings,
    stop_event: threading.Event,
) -> None:
    """Run both bounded lanes sequentially for direct recovery/test callers."""
    _process_archive_session(
        data_dir,
        session_id,
        settings=settings,
        stop_event=stop_event,
    )
    if not stop_event.is_set():
        _process_media_session(
            data_dir,
            session_id,
            settings=settings,
            stop_event=stop_event,
        )


class ExtensionIngestWorker:
    """One lifetime-owned worker supervising bounded archive and media lanes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._archive_thread: threading.Thread | None = None
        self._media_thread: threading.Thread | None = None
        self._service_lock: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="asr-extension-media-supervisor", daemon=True
        )
        self._thread.start()

    def enqueue(self, session_id: str) -> None:
        if SESSION_ID_RE.fullmatch(session_id) is not None:
            self.wake_event.set()

    def _pending_archive_session(self) -> str | None:
        with _connect(self.settings.data_dir) as connection:
            row = connection.execute(
                "SELECT sessions.id FROM extension_recording_sessions AS sessions "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM recordings "
                "JOIN meetings ON meetings.id = recordings.meeting_id "
                "WHERE recordings.extension_session_id = sessions.id "
                "AND meetings.deletion_requested_at IS NOT NULL"
                ") AND ("
                "(sessions.receipt_at IS NULL AND sessions.state IN ('finalizing', 'archiving')) "
                "OR (sessions.receipt_at IS NOT NULL "
                "AND sessions.state IN ('archived', 'finalizing') AND ("
                "EXISTS (SELECT 1 FROM extension_recording_chunks AS chunks "
                "WHERE chunks.session_id = sessions.id) OR "
                "EXISTS (SELECT 1 FROM extension_recording_reservations AS reservations "
                "WHERE reservations.session_id = sessions.id)"
                "))"
                ") ORDER BY sessions.updated_at ASC, sessions.id ASC LIMIT 1"
            ).fetchone()
        return row[0] if row is not None else None

    def _pending_media_session(self) -> str | None:
        with _connect(self.settings.data_dir) as connection:
            row = connection.execute(
                "SELECT sessions.id FROM extension_recording_sessions AS sessions "
                "WHERE sessions.receipt_at IS NOT NULL "
                "AND sessions.state IN ('archived', 'finalizing') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM extension_recording_chunks AS chunks "
                "WHERE chunks.session_id = sessions.id"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM extension_recording_reservations AS reservations "
                "WHERE reservations.session_id = sessions.id"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM recordings "
                "JOIN meetings ON meetings.id = recordings.meeting_id "
                "WHERE recordings.extension_session_id = sessions.id "
                "AND meetings.deletion_requested_at IS NOT NULL"
                ") ORDER BY sessions.updated_at ASC, sessions.id ASC LIMIT 1"
            ).fetchone()
        return row[0] if row is not None else None

    def _pending_session(self) -> str | None:
        """Return the next session for compatibility with direct lifecycle probes."""
        return self._pending_archive_session() or self._pending_media_session()

    def _wait_for_work(self, seconds: float) -> None:
        self.wake_event.wait(seconds)
        self.wake_event.clear()

    def _run_lane(
        self,
        lane: str,
        pending: Callable[[], str | None],
        process: Callable[..., None],
    ) -> None:
        retry_delay = _WORKER_SQL_RETRY_INITIAL_SECONDS
        while not self.stop_event.is_set():
            try:
                session_id = pending()
                if session_id is None:
                    retry_delay = _WORKER_SQL_RETRY_INITIAL_SECONDS
                    self._wait_for_work(_WORKER_IDLE_WAIT_SECONDS)
                    continue
                process(
                    self.settings.data_dir,
                    session_id,
                    settings=self.settings,
                    stop_event=self.stop_event,
                )
                retry_delay = _WORKER_SQL_RETRY_INITIAL_SECONDS
            except sqlite3.Error:
                LOGGER.warning(
                    "extension ingest %s lane database error; retrying in %.2fs",
                    lane,
                    retry_delay,
                    exc_info=True,
                )
                self._wait_for_work(retry_delay)
                retry_delay = min(retry_delay * 2, _WORKER_SQL_RETRY_MAX_SECONDS)
            except Exception:
                LOGGER.exception("extension ingest %s lane failed; retrying", lane)
                self._wait_for_work(retry_delay)
                retry_delay = min(retry_delay * 2, _WORKER_SQL_RETRY_MAX_SECONDS)

    def _run(self) -> None:
        descriptor = None
        while not self.stop_event.is_set():
            descriptor = acquire_service_lock(self.settings.data_dir, "media", retry_seconds=0.2)
            if descriptor is not None:
                break
            self.stop_event.wait(0.25)
        if descriptor is None:
            return
        self._service_lock = descriptor
        archive_thread = threading.Thread(
            target=self._run_lane,
            args=("archive", self._pending_archive_session, _process_archive_session),
            name="asr-extension-archive",
            daemon=True,
        )
        media_thread = threading.Thread(
            target=self._run_lane,
            args=("media", self._pending_media_session, _process_media_session),
            name="asr-extension-normalize",
            daemon=True,
        )
        self._archive_thread = archive_thread
        self._media_thread = media_thread
        try:
            write_service_heartbeat(self.settings.data_dir, "media", "starting", role="extension")
            archive_thread.start()
            media_thread.start()
            write_service_heartbeat(self.settings.data_dir, "media", "running", role="extension")
            last_heartbeat = time.monotonic()
            while archive_thread.is_alive() or media_thread.is_alive():
                if self.stop_event.wait(_WORKER_IDLE_WAIT_SECONDS):
                    break
                if time.monotonic() - last_heartbeat >= _WORKER_HEARTBEAT_INTERVAL_SECONDS:
                    write_service_heartbeat(
                        self.settings.data_dir, "media", "running", role="extension"
                    )
                    last_heartbeat = time.monotonic()
        finally:
            self.stop_event.set()
            self.wake_event.set()
            archive_thread.join()
            media_thread.join()
            with suppress(Exception):
                write_service_heartbeat(
                    self.settings.data_dir, "media", "stopped", role="extension"
                )
            with suppress(OSError):
                os.close(descriptor)
            self._service_lock = None

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=EXTENSION_MEDIA_WALL_SECONDS + 5)
        if thread is not None and not thread.is_alive():
            self._thread = None
            self._archive_thread = None
            self._media_thread = None


def recover_extension_ingest(data_dir: Path) -> None:
    """Recover only after acquiring each session's publication/delete boundary."""
    ensure_extension_storage(data_dir)
    temp_artifacts = _reconcile_extension_temp_artifacts(data_dir)
    with _connect(data_dir) as connection:
        database_session_ids = [
            row[0]
            for row in connection.execute(
                "SELECT sessions.id FROM extension_recording_sessions AS sessions "
                "WHERE sessions.state IN ('receiving', 'finalizing', 'archiving') "
                "OR EXISTS ("
                "SELECT 1 FROM extension_recording_chunks AS chunks "
                "WHERE chunks.session_id = sessions.id"
                ") OR EXISTS ("
                "SELECT 1 FROM extension_recording_reservations AS reservations "
                "WHERE reservations.session_id = sessions.id"
                ") ORDER BY sessions.id"
            ).fetchall()
        ]
    session_ids = sorted(set(database_session_ids) | set(temp_artifacts))
    for session_id in session_ids:
        with session_lock(data_dir, session_id):
            with _connect(data_dir) as connection:
                session = connection.execute(
                    "SELECT raw_key, raw_bytes, archive_sha256, state, receipt_at "
                    "FROM extension_recording_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    _session_dir(data_dir, session_id)
                    with _managed_extension_directories(data_dir, session_id) as directories:
                        _remove_enumerated_extension_temp_artifacts(
                            directories, temp_artifacts.get(session_id, {})
                        )
                    continue
                chunk_rows = connection.execute(
                    "SELECT sequence, storage_key, length_bytes, sha256 "
                    "FROM extension_recording_chunks WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
                reservations = connection.execute(
                    "SELECT id, temp_name FROM extension_recording_reservations "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
                recording = connection.execute(
                    "SELECT storage_key FROM recordings WHERE extension_session_id = ?",
                    (session_id,),
                ).fetchone()

            referenced_names: set[str] = set()
            for row in chunk_rows:
                expected_key = f"extension-staging/chunks/{session_id}/{int(row['sequence'])}.chunk"
                if row["storage_key"] != expected_key:
                    raise RuntimeError("An extension chunk has an unexpected storage key")
                referenced_names.add(f"{int(row['sequence'])}.chunk")
            reservation_names: set[str] = set()
            for reservation in reservations:
                if not _safe_entry_name(reservation["temp_name"]):
                    raise RuntimeError("An extension reservation has an unsafe temp name")
                reservation_names.add(reservation["temp_name"])

            _session_dir(data_dir, session_id)
            with _managed_extension_directories(data_dir, session_id) as directories:
                session_descriptor = directories["session"]
                changed = False
                for name in os.listdir(session_descriptor):
                    is_link = stat.S_ISLNK(
                        os.stat(name, dir_fd=session_descriptor, follow_symlinks=False).st_mode
                    )
                    if is_link or (name not in referenced_names and name not in reservation_names):
                        changed = _unlink_directory_entry(session_descriptor, name) or changed
                # No live writer can retain this lock. A reservation left by a crashed writer is
                # therefore an uncommitted temp, not evidence of an acknowledged chunk.
                for name in reservation_names:
                    changed = _unlink_directory_entry(session_descriptor, name) or changed
                if changed:
                    os.fsync(session_descriptor)
            with _connect(data_dir) as connection, connection:
                connection.execute(
                    "DELETE FROM extension_recording_reservations WHERE session_id = ?",
                    (session_id,),
                )

            if session["receipt_at"] is None:
                corrupt = False
                for row in chunk_rows:
                    path = _chunk_path(data_dir, session_id, int(row["sequence"]))
                    try:
                        length, digest = _hash_file(path, expected_length=int(row["length_bytes"]))
                    except ExtensionIngestError:
                        corrupt = True
                        continue
                    if length != row["length_bytes"] or digest != row["sha256"]:
                        corrupt = True
                if corrupt:
                    with _connect(data_dir) as connection, connection:
                        connection.execute(
                            "UPDATE extension_recording_sessions SET state = 'failed', "
                            "error_code = 'chunk_corrupt', "
                            "error_message = 'An acknowledged chunk is corrupt or missing.', "
                            "updated_at = ? WHERE id = ? AND receipt_at IS NULL "
                            "AND state <> 'canceled'",
                            (_now_text(), session_id),
                        )

            if session["raw_key"] not in {None, f"extension-raw/{session_id}.webm"}:
                raise RuntimeError("An extension session has an unexpected raw storage key")
            derivative_key = recording["storage_key"] if recording is not None else None
            if derivative_key not in {None, f"extension-derivatives/{session_id}.m4a"}:
                raise RuntimeError("An extension recording has an unexpected derivative key")
            with _managed_extension_directories(data_dir, session_id) as directories:
                changed_raw = False
                if session["raw_key"] is None:
                    changed_raw = _unlink_directory_entry(directories["raw"], f"{session_id}.webm")
                changed_derivative = False
                if derivative_key is None:
                    changed_derivative = _unlink_directory_entry(
                        directories["derivatives"], f"{session_id}.m4a"
                    )
                if changed_raw:
                    os.fsync(directories["raw"])
                if changed_derivative:
                    os.fsync(directories["derivatives"])
                _remove_enumerated_extension_temp_artifacts(
                    directories, temp_artifacts.get(session_id, {})
                )
            has_staging = bool(chunk_rows or reservations)
            raw_corrupt = False
            if session["receipt_at"] is not None and has_staging:
                raw_path = Path(data_dir) / "extension-raw" / f"{session_id}.webm"
                try:
                    raw_length, raw_digest = _hash_file(
                        raw_path, expected_length=session["raw_bytes"]
                    )
                    raw_corrupt = (
                        raw_digest != session["archive_sha256"]
                        or raw_length != session["raw_bytes"]
                    )
                except ExtensionIngestError:
                    raw_corrupt = True
            if raw_corrupt:
                with _connect(data_dir) as connection, connection:
                    connection.execute(
                        "UPDATE extension_recording_sessions SET state = 'failed', "
                        "error_code = 'raw_corrupt', "
                        "error_message = 'The durable raw receipt is missing or corrupt.', "
                        "updated_at = ? WHERE id = ? AND receipt_at IS NOT NULL "
                        "AND state <> 'canceled'",
                        (_now_text(), session_id),
                    )
            elif session["receipt_at"] is not None and has_staging:
                try:
                    _release_receipted_staging(data_dir, session_id, raw_verified=True)
                except (OSError, RuntimeError, sqlite3.Error):
                    _mark_staging_cleanup_failure(data_dir, session_id)


def cleanup_extension_session_artifacts(data_dir: Path, session_id: str) -> None:
    """Unlink only this session's files; callers must hold its session lock."""
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise RuntimeError("invalid extension session id")
    with _connect(data_dir) as connection:
        chunk_rows = connection.execute(
            "SELECT sequence, storage_key FROM extension_recording_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        session = connection.execute(
            "SELECT raw_key FROM extension_recording_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        recording = connection.execute(
            "SELECT storage_key FROM recordings WHERE extension_session_id = ?", (session_id,)
        ).fetchone()
    for row in chunk_rows:
        sequence = int(row["sequence"])
        expected_key = f"extension-staging/chunks/{session_id}/{sequence}.chunk"
        if row["storage_key"] != expected_key:
            raise RuntimeError("An extension chunk has an unexpected storage key")
    if session is not None and session["raw_key"] not in {
        None,
        f"extension-raw/{session_id}.webm",
    }:
        raise RuntimeError("An extension session has an unexpected raw storage key")
    if recording is not None and recording["storage_key"] not in {
        None,
        f"extension-derivatives/{session_id}.m4a",
    }:
        raise RuntimeError("An extension recording has an unexpected derivative key")
    _session_dir(data_dir, session_id)
    with _managed_extension_directories(data_dir, session_id) as directories:
        staging_changed = _remove_directory_entries(directories["session"], lambda name: True)
        raw_changed = _unlink_directory_entry(directories["raw"], f"{session_id}.webm")
        derivative_changed = _unlink_directory_entry(
            directories["derivatives"], f"{session_id}.m4a"
        )
        raw_tmp_changed = _remove_directory_entries(
            directories["raw_tmp"], lambda name: name.startswith(f".{session_id}-")
        )
        derivative_tmp_changed = _remove_directory_entries(
            directories["derivatives_tmp"],
            lambda name: name.startswith(f".{session_id}-"),
        )
        if staging_changed:
            os.fsync(directories["session"])
        if raw_changed:
            os.fsync(directories["raw"])
        if derivative_changed:
            os.fsync(directories["derivatives"])
        if raw_tmp_changed:
            os.fsync(directories["raw_tmp"])
        if derivative_tmp_changed:
            os.fsync(directories["derivatives_tmp"])
        with suppress(FileNotFoundError):
            os.rmdir(session_id, dir_fd=directories["chunks"])
        os.fsync(directories["chunks"])
