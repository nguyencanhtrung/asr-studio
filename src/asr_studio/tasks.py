"""Durable transcription task transitions and complete transcript publication."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asr_studio.alignment import ALIGNMENT_POLICY, ASR_UNCERTAINTY_POLICY
from asr_studio.asr_models import AUTO, engine_of
from asr_studio.diarization_runner import (
    DiarizationConfigurationError,
    prepared_diarization_directory,
)
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory
from asr_studio.storage import database_path

UNCERTAINTY_POLICY = ASR_UNCERTAINTY_POLICY
DIARIZATION_MODEL_NAME = "Community-1"
_REVIEW_REASONS = frozenset(
    {
        "asr_uncertain",
        "overlap",
        "multiple_speakers",
        "low_speaker_dominance",
        "unidentified_speaker",
    }
)
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_SEGMENT_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_LANGUAGE_HINTS = frozenset({"en", "vi"})
_LANGUAGE_HINT_UNSET = object()
_ACTIVE_TASK_STATES = frozenset({"accepted", "running", "stopping"})
_TASK_HEARTBEAT_MAX_AGE_SECONDS = 20
TASK_ERROR_MESSAGES = {
    "publication_failed": (
        "The completed transcript could not be saved. Check local storage and retry."
    ),
    "notes_publication_failed": (
        "The completed notes could not be saved. Check local storage and retry."
    ),
}


class TaskError(RuntimeError):
    """An API-safe failure in task acceptance or lifecycle transitions."""

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


@contextmanager
def _connect_read_only(data_dir: Path) -> Iterator[sqlite3.Connection]:
    path = database_path(data_dir).resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", timeout=5, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise TaskError(
            400,
            "invalid_request_id",
            "request_id must contain 8 to 128 letters, digits, dots, underscores, "
            "colons, or hyphens.",
        )
    return value


def _payload_hash(
    *,
    meeting_id: str,
    language_hint: str | None = None,
) -> str:
    payload: dict[str, str] = {"meeting_id": meeting_id}
    if language_hint is not None:
        payload["language_hint"] = language_hint
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _language_hint(value: object) -> str:
    if not isinstance(value, str) or value not in _LANGUAGE_HINTS:
        raise TaskError(400, "invalid_language_hint", "language_hint must be en or vi.")
    return value


def _worker_state(row: sqlite3.Row) -> str:
    if row["status"] not in _ACTIVE_TASK_STATES:
        return "idle"
    heartbeat_at = row["heartbeat_at"]
    if not heartbeat_at:
        return "checking-worker"
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at)
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - heartbeat).total_seconds()
    except (TypeError, ValueError):
        return "checking-worker"
    if -1 <= age <= _TASK_HEARTBEAT_MAX_AGE_SECONDS:
        return "healthy"
    return "checking-worker"


def _task_summary(row: sqlite3.Row) -> dict[str, object]:
    summary: dict[str, object] = {
        "id": row["id"],
        "request_id": row["request_id"],
        "meeting_id": row["meeting_id"],
        "kind": row["kind"],
        "model_name": row["model_name"],
        "language_hint": row["language_hint"],
        "device": row["device"],
        "compute_type": row["compute_type"],
        "status": row["status"],
        "worker_state": _worker_state(row),
        "stage": row["stage"],
        "result_scope": row["result_scope"],
        "audio_duration_seconds": row["audio_duration_seconds"],
        "transcript_end_seconds": row["transcript_end_seconds"],
        "segment_count": row["segment_count"],
        "elapsed_seconds": row["elapsed_seconds"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "accepted_at": row["accepted_at"],
        "started_at": row["started_at"],
        "heartbeat_at": row["heartbeat_at"],
        "cancel_requested_at": row["cancel_requested_at"],
        "completed_at": row["completed_at"],
        "updated_at": row["updated_at"],
    }
    summary["progress"] = (
        {
            "current": max(0, int(row["progress_current"])),
            "total": row["progress_total"],
            "unit": row["progress_unit"],
        }
        if isinstance(row["progress_unit"], str) and row["progress_unit"]
        else None
    )
    if row["kind"] == "transcription":
        summary["asr_model"] = row["asr_model"]
    if row["kind"] == "notes":
        summary["output_language"] = row["notes_output_language"]
        summary["provider"] = row["notes_provider"]
        summary["model"] = row["notes_model"]
        summary["reasoning_effort"] = row["notes_reasoning_effort"]
    return summary


def accept_transcription_task(
    data_dir: Path,
    *,
    meeting_id: object,
    request_id: object,
    model_path: Path,
    language_hint: object = _LANGUAGE_HINT_UNSET,
    diarization_model_path: Path = Path("/models/speaker-diarization-community-1"),
    device: str,
    compute_type: str,
    worker_ready: bool,
    asr_model: str = AUTO,
) -> dict[str, object]:
    """Accept one request atomically, returning the original row for HTTP retries."""
    if not isinstance(meeting_id, str) or not meeting_id:
        raise TaskError(400, "invalid_meeting_id", "Provide a meeting_id.")
    normalized_request_id = _request_id(request_id)
    normalized_model_path = str(Path(model_path).expanduser().resolve())
    model_name = Path(normalized_model_path).name
    normalized_diarization_model_path = str(Path(diarization_model_path).expanduser().resolve())
    task_id = uuid.uuid4().hex

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        meeting = connection.execute(
            "SELECT id, language_hint, deletion_requested_at FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            raise TaskError(404, "meeting_not_found", "Meeting not found.")
        if meeting["deletion_requested_at"] is not None:
            raise TaskError(
                409,
                "meeting_deletion_pending",
                "This meeting is being deleted. Wait for cleanup to finish before starting "
                "another task.",
            )
        normalized_language_hint = (
            meeting["language_hint"]
            if language_hint is _LANGUAGE_HINT_UNSET
            else _language_hint(language_hint)
        )
        request_hash = _payload_hash(
            meeting_id=meeting_id,
            language_hint=normalized_language_hint,
        )
        legacy_request_hash = _payload_hash(meeting_id=meeting_id)
        existing = connection.execute(
            "SELECT * FROM tasks WHERE request_id = ?", (normalized_request_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["language_hint"] != normalized_language_hint
                or existing["asr_model"] != asr_model
                or existing["request_hash"] not in {request_hash, legacy_request_hash}
            ):
                raise TaskError(
                    409,
                    "idempotency_conflict",
                    "This request_id was already used with different task details.",
                )
            return _task_summary(existing)

        slot = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()
        if slot is None:
            raise TaskError(503, "processing_unavailable", "The processing slot is unavailable.")
        if slot["active_task_id"] is not None:
            raise TaskError(
                409,
                "system_busy",
                "Another transcription task owns the processing slot.",
            )
        if not worker_ready:
            raise TaskError(503, "worker_unavailable", "The transcription worker is unavailable.")
        engine = engine_of(asr_model)
        if engine == "qwen" and device != "cuda":
            raise TaskError(
                503,
                "model_requires_gpu",
                "The selected ASR model runs only on an NVIDIA GPU.",
            )
        try:
            prepared_engine_directory(engine, Path(normalized_model_path))
        except ModelConfigurationError:
            raise TaskError(
                503,
                "model_unavailable",
                "The selected local ASR model is not prepared.",
            ) from None
        try:
            prepared_diarization_directory(Path(normalized_diarization_model_path))
        except DiarizationConfigurationError:
            raise TaskError(
                503,
                "diarization_model_unavailable",
                "The configured local Community-1 model is not prepared.",
            ) from None

        recording = connection.execute(
            "SELECT duration_seconds FROM recordings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
        if recording is None:
            raise TaskError(404, "meeting_not_found", "Meeting recording not found.")

        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "diarization_model_path, "
            "asr_model, model_name, device, compute_type, language_hint, status, stage, "
            "audio_duration_seconds) "
            "VALUES (?, ?, ?, ?, 'transcription', ?, ?, ?, ?, ?, ?, ?, 'accepted', 'accepted', ?)",
            (
                task_id,
                normalized_request_id,
                request_hash,
                meeting_id,
                normalized_model_path,
                normalized_diarization_model_path,
                asr_model,
                model_name,
                device,
                compute_type,
                normalized_language_hint,
                recording["duration_seconds"],
            ),
        )
        cursor = connection.execute(
            "UPDATE processing_slot SET active_task_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE slot_id = 1 AND active_task_id IS NULL",
            (task_id,),
        )
        if cursor.rowcount != 1:
            raise TaskError(409, "system_busy", "Another task owns the processing slot.")
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        summary = _task_summary(row)
    return summary


def get_task(data_dir: Path, task_id: str) -> dict[str, object]:
    with _connect_read_only(data_dir) as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(404, "task_not_found", "Task not found.")
    return _task_summary(row)


def cancel_task(data_dir: Path, task_id: str) -> dict[str, object]:
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError(404, "task_not_found", "Task not found.")
        if row["status"] == "accepted":
            connection.execute(
                "UPDATE tasks SET status = 'canceled', stage = 'canceled', "
                "cancel_requested_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'accepted'",
                (task_id,),
            )
            connection.execute(
                "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE slot_id = 1 AND active_task_id = ?",
                (task_id,),
            )
        elif row["status"] == "running":
            connection.execute(
                "UPDATE tasks SET status = 'stopping', stage = 'stopping', "
                "cancel_requested_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'running'",
                (task_id,),
            )
        updated = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert updated is not None
        return _task_summary(updated)


def claim_next_task(data_dir: Path, *, owner_id: str) -> dict[str, object] | None:
    """Atomically claim the one accepted task and bind it to a worker ownership token."""
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        slot = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()
        if slot is None or slot["active_task_id"] is None:
            return None
        task_id = slot["active_task_id"]
        cursor = connection.execute(
            "UPDATE tasks SET status = 'running', stage = 'starting', owner_id = ?, "
            "started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'accepted'",
            (owner_id, task_id),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        return dict(row)


def task_runtime_state(data_dir: Path, task_id: str) -> tuple[str, str | None] | None:
    with _connect(data_dir) as connection:
        row = connection.execute(
            "SELECT status, owner_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    return row["status"], row["owner_id"]


def update_task_progress(
    data_dir: Path,
    task_id: str,
    *,
    owner_id: str,
    stage: str,
    elapsed_seconds: float,
    transcript_end_seconds: float,
    segment_count: int,
    progress_current: int | None = None,
    progress_total: int | None = None,
    progress_unit: str | None = None,
    clear_progress: bool = False,
) -> bool:
    progress_sql = ""
    progress_values: tuple[object, ...] = ()
    if clear_progress:
        progress_sql = ", progress_current = 0, progress_total = NULL, progress_unit = NULL"
    elif progress_unit is not None:
        current = max(0, progress_current if isinstance(progress_current, int) else 0)
        total = (
            max(current, progress_total)
            if isinstance(progress_total, int) and progress_total >= 0
            else None
        )
        progress_sql = ", progress_current = ?, progress_total = ?, progress_unit = ?"
        progress_values = (current, total, progress_unit)
    with _connect(data_dir) as connection, connection:
        cursor = connection.execute(
            "UPDATE tasks SET stage = ?, elapsed_seconds = ?, "
            "transcript_end_seconds = MAX(transcript_end_seconds, ?), segment_count = ?, "
            "heartbeat_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP"
            f"{progress_sql} "
            "WHERE id = ? AND owner_id = ? AND status = 'running'",
            (
                stage,
                max(0.0, elapsed_seconds),
                max(0.0, transcript_end_seconds),
                max(0, segment_count),
                *progress_values,
                task_id,
                owner_id,
            ),
        )
        return cursor.rowcount == 1


def fail_task(
    data_dir: Path,
    task_id: str,
    *,
    owner_id: str,
    error_code: str,
    message: str,
    elapsed_seconds: float,
) -> bool:
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT status FROM tasks WHERE id = ? AND owner_id = ?", (task_id, owner_id)
        ).fetchone()
        if task is None or task["status"] not in {"running", "stopping"}:
            return False
        if task["status"] == "stopping":
            connection.execute(
                "UPDATE tasks SET status = 'canceled', stage = 'canceled', "
                "elapsed_seconds = ?, completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND owner_id = ? "
                "AND status = 'stopping'",
                (max(0.0, elapsed_seconds), task_id, owner_id),
            )
        else:
            connection.execute(
                "UPDATE tasks SET status = 'failed', stage = 'failed', error_code = ?, "
                "error_message = ?, elapsed_seconds = ?, completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND owner_id = ? "
                "AND status = 'running'",
                (error_code, message, max(0.0, elapsed_seconds), task_id, owner_id),
            )
        connection.execute(
            "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE slot_id = 1 AND active_task_id = ?",
            (task_id,),
        )
        return True


def finish_canceled_task(
    data_dir: Path, task_id: str, *, owner_id: str, elapsed_seconds: float
) -> bool:
    """Release the slot only after the worker has terminated and reaped its runner group."""
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE tasks SET status = 'canceled', stage = 'canceled', elapsed_seconds = ?, "
            "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND owner_id = ? AND status = 'stopping'",
            (max(0.0, elapsed_seconds), task_id, owner_id),
        )
        if cursor.rowcount == 1:
            connection.execute(
                "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE slot_id = 1 AND active_task_id = ?",
                (task_id,),
            )
            return True
        return False


def finish_interrupted_task(
    data_dir: Path,
    task_id: str,
    *,
    owner_id: str,
    elapsed_seconds: float,
    message: str | None = None,
) -> bool:
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT status, kind FROM tasks WHERE id = ? AND owner_id = ?", (task_id, owner_id)
        ).fetchone()
        if task is None or task["status"] not in {"running", "stopping"}:
            return False
        if task["status"] == "stopping":
            connection.execute(
                "UPDATE tasks SET status = 'canceled', stage = 'canceled', elapsed_seconds = ?, "
                "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND owner_id = ? AND status = 'stopping'",
                (max(0.0, elapsed_seconds), task_id, owner_id),
            )
        else:
            interruption_message = message or (
                "The worker stopped before notes finished."
                if task["kind"] == "notes"
                else "The worker stopped before transcription finished."
            )
            connection.execute(
                "UPDATE tasks SET status = 'interrupted', stage = 'interrupted', "
                "error_code = 'worker_stopped', error_message = ?, elapsed_seconds = ?, "
                "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND owner_id = ? AND status = 'running'",
                (interruption_message, max(0.0, elapsed_seconds), task_id, owner_id),
            )
        connection.execute(
            "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE slot_id = 1 AND active_task_id = ?",
            (task_id,),
        )
        return True


def _number(value: object, *, field: str, minimum: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError(500, "invalid_runner_result", f"Invalid {field} in runner result.")
    normalized = float(value)
    if not math.isfinite(normalized) or (minimum is not None and normalized < minimum):
        raise TaskError(500, "invalid_runner_result", f"Invalid {field} in runner result.")
    return normalized


def _validated_segments(segments: Sequence[dict[str, Any]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for sequence, segment in enumerate(segments):
        segment_id = segment.get("id")
        text = segment.get("text")
        if (
            not isinstance(segment_id, str)
            or not _SEGMENT_ID_RE.fullmatch(segment_id)
            or segment_id in seen_ids
            or not isinstance(text, str)
        ):
            raise TaskError(500, "invalid_runner_result", "The runner returned an invalid segment.")
        seen_ids.add(segment_id)
        start_seconds = _number(segment.get("start_seconds"), field="start_seconds", minimum=0)
        end_seconds = _number(segment.get("end_seconds"), field="end_seconds", minimum=0)
        if start_seconds is None or end_seconds is None:
            raise TaskError(500, "invalid_runner_result", "Segment timestamps are required.")
        if end_seconds < start_seconds:
            raise TaskError(500, "invalid_runner_result", "Segment timestamps are out of order.")
        avg_logprob = _number(segment.get("avg_logprob"), field="avg_logprob")
        no_speech_prob = _number(segment.get("no_speech_prob"), field="no_speech_prob", minimum=0)
        compression_ratio = _number(
            segment.get("compression_ratio"), field="compression_ratio", minimum=0
        )
        original_speaker_id = segment.get("original_speaker_id")
        speaker_id = segment.get("speaker_id", original_speaker_id)
        if (original_speaker_id is not None and not isinstance(original_speaker_id, str)) or (
            speaker_id is not None and not isinstance(speaker_id, str)
        ):
            raise TaskError(500, "invalid_runner_result", "Invalid speaker assignment.")
        if speaker_id != original_speaker_id:
            raise TaskError(
                500,
                "invalid_runner_result",
                "Initial displayed and original speaker assignments must match.",
            )
        overlap = segment.get("overlap", False)
        if not isinstance(overlap, bool):
            raise TaskError(500, "invalid_runner_result", "Invalid overlap marker.")
        speaker_dominance = _number(
            segment.get("speaker_dominance"), field="speaker_dominance", minimum=0
        )
        if speaker_dominance is not None and speaker_dominance > 1:
            raise TaskError(500, "invalid_runner_result", "Invalid speaker dominance.")
        review_reasons = segment.get("review_reasons", [])
        if (
            not isinstance(review_reasons, list)
            or any(
                not isinstance(reason, str) or reason not in _REVIEW_REASONS
                for reason in review_reasons
            )
            or len(set(review_reasons)) != len(review_reasons)
        ):
            raise TaskError(500, "invalid_runner_result", "Invalid review reasons.")
        supplied_uncertain = segment.get("uncertain", False)
        if not isinstance(supplied_uncertain, bool):
            raise TaskError(500, "invalid_runner_result", "Invalid uncertainty marker.")
        if no_speech_prob is not None and no_speech_prob > 1:
            raise TaskError(
                500, "invalid_runner_result", "Invalid no_speech_prob in runner result."
            )
        asr_uncertain = (
            (avg_logprob is not None and avg_logprob < -1.0)
            or (no_speech_prob is not None and no_speech_prob >= 0.6)
            or (compression_ratio is not None and compression_ratio > 2.4)
        )
        if asr_uncertain and "asr_uncertain" not in review_reasons:
            review_reasons = [*review_reasons, "asr_uncertain"]
        uncertain = supplied_uncertain or bool(review_reasons) or asr_uncertain
        rows.append(
            {
                "id": segment_id,
                "sequence": sequence,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "text": text,
                "original_speaker_id": original_speaker_id,
                "speaker_id": speaker_id,
                "avg_logprob": avg_logprob,
                "no_speech_prob": no_speech_prob,
                "compression_ratio": compression_ratio,
                "uncertain": int(uncertain),
                "review_reasons": json.dumps(review_reasons, separators=(",", ":")),
                "overlap": int(overlap),
                "speaker_dominance": speaker_dominance,
                "assignment_version": 1,
            }
        )
    return rows


def publish_complete_generation(
    data_dir: Path,
    task_id: str,
    *,
    owner_id: str,
    segments: Sequence[dict[str, Any]],
    speakers: Sequence[dict[str, Any]] = (),
    regular_turn_count: int = 0,
    exclusive_turn_count: int = 0,
    alignment_policy: str = ALIGNMENT_POLICY,
    diarization_model_name: str = DIARIZATION_MODEL_NAME,
    language_probability: object,
    elapsed_seconds: float,
) -> dict[str, object] | None:
    """Atomically publish a full generation and succeed only for its current owner."""
    rows = _validated_segments(segments)
    if (
        isinstance(regular_turn_count, bool)
        or not isinstance(regular_turn_count, int)
        or regular_turn_count < 0
        or isinstance(exclusive_turn_count, bool)
        or not isinstance(exclusive_turn_count, int)
        or exclusive_turn_count < 0
        or alignment_policy != ALIGNMENT_POLICY
        or diarization_model_name != DIARIZATION_MODEL_NAME
    ):
        raise TaskError(500, "invalid_runner_result", "Invalid diarization metadata.")
    normalized_speakers: list[dict[str, object]] = []
    speaker_ids: set[str] = set()
    display_names: set[str] = set()
    for speaker in speakers:
        speaker_id = speaker.get("speaker_id")
        display_name = speaker.get("display_name")
        first_seen = _number(
            speaker.get("first_seen_seconds"), field="first_seen_seconds", minimum=0
        )
        last_seen = _number(speaker.get("last_seen_seconds"), field="last_seen_seconds", minimum=0)
        if (
            not isinstance(speaker_id, str)
            or not speaker_id
            or speaker_id in speaker_ids
            or not isinstance(display_name, str)
            or not display_name
            or display_name in display_names
            or first_seen is None
            or last_seen is None
            or last_seen < first_seen
        ):
            raise TaskError(500, "invalid_runner_result", "Invalid generation speaker catalog.")
        speaker_ids.add(speaker_id)
        display_names.add(display_name)
        normalized_speakers.append(
            {
                "speaker_id": speaker_id,
                "display_name": display_name,
                "first_seen_seconds": first_seen,
                "last_seen_seconds": last_seen,
            }
        )
    if any(
        row["original_speaker_id"] is not None and row["original_speaker_id"] not in speaker_ids
        for row in rows
    ):
        raise TaskError(500, "invalid_runner_result", "A segment uses an unknown speaker.")
    normalized_language_probability = _number(
        language_probability, field="language_probability", minimum=0
    )
    if normalized_language_probability is not None and normalized_language_probability > 1:
        raise TaskError(500, "invalid_runner_result", "Invalid language probability.")
    latest_segment_end = max((float(row["end_seconds"]) for row in rows), default=0.0)

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        slot = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()
        if (
            task is None
            or task["status"] != "running"
            or task["owner_id"] != owner_id
            or task["stage"] != "publishing"
            or slot is None
            or slot["active_task_id"] != task_id
        ):
            return None

        generation_id = uuid.uuid4().hex
        generation_number = connection.execute(
            "SELECT COALESCE(MAX(generation_number), 0) + 1 FROM transcript_generations "
            "WHERE meeting_id = ?",
            (task["meeting_id"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, device, compute_type, language_probability, "
            "uncertainty_policy, segment_count, diarization_model_name, regular_turn_count, "
            "exclusive_turn_count, alignment_policy) "
            "VALUES (?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id,
                task["meeting_id"],
                task_id,
                generation_number,
                task["language_hint"],
                task["model_name"],
                task["device"],
                task["compute_type"],
                normalized_language_probability,
                UNCERTAINTY_POLICY,
                len(rows),
                diarization_model_name,
                regular_turn_count,
                exclusive_turn_count,
                alignment_policy,
            ),
        )
        connection.executemany(
            "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
            "first_seen_seconds, last_seen_seconds) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    generation_id,
                    speaker["speaker_id"],
                    speaker["display_name"],
                    speaker["first_seen_seconds"],
                    speaker["last_seen_seconds"],
                )
                for speaker in normalized_speakers
            ],
        )
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    generation_id,
                    row["sequence"],
                    row["start_seconds"],
                    row["end_seconds"],
                    row["text"],
                    row["original_speaker_id"],
                    row["speaker_id"],
                    row["avg_logprob"],
                    row["no_speech_prob"],
                    row["compression_ratio"],
                    row["uncertain"],
                    row["review_reasons"],
                    row["overlap"],
                    row["speaker_dominance"],
                    row["assignment_version"],
                )
                for row in rows
            ],
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = ? WHERE id = ?",
            (generation_id, task["meeting_id"]),
        )
        cursor = connection.execute(
            "UPDATE tasks SET status = 'succeeded', stage = 'complete', "
            "result_scope = 'transcript_only', transcript_end_seconds = ?, segment_count = ?, "
            "elapsed_seconds = ?, completed_at = CURRENT_TIMESTAMP, "
            "heartbeat_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND owner_id = ? AND status = 'running'",
            (
                latest_segment_end,
                len(rows),
                max(0.0, elapsed_seconds),
                task_id,
                owner_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskError(409, "task_ownership_changed", "Task ownership changed before publish.")
        connection.execute(
            "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE slot_id = 1 AND active_task_id = ?",
            (task_id,),
        )
        return {
            "id": generation_id,
            "meeting_id": task["meeting_id"],
            "generation_number": generation_number,
            "status": "complete",
            "segment_count": len(rows),
            "uncertainty_policy": UNCERTAINTY_POLICY,
            "alignment_policy": alignment_policy,
        }


def recover_abandoned_tasks(data_dir: Path) -> list[str]:
    """Cancel stopping tasks and interrupt other active work once the exclusive lock is held."""
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id FROM tasks WHERE status IN ('accepted', 'running', 'stopping')"
        ).fetchall()
        task_ids = [row["id"] for row in rows]
        connection.execute(
            "UPDATE tasks SET status = 'canceled', stage = 'canceled', "
            "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE status = 'stopping'"
        )
        connection.execute(
            "UPDATE tasks SET status = 'interrupted', stage = 'interrupted', "
            "error_code = 'worker_restarted', "
            "error_message = CASE kind WHEN 'notes' "
            "THEN 'The worker restarted before notes finished.' "
            "ELSE 'The worker restarted before transcription finished.' END, "
            "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('accepted', 'running')"
        )
        connection.execute(
            "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE slot_id = 1"
        )
    return task_ids


def _ordered_generation_speakers(
    connection: sqlite3.Connection, generation_id: str
) -> list[sqlite3.Row]:
    """Order model speakers by first appearance, then manual speakers by creation order."""
    return connection.execute(
        "SELECT speaker_id, display_name, first_seen_seconds, last_seen_seconds "
        "FROM generation_speakers WHERE generation_id = ? "
        "ORDER BY CASE WHEN substr(speaker_id, 1, 7) = 'MANUAL_' THEN 1 ELSE 0 END, "
        "CASE WHEN substr(speaker_id, 1, 7) != 'MANUAL_' THEN first_seen_seconds END, "
        "CASE WHEN substr(speaker_id, 1, 7) = 'MANUAL_' THEN rowid END, speaker_id",
        (generation_id,),
    ).fetchall()


def _generation_payload(
    connection: sqlite3.Connection, generation: sqlite3.Row
) -> dict[str, object]:
    segment_rows = connection.execute(
        "SELECT id, sequence, start_seconds, end_seconds, text, original_speaker_id, "
        "speaker_id, avg_logprob, no_speech_prob, compression_ratio, uncertain, "
        "review_reasons, overlap, speaker_dominance, assignment_version "
        "FROM transcript_segments WHERE generation_id = ? ORDER BY sequence",
        (generation["id"],),
    ).fetchall()
    speaker_rows = _ordered_generation_speakers(connection, generation["id"])
    segments = [
        {
            "id": row["id"],
            "sequence": row["sequence"],
            "start_seconds": row["start_seconds"],
            "end_seconds": row["end_seconds"],
            "text": row["text"],
            "original_speaker_id": row["original_speaker_id"],
            "speaker_id": row["speaker_id"],
            "avg_logprob": row["avg_logprob"],
            "no_speech_prob": row["no_speech_prob"],
            "compression_ratio": row["compression_ratio"],
            "uncertain": bool(row["uncertain"]),
            "review_reasons": json.loads(row["review_reasons"]),
            "overlap": bool(row["overlap"]),
            "speaker_dominance": row["speaker_dominance"],
            "assignment_version": row["assignment_version"],
        }
        for row in segment_rows
    ]
    speakers = [
        {
            "speaker_id": row["speaker_id"],
            "display_name": row["display_name"],
            "first_seen_seconds": row["first_seen_seconds"],
            "last_seen_seconds": row["last_seen_seconds"],
        }
        for row in speaker_rows
    ]
    return {
        "id": generation["id"],
        "generation_number": generation["generation_number"],
        "status": generation["status"],
        "language_hint": generation["language_hint"],
        "model_name": generation["model_name"],
        "device": generation["device"],
        "compute_type": generation["compute_type"],
        "language_probability": generation["language_probability"],
        "source_version": generation["source_version"],
        "uncertainty_policy": generation["uncertainty_policy"],
        "segment_count": generation["segment_count"],
        "completed_at": generation["completed_at"],
        "diarization_model_name": generation["diarization_model_name"],
        "regular_turn_count": generation["regular_turn_count"],
        "exclusive_turn_count": generation["exclusive_turn_count"],
        "alignment_policy": generation["alignment_policy"],
        "speakers": speakers,
        "segments": segments,
    }


def transcription_state(data_dir: Path, meeting_id: str) -> dict[str, object]:
    """Return transcription work, the current transcript, and read-only prior generations."""
    with _connect(data_dir) as connection:
        latest = connection.execute(
            "SELECT * FROM tasks WHERE meeting_id = ? AND kind = 'transcription' "
            "ORDER BY rowid DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        meeting = connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        generation_rows = connection.execute(
            "SELECT * FROM transcript_generations WHERE meeting_id = ? "
            "ORDER BY generation_number DESC",
            (meeting_id,),
        ).fetchall()
        current_generation_id = meeting["current_generation_id"] if meeting is not None else None
        current_row = next(
            (row for row in generation_rows if row["id"] == current_generation_id), None
        )
        generation_summary = (
            _generation_payload(connection, current_row) if current_row is not None else None
        )
        history = [
            _generation_payload(connection, row)
            for row in generation_rows
            if row["id"] != current_generation_id
        ]
    return {
        "task": _task_summary(latest) if latest is not None else None,
        "generation": generation_summary,
        "generation_history": history,
    }


def _expected_source_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TaskError(400, "invalid_source_version", "Provide a valid source version.")
    return value


def _current_generation_for_write(
    connection: sqlite3.Connection,
    meeting_id: str,
    expected_generation_id: object,
    expected_source_version: object,
) -> tuple[str, int]:
    if not isinstance(expected_generation_id, str) or not expected_generation_id:
        raise TaskError(400, "invalid_generation_id", "Provide the expected generation id.")
    version = _expected_source_version(expected_source_version)
    meeting = connection.execute(
        "SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        raise TaskError(404, "meeting_not_found", "Meeting not found.")
    active_notes = connection.execute(
        "SELECT 1 FROM tasks WHERE meeting_id = ? AND kind = 'notes' "
        "AND status IN ('accepted', 'running', 'stopping') LIMIT 1",
        (meeting_id,),
    ).fetchone()
    if active_notes is not None:
        raise TaskError(
            409,
            "notes_source_locked",
            "This transcript is locked while notes are being generated.",
        )
    if meeting["current_generation_id"] != expected_generation_id:
        raise TaskError(
            409,
            "current_generation_changed",
            "The current transcript changed. Reload before editing it.",
        )
    generation = connection.execute(
        "SELECT status, source_version FROM transcript_generations WHERE id = ? AND meeting_id = ?",
        (expected_generation_id, meeting_id),
    ).fetchone()
    if generation is None:
        raise TaskError(404, "generation_not_found", "Transcript generation not found.")
    if generation["status"] != "complete":
        raise TaskError(
            409,
            "generation_not_complete",
            "Only a completed transcript can be edited.",
        )
    if generation["source_version"] != version:
        raise TaskError(
            409,
            "source_version_changed",
            "The transcript source changed. Keep your draft, reload, and review it before saving.",
        )
    return expected_generation_id, version


def _bump_source_version(
    connection: sqlite3.Connection, generation_id: str, expected_source_version: int
) -> int:
    next_version = expected_source_version + 1
    updated = connection.execute(
        "UPDATE transcript_generations SET source_version = ? WHERE id = ? AND source_version = ?",
        (next_version, generation_id, expected_source_version),
    )
    if updated.rowcount != 1:
        raise TaskError(
            409,
            "source_version_changed",
            "The transcript source changed. Keep your draft, reload, and review it before saving.",
        )
    return next_version


def update_current_generation_segment_text(
    data_dir: Path,
    meeting_id: str,
    segment_id: object,
    *,
    expected_generation_id: object,
    expected_source_version: object,
    text: object,
) -> dict[str, object]:
    """Update only one segment's text in the current complete generation."""
    if not isinstance(segment_id, str) or not _SEGMENT_ID_RE.fullmatch(segment_id):
        raise TaskError(400, "invalid_segment_id", "Provide a valid segment id.")
    if not isinstance(text, str) or len(text) > 10000 or "\x00" in text:
        raise TaskError(
            400, "invalid_segment_text", "Segment text must be at most 10,000 characters."
        )

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        generation_id, source_version = _current_generation_for_write(
            connection, meeting_id, expected_generation_id, expected_source_version
        )
        segment = connection.execute(
            "SELECT text FROM transcript_segments WHERE id = ? AND generation_id = ?",
            (segment_id, generation_id),
        ).fetchone()
        if segment is None:
            raise TaskError(404, "segment_not_found", "Transcript segment not found.")
        if segment["text"] != text:
            updated = connection.execute(
                "UPDATE transcript_segments SET text = ? WHERE id = ? AND generation_id = ?",
                (text, segment_id, generation_id),
            )
            if updated.rowcount != 1:
                raise TaskError(404, "segment_not_found", "Transcript segment not found.")
            source_version = _bump_source_version(connection, generation_id, source_version)
        return {
            "id": segment_id,
            "generation_id": generation_id,
            "text": text,
            "source_version": source_version,
        }


def _speaker_display_name(
    connection: sqlite3.Connection, generation_id: str, speaker_id: str
) -> str:
    rows = _ordered_generation_speakers(connection, generation_id)
    position = next(
        (index + 1 for index, row in enumerate(rows) if row["speaker_id"] == speaker_id), None
    )
    if position is None:
        raise TaskError(404, "speaker_not_found", "Transcript speaker not found.")
    names = {row["display_name"] for row in rows if row["speaker_id"] != speaker_id}
    default_name = f"Speaker {position}"
    while default_name in names:
        position += 1
        default_name = f"Speaker {position}"
    return default_name


def _validate_speaker_name(value: object) -> str:
    if not isinstance(value, str):
        raise TaskError(400, "invalid_speaker_name", "Provide a speaker display name.")
    name = value.strip()
    if len(name) > 80 or "\x00" in name:
        raise TaskError(400, "invalid_speaker_name", "Speaker names must be at most 80 characters.")
    return name


def _assert_speaker_name_available(
    connection: sqlite3.Connection,
    generation_id: str,
    display_name: str,
    *,
    speaker_id: str | None = None,
) -> None:
    existing = connection.execute(
        "SELECT speaker_id FROM generation_speakers WHERE generation_id = ? AND display_name = ?",
        (generation_id, display_name),
    ).fetchone()
    if existing is not None and existing["speaker_id"] != speaker_id:
        raise TaskError(
            409,
            "speaker_name_conflict",
            "Another speaker in this transcript already uses that display name.",
        )


def add_current_generation_speaker(
    data_dir: Path,
    meeting_id: str,
    *,
    expected_generation_id: object,
    expected_source_version: object,
    display_name: object = "",
) -> dict[str, object]:
    """Add a speaker to the current complete generation using a backend-owned ID."""
    name = _validate_speaker_name(display_name)
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        generation_id, source_version = _current_generation_for_write(
            connection, meeting_id, expected_generation_id, expected_source_version
        )
        speaker_id = f"MANUAL_{uuid.uuid4().hex}"
        if not name:
            count = connection.execute(
                "SELECT COUNT(*) FROM generation_speakers WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()[0]
            name = f"Speaker {count + 1}"
            while connection.execute(
                "SELECT 1 FROM generation_speakers WHERE generation_id = ? AND display_name = ?",
                (generation_id, name),
            ).fetchone():
                count += 1
                name = f"Speaker {count + 1}"
        _assert_speaker_name_available(connection, generation_id, name)
        connection.execute(
            "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
            "first_seen_seconds, last_seen_seconds) VALUES (?, ?, ?, 0, 0)",
            (generation_id, speaker_id, name),
        )
        source_version = _bump_source_version(connection, generation_id, source_version)
        return {
            "generation_id": generation_id,
            "speaker_id": speaker_id,
            "display_name": name,
            "first_seen_seconds": 0,
            "last_seen_seconds": 0,
            "source_version": source_version,
        }


def update_current_generation_speaker_name(
    data_dir: Path,
    meeting_id: str,
    speaker_id: object,
    *,
    expected_generation_id: object,
    expected_source_version: object,
    display_name: object,
) -> dict[str, object]:
    """Rename or reset one speaker in the current complete generation."""
    if not isinstance(speaker_id, str) or not speaker_id or len(speaker_id) > 80:
        raise TaskError(400, "invalid_speaker_id", "Provide a speaker from the current generation.")
    requested_name = _validate_speaker_name(display_name)
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        generation_id, source_version = _current_generation_for_write(
            connection, meeting_id, expected_generation_id, expected_source_version
        )
        speaker = connection.execute(
            "SELECT display_name FROM generation_speakers "
            "WHERE generation_id = ? AND speaker_id = ?",
            (generation_id, speaker_id),
        ).fetchone()
        if speaker is None:
            raise TaskError(404, "speaker_not_found", "Transcript speaker not found.")
        name = requested_name or _speaker_display_name(connection, generation_id, speaker_id)
        _assert_speaker_name_available(connection, generation_id, name, speaker_id=speaker_id)
        if speaker["display_name"] != name:
            connection.execute(
                "UPDATE generation_speakers SET display_name = ? "
                "WHERE generation_id = ? AND speaker_id = ?",
                (name, generation_id, speaker_id),
            )
            source_version = _bump_source_version(connection, generation_id, source_version)
        return {
            "generation_id": generation_id,
            "speaker_id": speaker_id,
            "display_name": name,
            "source_version": source_version,
        }


def reassign_current_generation_speaker(
    data_dir: Path,
    meeting_id: str,
    segment_id: object,
    *,
    expected_generation_id: object,
    expected_source_version: object,
    expected_assignment_version: object,
    speaker_id: object,
) -> dict[str, object]:
    """Reassign one current-generation span using optimistic concurrency checks."""
    if not isinstance(segment_id, str) or not _SEGMENT_ID_RE.fullmatch(segment_id):
        raise TaskError(400, "invalid_segment_id", "Provide a valid segment id.")
    if not isinstance(expected_generation_id, str) or not expected_generation_id:
        raise TaskError(400, "invalid_generation_id", "Provide the expected generation id.")
    if (
        isinstance(expected_assignment_version, bool)
        or not isinstance(expected_assignment_version, int)
        or expected_assignment_version < 1
    ):
        raise TaskError(400, "invalid_assignment_version", "Provide a valid assignment version.")
    if speaker_id is not None and not isinstance(speaker_id, str):
        raise TaskError(400, "invalid_speaker_id", "Provide a speaker from the current generation.")

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        generation_id, source_version = _current_generation_for_write(
            connection, meeting_id, expected_generation_id, expected_source_version
        )
        segment = connection.execute(
            "SELECT id, generation_id, original_speaker_id, speaker_id, assignment_version "
            "FROM transcript_segments WHERE id = ? AND generation_id = ?",
            (segment_id, generation_id),
        ).fetchone()
        if segment is None:
            raise TaskError(404, "segment_not_found", "Transcript segment not found.")
        if segment["assignment_version"] != expected_assignment_version:
            raise TaskError(
                409,
                "assignment_version_changed",
                "The speaker assignment changed. Reload before trying again.",
            )
        if speaker_id is not None:
            exists = connection.execute(
                "SELECT 1 FROM generation_speakers WHERE generation_id = ? AND speaker_id = ?",
                (generation_id, speaker_id),
            ).fetchone()
            if exists is None:
                raise TaskError(
                    400, "invalid_speaker_id", "Provide a speaker from the current generation."
                )
        version = expected_assignment_version
        if segment["speaker_id"] != speaker_id:
            version += 1
            updated = connection.execute(
                "UPDATE transcript_segments SET speaker_id = ?, assignment_version = ? "
                "WHERE id = ? AND generation_id = ? AND assignment_version = ?",
                (
                    speaker_id,
                    version,
                    segment_id,
                    generation_id,
                    expected_assignment_version,
                ),
            )
            if updated.rowcount != 1:
                raise TaskError(
                    409,
                    "assignment_version_changed",
                    "The speaker assignment changed. Reload before trying again.",
                )
            source_version = _bump_source_version(connection, generation_id, source_version)
        return {
            "id": segment_id,
            "generation_id": generation_id,
            "original_speaker_id": segment["original_speaker_id"],
            "speaker_id": speaker_id,
            "assignment_version": version,
            "source_version": source_version,
        }
