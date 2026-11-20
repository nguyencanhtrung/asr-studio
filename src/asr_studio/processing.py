# ruff: noqa: E501

"""Durable automatic extension-recording processing orchestration.

This module is intentionally a small state layer around the existing task
workers and provider adapters.  It never performs inference, media conversion,
provider acquisition, or network I/O while a SQLite transaction is open.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from asr_studio import notes_service
from asr_studio.asr_models import AUTO, engine_of, resolve_run, selectable_models
from asr_studio.config import Settings
from asr_studio.diarization_runner import (
    DiarizationConfigurationError,
    prepared_diarization_directory,
)
from asr_studio.notes_document import PROMPT_VERSION, SCHEMA_VERSION
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory
from asr_studio.service_runtime import acquire_service_lock, write_service_heartbeat
from asr_studio.storage import database_path

MAX_WORKFLOWS = 32
MAX_STEPS = 64
MAX_RETRIES = 6
AUTOMATIC_ADMISSIONS_BEFORE_GUI_GRACE = 2
GUI_GRACE_SECONDS = 1.0
RETRY_BASE_SECONDS = 1.0
_ACTIVE = ("accepted", "running", "stopping")
# Waiting reasons that hold a step until the operator confirms its source.
ATTENTION_REASONS = frozenset({"source_confirmation_required", "interrupted_recording"})
_ASR_WAITING = {"worker_offline", "model_unavailable", "diarization_model_unavailable", "slot_busy"}
_NOTES_WAITING = {
    "provider_unconfigured",
    "provider_unavailable",
    "account_changed",
    "recipe_changed",
}


def _same_epoch(left: object, right: object) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


def _notes_effort_matches(
    provider: object, workflow_effort: object, provider_effort: object
) -> bool:
    """Compare the saved recipe with the provider's resolved task setting."""
    if workflow_effort == provider_effort:
        return True
    # Antigravity resolves its legacy/default setting to the explicit high level.
    return provider == "antigravity" and workflow_effort == "default" and provider_effort == "high"


class ProcessingError(RuntimeError):
    """An API-safe processing state transition failure."""

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


def _new_id() -> str:
    return uuid.uuid4().hex


def _automatic_request_id(workflow_id: str, kind: str, attempt_number: int) -> str:
    """Build a fresh automatic key without colliding with legacy epoch keys."""
    return f"auto:{workflow_id}:{kind}:attempt:{attempt_number}"


def _hash(*parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise ProcessingError(400, "invalid_request_id", "request_id must be 8 to 128 characters.")
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
        for character in value
    ):
        raise ProcessingError(
            400, "invalid_request_id", "request_id contains an unsupported character."
        )
    return value


def _prefs_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM processing_preferences WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("processing preferences are not initialized")
    return row


def processing_preferences(data_dir: Path) -> dict[str, object]:
    with _connect(data_dir) as connection:
        row = _prefs_row(connection)
    return {
        "asr": {"choice": row["asr_choice"], "language": row["asr_language"]},
        "notes": {
            "provider": row["notes_provider"],
            "model": row["notes_model"],
            "reasoning_effort": row["notes_reasoning_effort"],
            "output_language": row["notes_output_language"],
        },
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def validate_processing_preferences(
    *,
    asr_choice: object,
    asr_language: object,
    notes_provider: object,
    notes_model: object,
    notes_reasoning_effort: object,
    notes_output_language: object,
) -> dict[str, object]:
    if not isinstance(asr_choice, str) or asr_choice not in selectable_models():
        raise ProcessingError(400, "invalid_asr_choice", "Choose a supported ASR model or auto.")
    if not isinstance(asr_language, str) or asr_language not in {"en", "vi"}:
        raise ProcessingError(400, "invalid_asr_language", "Choose en or vi for ASR language.")
    if notes_provider is not None and notes_provider not in {"chatgpt", "antigravity"}:
        raise ProcessingError(
            400, "invalid_notes_provider", "Choose chatgpt, antigravity, or unset."
        )
    for field, value in (
        ("notes_model", notes_model),
        ("notes_reasoning_effort", notes_reasoning_effort),
    ):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise ProcessingError(400, f"invalid_{field}", f"{field} is invalid.")
    if not isinstance(notes_output_language, str) or notes_output_language not in {"en", "vi"}:
        raise ProcessingError(
            400, "invalid_notes_output_language", "Choose en or vi for notes output."
        )
    if notes_provider is not None and (notes_model is None or notes_reasoning_effort is None):
        raise ProcessingError(
            400,
            "invalid_notes_recipe",
            "A configured notes provider requires a model and reasoning effort.",
        )
    return {
        "asr_choice": asr_choice,
        "asr_language": asr_language,
        "notes_provider": notes_provider,
        "notes_model": notes_model,
        "notes_reasoning_effort": notes_reasoning_effort,
        "notes_output_language": notes_output_language,
    }


def save_processing_preferences(
    data_dir: Path,
    *,
    asr_choice: object = AUTO,
    asr_language: object = "vi",
    notes_provider: object = None,
    notes_model: object = None,
    notes_reasoning_effort: object = None,
    notes_output_language: object = "en",
) -> dict[str, object]:
    validate_processing_preferences(
        asr_choice=asr_choice,
        asr_language=asr_language,
        notes_provider=notes_provider,
        notes_model=notes_model,
        notes_reasoning_effort=notes_reasoning_effort,
        notes_output_language=notes_output_language,
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE processing_preferences SET asr_choice = ?, asr_language = ?, notes_provider = ?, "
            "notes_model = ?, notes_reasoning_effort = ?, notes_output_language = ?, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 1",
            (
                asr_choice,
                asr_language,
                notes_provider,
                notes_model,
                notes_reasoning_effort,
                notes_output_language,
            ),
        )
    return processing_preferences(data_dir)


def save_asr_preference(data_dir: Path, asr_choice: object) -> dict[str, object]:
    with _connect(data_dir) as connection:
        row = _prefs_row(connection)
    return save_processing_preferences(
        data_dir,
        asr_choice=asr_choice,
        asr_language=row["asr_language"],
        notes_provider=row["notes_provider"],
        notes_model=row["notes_model"],
        notes_reasoning_effort=row["notes_reasoning_effort"],
        notes_output_language=row["notes_output_language"],
    )


def _queue_event(
    connection: sqlite3.Connection, workflow_id: str, event_type: str, payload: dict[str, object]
) -> None:
    event_key = (
        f"{workflow_id}:{event_type}:{payload.get('epoch', '')}:{payload.get('generation_id', '')}"
    )
    connection.execute(
        "INSERT OR IGNORE INTO processing_outbox(id, event_key, workflow_id, event_type, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (_new_id(), event_key, workflow_id, event_type, json.dumps(payload, separators=(",", ":"))),
    )


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    step: sqlite3.Row,
    workflow_epoch: int,
    attempt_number: int,
    request_id: str,
    request_hash: str,
    state: str = "waiting",
) -> str:
    attempt_id = _new_id()
    connection.execute(
        "INSERT INTO processing_attempts(id, step_id, attempt_number, request_id, request_hash, workflow_epoch, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (attempt_id, step["id"], attempt_number, request_id, request_hash, workflow_epoch, state),
    )
    connection.execute(
        "UPDATE processing_steps SET attempt_number = ?, current_attempt_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (attempt_number, attempt_id, step["id"]),
    )
    return attempt_id


def _initial_attempts(connection: sqlite3.Connection, workflow_id: str, epoch: int) -> None:
    for kind in ("transcription", "notes"):
        step = connection.execute(
            "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = ?", (workflow_id, kind)
        ).fetchone()
        assert step is not None
        request_id = _automatic_request_id(workflow_id, kind, 1)
        _insert_attempt(
            connection,
            step=step,
            workflow_epoch=epoch,
            attempt_number=1,
            request_id=request_id,
            request_hash=_hash(workflow_id, kind, epoch, 1),
        )


def ready_handoff_tx(connection: sqlite3.Connection, session_id: str) -> str | None:
    """Create one workflow and its waiting steps in the ready-publication transaction."""
    row = connection.execute(
        "SELECT sessions.id AS session_id, sessions.state, sessions.language_hint AS session_language, "
        "sessions.language_hint_omitted, sessions.finalize_capture_complete, "
        "recordings.id AS recording_id, recordings.meeting_id, "
        "meetings.current_generation_id, meetings.deletion_requested_at, meetings.language_hint, "
        "meetings.metadata_version, meetings.notes_source_version, "
        "current_generation.source_version AS current_source_version, "
        "recordings.duration_seconds FROM extension_recording_sessions AS sessions "
        "JOIN recordings ON recordings.extension_session_id = sessions.id "
        "JOIN meetings ON meetings.id = recordings.meeting_id "
        "LEFT JOIN transcript_generations AS current_generation "
        "ON current_generation.id = meetings.current_generation_id "
        "WHERE sessions.id = ?",
        (session_id,),
    ).fetchone()
    if row is None or row["state"] != "ready" or row["deletion_requested_at"] is not None:
        return None
    existing = connection.execute(
        "SELECT id FROM processing_workflows WHERE extension_session_id = ?", (session_id,)
    ).fetchone()
    if existing is not None:
        return existing["id"]
    prefs = _prefs_row(connection)
    asr_language = (
        prefs["asr_language"] if row["language_hint_omitted"] else row["session_language"]
    )
    workflow_id = _new_id()
    # A saved interrupted prefix waits for an explicit start; it is never
    # processed automatically as if it were the complete meeting.
    interrupted = row["finalize_capture_complete"] == 0
    source_changed = (
        row["current_generation_id"] is not None
        or row["metadata_version"] != 1
        or row["notes_source_version"] != 1
    )
    state = "needs_attention" if interrupted or source_changed else "waiting"
    attention_reason = "source_confirmation_required" if source_changed else "interrupted_recording"
    connection.execute(
        "INSERT INTO processing_workflows(id, meeting_id, recording_id, extension_session_id, state, ready_sequence, "
        "asr_choice, asr_language, notes_provider, notes_model, notes_reasoning_effort, notes_output_language, "
        "preferences_version) "
        "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(ready_sequence), 0) + 1 FROM processing_workflows), ?, ?, ?, ?, ?, ?, ?)",
        (
            workflow_id,
            row["meeting_id"],
            row["recording_id"],
            session_id,
            state,
            prefs["asr_choice"],
            asr_language,
            prefs["notes_provider"],
            prefs["notes_model"],
            prefs["notes_reasoning_effort"],
            prefs["notes_output_language"],
            prefs["version"],
        ),
    )
    for kind in ("transcription", "notes"):
        step_id = _new_id()
        waiting_reason = attention_reason if state == "needs_attention" else None
        step_state = "needs_attention" if state == "needs_attention" else "waiting"
        connection.execute(
            "INSERT INTO processing_steps(id, workflow_id, kind, state, waiting_reason, "
            "source_generation_id, source_version, notes_source_version, metadata_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step_id,
                workflow_id,
                kind,
                step_state,
                waiting_reason,
                row["current_generation_id"],
                row["current_source_version"],
                row["notes_source_version"],
                row["metadata_version"],
            ),
        )
    _initial_attempts(connection, workflow_id, 1)
    _queue_event(connection, workflow_id, "ready", {"epoch": 1, "meeting_id": row["meeting_id"]})
    return workflow_id


def reconcile_ready_sources(data_dir: Path) -> int:
    created = 0
    with _connect(data_dir) as connection:
        sessions = connection.execute(
            "SELECT id FROM extension_recording_sessions WHERE state = 'ready' ORDER BY updated_at, id"
        ).fetchall()
    for session in sessions:
        with _connect(data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT 1 FROM processing_workflows WHERE extension_session_id = ?",
                (session["id"],),
            ).fetchone()
            ready_handoff_tx(connection, session["id"])
            if (
                before is None
                and connection.execute(
                    "SELECT 1 FROM processing_workflows WHERE extension_session_id = ?",
                    (session["id"],),
                ).fetchone()
                is not None
            ):
                created += 1
    return created


def _workflow_row(connection: sqlite3.Connection, meeting_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM processing_workflows WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()


def _task_summary(connection: sqlite3.Connection, task_id: str) -> dict[str, object]:
    from asr_studio.tasks import _task_summary as summarize

    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return summarize(row)


def processing_state(data_dir: Path, meeting_id: str) -> dict[str, object] | None:
    with _connect(data_dir) as connection:
        workflow = _workflow_row(connection, meeting_id)
        if workflow is None:
            return None
        steps = connection.execute(
            "SELECT * FROM processing_steps WHERE workflow_id = ? ORDER BY kind", (workflow["id"],)
        ).fetchall()
        attempts = connection.execute(
            "SELECT * FROM processing_attempts WHERE step_id IN (SELECT id FROM processing_steps WHERE workflow_id = ?) "
            "ORDER BY attempt_number",
            (workflow["id"],),
        ).fetchall()
        current_source = connection.execute(
            "SELECT meetings.current_generation_id, meetings.metadata_version, "
            "meetings.notes_source_version, generations.source_version "
            "FROM meetings LEFT JOIN transcript_generations AS generations "
            "ON generations.id = meetings.current_generation_id WHERE meetings.id = ?",
            (meeting_id,),
        ).fetchone()
    return {
        "id": workflow["id"],
        "meeting_id": meeting_id,
        "state": workflow["state"],
        "epoch": workflow["epoch"],
        "ready_sequence": workflow["ready_sequence"],
        "pinned": {
            "asr_choice": workflow["asr_choice"],
            "asr_language": workflow["asr_language"],
            "notes_provider": workflow["notes_provider"],
            "notes_model": workflow["notes_model"],
            "notes_reasoning_effort": workflow["notes_reasoning_effort"],
            "notes_output_language": workflow["notes_output_language"],
        },
        "preferences_version": workflow["preferences_version"],
        "current_source": {
            "generation_id": current_source["current_generation_id"],
            "source_version": current_source["source_version"],
            "metadata_version": current_source["metadata_version"],
            "notes_source_version": current_source["notes_source_version"],
        }
        if current_source is not None
        else None,
        "steps": [
            {
                "id": step["id"],
                "kind": step["kind"],
                "state": step["state"],
                "waiting_reason": step["waiting_reason"],
                "task_id": step["task_id"],
                "attempt_number": step["attempt_number"],
                "source_snapshot_id": step["source_snapshot_id"],
                "source_generation_id": step["source_generation_id"],
                "source_version": step["source_version"],
                "notes_source_version": step["notes_source_version"],
                "metadata_version": step["metadata_version"],
                "recipe_version": step["recipe_version"],
            }
            for step in steps
        ],
        "attempts": [
            {
                "id": attempt["id"],
                "step_id": attempt["step_id"],
                "attempt_number": attempt["attempt_number"],
                "request_id": attempt["request_id"],
                "state": attempt["state"],
                "task_id": attempt["task_id"],
                "error_code": attempt["error_code"],
            }
            for attempt in attempts
        ],
        "actions": {
            "cancel": workflow["state"] not in {"canceled", "deleted", "succeeded"},
            "retry_notes": workflow["state"] not in {"canceled", "deleted"}
            and any(step["kind"] == "notes" and step["source_snapshot_id"] for step in steps),
            "apply_current_settings": workflow["state"] in {"waiting", "needs_attention"},
            "use_current_source": workflow["state"] == "needs_attention",
        },
    }


def _mark_waiting(
    connection: sqlite3.Connection, step_id: str, reason: str | None, *, message: str | None = None
) -> None:
    connection.execute(
        "UPDATE processing_steps SET waiting_reason = ?, last_error_message = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND state IN ('waiting', 'failed', 'needs_attention')",
        (reason, message, step_id),
    )


def _current_source_row(connection: sqlite3.Connection, meeting_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT meetings.current_generation_id, generations.source_version, "
        "meetings.notes_source_version, meetings.metadata_version "
        "FROM meetings LEFT JOIN transcript_generations AS generations "
        "ON generations.id = meetings.current_generation_id WHERE meetings.id = ?",
        (meeting_id,),
    ).fetchone()


def _source_matches_handoff(
    connection: sqlite3.Connection, workflow_id: str, step: sqlite3.Row | None = None
) -> bool:
    if step is None:
        step = connection.execute(
            "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'transcription'",
            (workflow_id,),
        ).fetchone()
    workflow = connection.execute(
        "SELECT meeting_id FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    source = (
        _current_source_row(connection, workflow["meeting_id"]) if workflow is not None else None
    )
    return bool(
        step is not None
        and source is not None
        and step["source_generation_id"] == source["current_generation_id"]
        and step["source_version"] == source["source_version"]
        and step["notes_source_version"] == source["notes_source_version"]
        and step["metadata_version"] == source["metadata_version"]
    )


def _mark_source_confirmation_required(connection: sqlite3.Connection, workflow_id: str) -> None:
    message = "The current transcript or meeting details changed. Confirm the displayed source."
    connection.execute(
        "UPDATE processing_workflows SET state = 'needs_attention', "
        "last_error_code = 'source_confirmation_required', last_error_message = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND state NOT IN ('canceled', 'deleted')",
        (message, workflow_id),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'needs_attention', "
        "waiting_reason = 'source_confirmation_required', last_error_code = ?, "
        "last_error_message = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE workflow_id = ? AND kind = 'transcription' "
        "AND state IN ('waiting', 'accepted', 'running', 'failed')",
        ("source_confirmation_required", message, workflow_id),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'needs_attention', "
        "waiting_reason = 'source_confirmation_required', last_error_code = ?, "
        "last_error_message = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE workflow_id = ? AND kind = 'notes' AND source_snapshot_id IS NULL "
        "AND state IN ('waiting', 'accepted', 'running', 'failed')",
        ("source_confirmation_required", message, workflow_id),
    )


def _assert_actionable_workflow(
    connection: sqlite3.Connection, workflow: sqlite3.Row, *, allow_succeeded: bool = False
) -> None:
    meeting = connection.execute(
        "SELECT deletion_requested_at FROM meetings WHERE id = ?", (workflow["meeting_id"],)
    ).fetchone()
    if meeting is None or meeting["deletion_requested_at"] is not None:
        raise ProcessingError(409, "meeting_deletion_pending", "This meeting is being deleted.")
    if workflow["state"] in {"canceled", "deleted"}:
        raise ProcessingError(
            409, "processing_canceled", "This processing workflow is no longer active."
        )
    if not allow_succeeded and workflow["state"] == "succeeded":
        raise ProcessingError(
            409, "processing_complete", "This processing workflow is already complete."
        )
    if (
        connection.execute(
            "SELECT 1 FROM tasks WHERE processing_workflow_id = ? AND status IN ('accepted', 'running', 'stopping')",
            (workflow["id"],),
        ).fetchone()
        is not None
    ):
        raise ProcessingError(
            409, "processing_active", "Wait for the active processing step to finish."
        )


def _automatic_task_common(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    epoch: int,
    request_id: str,
    request_hash: str,
    kind: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, str] | dict[str, object] | None:
    """Return workflow/step/attempt or a replay summary; called inside BEGIN IMMEDIATE."""
    workflow = connection.execute(
        "SELECT * FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    step = connection.execute(
        "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = ?", (workflow_id, kind)
    ).fetchone()
    if workflow is None or step is None:
        raise ProcessingError(404, "processing_not_found", "Processing workflow not found.")
    if workflow["epoch"] != epoch:
        raise ProcessingError(
            409, "processing_epoch_changed", "Processing settings changed; reload and retry."
        )
    if workflow["state"] in {"canceled", "deleted"} or step["state"] in {
        "canceled",
        "needs_attention",
    }:
        raise ProcessingError(
            409, "processing_canceled", "This processing workflow is no longer active."
        )
    attempt = (
        connection.execute(
            "SELECT * FROM processing_attempts WHERE id = ?", (step["current_attempt_id"],)
        ).fetchone()
        if step["current_attempt_id"]
        else None
    )
    if attempt is not None and attempt["request_id"] != request_id:
        raise ProcessingError(
            409, "processing_attempt_changed", "Another attempt is already pending."
        )
    existing = connection.execute(
        "SELECT * FROM tasks WHERE request_id = ?", (request_id,)
    ).fetchone()
    if existing is not None:
        if attempt is None or attempt["state"] not in {"waiting", "accepted", "running"}:
            raise ProcessingError(
                409,
                "processing_attempt_changed",
                "The current processing attempt is no longer waiting.",
            )
        if existing["request_hash"] != request_hash:
            raise ProcessingError(
                409, "idempotency_conflict", "This request_id was already used differently."
            )
        return {"task_id": existing["id"], "replay": True}
    if attempt is None:
        attempt_id = _insert_attempt(
            connection,
            step=step,
            workflow_epoch=epoch,
            attempt_number=max(step["attempt_number"], 0) + 1,
            request_id=request_id,
            request_hash=request_hash,
        )
        attempt = connection.execute(
            "SELECT * FROM processing_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    elif attempt["state"] != "waiting":
        raise ProcessingError(
            409,
            "processing_attempt_changed",
            "The current processing attempt is no longer waiting.",
        )
    return workflow, step, attempt, "new"


def admit_automatic_transcription(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    epoch: int,
    request_id: str,
    request_hash: str,
    model_path: Path,
    asr_model: str,
    language_hint: str,
    device: str,
    compute_type: str,
    diarization_model_path: Path,
    duration_seconds: float,
) -> dict[str, object] | None:
    replay_or_rows = _automatic_task_common(
        connection,
        workflow_id=workflow_id,
        epoch=epoch,
        request_id=request_id,
        request_hash=request_hash,
        kind="transcription",
    )
    if replay_or_rows is None or isinstance(replay_or_rows, dict):
        return replay_or_rows
    workflow, step, attempt, _ = replay_or_rows
    if not _source_matches_handoff(connection, workflow["id"], step):
        _mark_source_confirmation_required(connection, workflow["id"])
        return None
    recording = connection.execute(
        "SELECT storage_key, content_type, duration_seconds FROM recordings WHERE id = ?",
        (workflow["recording_id"],),
    ).fetchone()
    if (
        recording is None
        or recording["storage_key"] is None
        or recording["content_type"] is None
        or recording["duration_seconds"] is None
    ):
        _mark_waiting(connection, step["id"], "recording_not_ready")
        return None
    slot = connection.execute(
        "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
    ).fetchone()
    if slot is None or slot["active_task_id"] is not None:
        _mark_waiting(connection, step["id"], "slot_busy")
        return None
    task_id = _new_id()
    connection.execute(
        "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, diarization_model_path, asr_model, model_name, device, compute_type, language_hint, status, stage, audio_duration_seconds, owner_role, processing_workflow_id, processing_step_id, processing_attempt_id, processing_epoch) "
        "VALUES (?, ?, ?, ?, 'transcription', ?, ?, ?, ?, ?, ?, ?, 'accepted', 'accepted', ?, 'transcription', ?, ?, ?, ?)",
        (
            task_id,
            request_id,
            request_hash,
            workflow["meeting_id"],
            str(model_path),
            str(diarization_model_path),
            asr_model,
            Path(model_path).name,
            device,
            compute_type,
            language_hint,
            duration_seconds,
            workflow_id,
            step["id"],
            attempt["id"],
            epoch,
        ),
    )
    if (
        connection.execute(
            "UPDATE processing_slot SET active_task_id = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = 1 AND active_task_id IS NULL",
            (task_id,),
        ).rowcount
        != 1
    ):
        raise ProcessingError(409, "system_busy", "Another task owns the processing slot.")
    connection.execute(
        "UPDATE processing_attempts SET task_id = ?, state = 'accepted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task_id, attempt["id"]),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'accepted', waiting_reason = NULL, task_id = ?, asr_choice = ?, asr_language = ?, asr_model = ?, asr_model_path = ?, device = ?, compute_type = ?, diarization_model_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (
            task_id,
            workflow["asr_choice"],
            language_hint,
            asr_model,
            str(model_path),
            device,
            compute_type,
            str(diarization_model_path),
            step["id"],
        ),
    )
    connection.execute(
        "UPDATE processing_workflows SET state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND epoch = ?",
        (workflow_id, epoch),
    )
    return {"task_id": task_id, "replay": False}


def admit_automatic_notes(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    epoch: int,
    request_id: str,
    request_hash: str,
    pin: dict[str, object],
) -> dict[str, object] | None:
    replay_or_rows = _automatic_task_common(
        connection,
        workflow_id=workflow_id,
        epoch=epoch,
        request_id=request_id,
        request_hash=request_hash,
        kind="notes",
    )
    if replay_or_rows is None or isinstance(replay_or_rows, dict):
        return replay_or_rows
    workflow, step, attempt, _ = replay_or_rows
    snapshot_id = step["source_snapshot_id"]
    if snapshot_id is None:
        _mark_waiting(connection, step["id"], "transcript_required")
        return None
    if workflow["notes_provider"] != pin.get("provider") or workflow["notes_model"] is None:
        _mark_waiting(
            connection,
            step["id"],
            "provider_unconfigured" if workflow["notes_model"] is None else "recipe_changed",
        )
        return None
    if workflow["notes_model"] != pin.get("model"):
        _mark_waiting(connection, step["id"], "recipe_changed")
        return None
    if not _notes_effort_matches(
        workflow["notes_provider"],
        workflow["notes_reasoning_effort"],
        pin.get("reasoning_effort"),
    ):
        _mark_waiting(connection, step["id"], "recipe_changed")
        return None
    if step["notes_connection_version"] is not None and not _same_epoch(
        step["notes_connection_version"], pin.get("connection_version")
    ):
        _mark_waiting(connection, step["id"], "account_changed")
        return None
    slot = connection.execute(
        "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
    ).fetchone()
    if slot is None or slot["active_task_id"] is not None:
        _mark_waiting(connection, step["id"], "slot_busy")
        return None
    recording = connection.execute(
        "SELECT duration_seconds FROM recordings WHERE id = ?", (workflow["recording_id"],)
    ).fetchone()
    meeting = connection.execute(
        "SELECT language_hint FROM meetings WHERE id = ?", (workflow["meeting_id"],)
    ).fetchone()
    source = connection.execute(
        "SELECT snapshot_json FROM notes_sources WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if recording is None or meeting is None or source is None:
        _mark_waiting(connection, step["id"], "source_unavailable")
        return None
    try:
        segment_count = len(json.loads(source["snapshot_json"])["segments"])
    except (TypeError, KeyError, ValueError):
        _mark_waiting(connection, step["id"], "source_unavailable")
        return None
    task_id = _new_id()
    connection.execute(
        "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, model_name, device, compute_type, language_hint, status, stage, result_scope, audio_duration_seconds, segment_count, notes_provider, notes_connection_version, notes_model, notes_reasoning_effort, notes_prompt_version, notes_schema_version, notes_source_id, notes_output_language, owner_role, processing_workflow_id, processing_step_id, processing_attempt_id, processing_epoch) "
        "VALUES (?, ?, ?, ?, 'notes', '', ?, 'auto', 'provider', ?, 'accepted', 'accepted', 'notes_only', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'notes', ?, ?, ?, ?)",
        (
            task_id,
            request_id,
            request_hash,
            workflow["meeting_id"],
            pin["model"],
            meeting["language_hint"],
            recording["duration_seconds"],
            segment_count,
            pin["provider"],
            pin["connection_version"],
            pin["model"],
            pin["reasoning_effort"],
            PROMPT_VERSION,
            SCHEMA_VERSION,
            snapshot_id,
            workflow["notes_output_language"],
            workflow_id,
            step["id"],
            attempt["id"],
            epoch,
        ),
    )
    if (
        connection.execute(
            "UPDATE processing_slot SET active_task_id = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = 1 AND active_task_id IS NULL",
            (task_id,),
        ).rowcount
        != 1
    ):
        raise ProcessingError(409, "system_busy", "Another task owns the processing slot.")
    connection.execute(
        "UPDATE processing_attempts SET task_id = ?, state = 'accepted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task_id, attempt["id"]),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'accepted', waiting_reason = NULL, task_id = ?, notes_connection_version = ?, notes_provider = ?, notes_model = ?, notes_reasoning_effort = ?, notes_output_language = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (
            task_id,
            pin["connection_version"],
            pin["provider"],
            pin["model"],
            pin["reasoning_effort"],
            workflow["notes_output_language"],
            step["id"],
        ),
    )
    return {"task_id": task_id, "replay": False}


def _freeze_notes_source(
    connection: sqlite3.Connection, meeting_id: str, generation_id: str
) -> str:
    from asr_studio.notes import _source_snapshot

    meeting = connection.execute(
        "SELECT id, meeting_date, language_hint, metadata_version, notes_source_version FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    generation = connection.execute(
        "SELECT id, source_version FROM transcript_generations WHERE id = ? AND meeting_id = ?",
        (generation_id, meeting_id),
    ).fetchone()
    if meeting is None or generation is None:
        raise RuntimeError("cannot freeze a missing transcript source")
    snapshot_id = _new_id()
    snapshot = _source_snapshot(
        connection, meeting=meeting, generation=generation, snapshot_id=snapshot_id
    )
    connection.execute(
        "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, notes_source_version, meeting_date, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            meeting_id,
            generation_id,
            generation["source_version"],
            meeting["notes_source_version"],
            meeting["meeting_date"],
            json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        ),
    )
    return snapshot_id


def transcript_published_tx(
    connection: sqlite3.Connection, task: sqlite3.Row, generation_id: str
) -> None:
    workflow_id = task["processing_workflow_id"]
    if workflow_id is None:
        return
    workflow = connection.execute(
        "SELECT * FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    if (
        workflow is None
        or not _same_epoch(workflow["epoch"], task["processing_epoch"])
        or workflow["state"] in {"canceled", "deleted"}
    ):
        raise ProcessingError(
            409, "processing_canceled", "The processing workflow was canceled before publication."
        )
    snapshot_id = _freeze_notes_source(connection, task["meeting_id"], generation_id)
    transcription = connection.execute(
        "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'transcription'",
        (workflow_id,),
    ).fetchone()
    notes = connection.execute(
        "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'", (workflow_id,)
    ).fetchone()
    connection.execute(
        "UPDATE processing_attempts SET state = 'succeeded', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task["processing_attempt_id"],),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'succeeded', waiting_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (transcription["id"],),
    )
    reason = "provider_unconfigured" if workflow["notes_provider"] is None else None
    connection.execute(
        "UPDATE processing_steps SET state = 'waiting', waiting_reason = ?, source_snapshot_id = ?, source_generation_id = ?, source_version = (SELECT source_version FROM transcript_generations WHERE id = ?), notes_source_version = (SELECT notes_source_version FROM meetings WHERE id = ?), metadata_version = (SELECT metadata_version FROM meetings WHERE id = ?), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (
            reason,
            snapshot_id,
            generation_id,
            generation_id,
            task["meeting_id"],
            task["meeting_id"],
            notes["id"],
        ),
    )
    connection.execute(
        "UPDATE processing_workflows SET state = 'waiting', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (workflow_id,),
    )
    _queue_event(
        connection,
        workflow_id,
        "transcript_ready",
        {
            "epoch": workflow["epoch"],
            "generation_id": generation_id,
            "source_snapshot_id": snapshot_id,
        },
    )


def publication_allowed_tx(connection: sqlite3.Connection, task: sqlite3.Row) -> bool:
    """Fence a runner immediately before it writes a generation/revision."""
    workflow_id = task["processing_workflow_id"]
    if workflow_id is None:
        return True
    workflow = connection.execute(
        "SELECT * FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    allowed = bool(
        workflow is not None
        and _same_epoch(workflow["epoch"], task["processing_epoch"])
        and workflow["state"] not in {"canceled", "deleted", "needs_attention"}
        and connection.execute(
            "SELECT deletion_requested_at FROM meetings WHERE id = ?", (task["meeting_id"],)
        ).fetchone()[0]
        is None
    )
    if not allowed:
        return False
    if task["kind"] == "transcription":
        step = connection.execute(
            "SELECT * FROM processing_steps WHERE id = ?", (task["processing_step_id"],)
        ).fetchone()
        if not _source_matches_handoff(connection, workflow_id, step):
            _mark_source_confirmation_required(connection, workflow_id)
            return False
    return True


def publication_blocked_tx(connection: sqlite3.Connection, task: sqlite3.Row) -> None:
    """Release an automatic ASR task blocked by a changed source fence."""
    if task["processing_workflow_id"] is None or task["kind"] != "transcription":
        return
    workflow = connection.execute(
        "SELECT state FROM processing_workflows WHERE id = ?",
        (task["processing_workflow_id"],),
    ).fetchone()
    if workflow is None or workflow["state"] != "needs_attention":
        return
    message = "The current transcript or meeting details changed. Confirm the displayed source."
    cursor = connection.execute(
        "UPDATE tasks SET status = 'interrupted', stage = 'interrupted', "
        "error_code = 'source_confirmation_required', error_message = ?, "
        "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'running'",
        (message, task["id"]),
    )
    if cursor.rowcount != 1:
        return
    connection.execute(
        "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP "
        "WHERE slot_id = 1 AND active_task_id = ?",
        (task["id"],),
    )
    automatic_task_terminal_tx(
        connection,
        task,
        "interrupted",
        "source_confirmation_required",
        message,
    )


def notes_published_tx(connection: sqlite3.Connection, task: sqlite3.Row, revision_id: str) -> None:
    workflow_id = task["processing_workflow_id"]
    if workflow_id is None:
        return
    workflow = connection.execute(
        "SELECT * FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    if (
        workflow is None
        or not _same_epoch(workflow["epoch"], task["processing_epoch"])
        or workflow["state"] in {"canceled", "deleted"}
    ):
        raise ProcessingError(
            409, "processing_canceled", "The processing workflow was canceled before publication."
        )
    connection.execute(
        "UPDATE processing_attempts SET state = 'succeeded', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task["processing_attempt_id"],),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'succeeded', waiting_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task["processing_step_id"],),
    )
    connection.execute(
        "UPDATE processing_workflows SET state = 'succeeded', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (workflow_id,),
    )
    _queue_event(
        connection,
        workflow_id,
        "notes_ready",
        {"epoch": workflow["epoch"], "revision_id": revision_id},
    )


def automatic_task_started_tx(
    connection: sqlite3.Connection,
    task_id: str,
    workflow_id: str,
    step_id: str,
    attempt_id: str,
    epoch: int,
) -> bool:
    workflow = connection.execute(
        "SELECT state, epoch FROM processing_workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    if (
        workflow is None
        or not _same_epoch(workflow["epoch"], epoch)
        or workflow["state"] in {"canceled", "deleted"}
    ):
        return False
    connection.execute(
        "UPDATE processing_attempts SET state = 'running', execution_no = execution_no + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (attempt_id,),
    )
    connection.execute(
        "UPDATE processing_steps SET state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (step_id,),
    )
    connection.execute(
        "UPDATE processing_workflows SET state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (workflow_id,),
    )
    return True


def automatic_task_terminal_tx(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    state: str,
    error_code: str | None,
    message: str | None,
) -> None:
    if task["processing_workflow_id"] is None:
        return
    attempt_state = (
        "canceled"
        if state == "canceled"
        else ("interrupted" if state == "interrupted" else "failed")
    )
    workflow = connection.execute(
        "SELECT state FROM processing_workflows WHERE id = ?",
        (task["processing_workflow_id"],),
    ).fetchone()
    source_confirmation = (
        workflow is not None
        and workflow["state"] == "needs_attention"
        and error_code == "source_confirmation_required"
    )
    connection.execute(
        "UPDATE processing_attempts SET state = ?, error_code = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (attempt_state, error_code, message, task["processing_attempt_id"]),
    )
    connection.execute(
        "UPDATE processing_steps SET state = ?, waiting_reason = ?, last_error_code = ?, last_error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (
            "needs_attention"
            if source_confirmation
            else ("canceled" if state == "canceled" else "failed"),
            "source_confirmation_required"
            if source_confirmation
            else (None if state == "canceled" else error_code),
            error_code,
            message,
            task["processing_step_id"],
        ),
    )
    if source_confirmation:
        return
    if state == "canceled":
        connection.execute(
            "UPDATE processing_workflows SET state = 'canceled', epoch = epoch + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND state NOT IN ('canceled', 'deleted')",
            (task["processing_workflow_id"],),
        )
    else:
        connection.execute(
            "UPDATE processing_workflows SET state = 'waiting', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND state = 'running'",
            (task["processing_workflow_id"],),
        )


def cancel_workflow(
    data_dir: Path, meeting_id: str, *, request_id: object, expected_epoch: object
) -> dict[str, object]:
    request_id = _request_id(request_id)
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 1
    ):
        raise ProcessingError(
            400, "invalid_processing_epoch", "Provide the current processing epoch."
        )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        workflow = _workflow_row(connection, meeting_id)
        if workflow is None:
            raise ProcessingError(404, "processing_not_found", "Processing workflow not found.")
        replay = connection.execute(
            "SELECT 1 FROM processing_outbox WHERE event_key = ?",
            (f"cancel:{meeting_id}:{request_id}",),
        ).fetchone()
        if replay is None and not _same_epoch(workflow["epoch"], expected_epoch):
            raise ProcessingError(
                409, "processing_epoch_changed", "Processing changed; reload before canceling."
            )
        if replay is None:
            connection.execute(
                "UPDATE processing_workflows SET state = 'canceled', epoch = epoch + 1, last_error_code = 'canceled_by_operator', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE processing_steps SET state = 'canceled', waiting_reason = 'canceled_by_operator', updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ? AND state IN ('waiting', 'accepted', 'failed', 'needs_attention')",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE processing_attempts SET state = 'canceled', updated_at = CURRENT_TIMESTAMP WHERE step_id IN (SELECT id FROM processing_steps WHERE workflow_id = ?) AND state IN ('waiting', 'accepted')",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE tasks SET status = 'canceled', stage = 'canceled', cancel_requested_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE processing_workflow_id = ? AND status = 'accepted'",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE tasks SET status = 'stopping', stage = 'stopping', "
                "cancel_requested_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE processing_workflow_id = ? AND status = 'running'",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE processing_slot SET active_task_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE active_task_id IN (SELECT id FROM tasks WHERE processing_workflow_id = ? AND status = 'canceled')",
                (workflow["id"],),
            )
            _queue_event(connection, workflow["id"], "canceled", {"epoch": workflow["epoch"] + 1})
            connection.execute(
                "UPDATE processing_outbox SET event_key = ? WHERE workflow_id = ? AND event_type = 'canceled' AND processed_at IS NULL",
                (f"cancel:{meeting_id}:{request_id}", workflow["id"]),
            )
    return processing_state(data_dir, meeting_id) or {}


def retry_notes(
    data_dir: Path,
    meeting_id: str,
    *,
    request_id: object,
    expected_epoch: object,
    source_snapshot_id: object,
    confirmed_recipe_version: object,
) -> dict[str, object]:
    request_id = _request_id(request_id)
    if not isinstance(source_snapshot_id, str) or len(source_snapshot_id) != 32:
        raise ProcessingError(
            400, "invalid_source_snapshot", "Provide the frozen source snapshot id."
        )
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 1
        or isinstance(confirmed_recipe_version, bool)
        or not isinstance(confirmed_recipe_version, int)
        or confirmed_recipe_version < 1
    ):
        raise ProcessingError(
            400, "invalid_processing_version", "Provide the current processing versions."
        )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        workflow = _workflow_row(connection, meeting_id)
        if workflow is None:
            raise ProcessingError(404, "processing_not_found", "Processing workflow not found.")
        _assert_actionable_workflow(connection, workflow, allow_succeeded=True)
        if not _same_epoch(workflow["epoch"], expected_epoch):
            raise ProcessingError(
                409, "processing_epoch_changed", "Processing changed; reload and retry."
            )
        step = connection.execute(
            "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'",
            (workflow["id"],),
        ).fetchone()
        if step is None or step["source_snapshot_id"] != source_snapshot_id:
            raise ProcessingError(
                409,
                "source_confirmation_required",
                "Confirm the frozen source before retrying notes.",
            )
        if step["recipe_version"] != confirmed_recipe_version:
            raise ProcessingError(
                409, "recipe_changed", "Confirm the current notes recipe before retrying."
            )
        active = connection.execute(
            "SELECT 1 FROM tasks WHERE processing_workflow_id = ? AND status IN ('accepted', 'running', 'stopping')",
            (workflow["id"],),
        ).fetchone()
        if active is not None:
            raise ProcessingError(
                409, "processing_active", "Wait for the current processing step to finish."
            )
        new_epoch = workflow["epoch"] + 1
        connection.execute(
            "UPDATE processing_workflows SET epoch = ?, state = 'waiting', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_epoch, workflow["id"]),
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'waiting', waiting_reason = NULL, task_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (step["id"],),
        )
        step = connection.execute(
            "SELECT * FROM processing_steps WHERE id = ?", (step["id"],)
        ).fetchone()
        attempt_id = _insert_attempt(
            connection,
            step=step,
            workflow_epoch=new_epoch,
            attempt_number=step["attempt_number"] + 1,
            request_id=request_id,
            request_hash=_hash(meeting_id, "notes", source_snapshot_id, new_epoch),
        )
        connection.execute(
            "UPDATE processing_steps SET current_attempt_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (attempt_id, step["id"]),
        )
    return processing_state(data_dir, meeting_id) or {}


def apply_current_settings(
    data_dir: Path,
    meeting_id: str,
    *,
    expected_epoch: object,
    expected_preferences_version: object,
    confirmed_pin: dict[str, object] | None = None,
) -> dict[str, object]:
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 1
    ):
        raise ProcessingError(
            400, "invalid_processing_epoch", "Provide the current processing epoch."
        )
    if (
        isinstance(expected_preferences_version, bool)
        or not isinstance(expected_preferences_version, int)
        or expected_preferences_version < 1
    ):
        raise ProcessingError(
            400,
            "invalid_preferences_version",
            "Provide the preferences revision shown to the operator.",
        )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        workflow = _workflow_row(connection, meeting_id)
        if workflow is None:
            raise ProcessingError(404, "processing_not_found", "Processing workflow not found.")
        _assert_actionable_workflow(connection, workflow)
        if not _same_epoch(workflow["epoch"], expected_epoch):
            raise ProcessingError(
                409, "processing_epoch_changed", "Processing changed; reload and retry."
            )
        prefs = _prefs_row(connection)
        if not _same_epoch(prefs["version"], expected_preferences_version):
            raise ProcessingError(
                409,
                "preferences_changed",
                "Processing preferences changed; reload and confirm them.",
            )
        if prefs["notes_provider"] is not None and (
            confirmed_pin is None
            or any(
                confirmed_pin.get(key) != prefs[column]
                for key, column in (
                    ("provider", "notes_provider"),
                    ("model", "notes_model"),
                )
            )
            or not _notes_effort_matches(
                prefs["notes_provider"],
                prefs["notes_reasoning_effort"],
                confirmed_pin.get("reasoning_effort"),
            )
        ):
            raise ProcessingError(
                409,
                "provider_confirmation_required",
                "Confirm the selected provider account and recipe before applying settings.",
            )
        notes_step = connection.execute(
            "SELECT source_snapshot_id FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'",
            (workflow["id"],),
        ).fetchone()
        next_state = (
            "needs_attention"
            if workflow["state"] == "needs_attention"
            and (notes_step is None or notes_step["source_snapshot_id"] is None)
            else "waiting"
        )
        new_epoch = expected_epoch + 1
        connection.execute(
            "UPDATE processing_workflows SET epoch = ?, state = ?, asr_choice = ?, asr_language = ?, notes_provider = ?, notes_model = ?, notes_reasoning_effort = ?, notes_output_language = ?, preferences_version = ?, last_error_code = NULL, last_error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                new_epoch,
                next_state,
                prefs["asr_choice"],
                prefs["asr_language"],
                prefs["notes_provider"],
                prefs["notes_model"],
                prefs["notes_reasoning_effort"],
                prefs["notes_output_language"],
                prefs["version"],
                workflow["id"],
            ),
        )
        connection.execute(
            "UPDATE processing_steps SET state = CASE WHEN source_snapshot_id IS NOT NULL THEN 'waiting' ELSE state END, "
            "waiting_reason = CASE WHEN source_snapshot_id IS NOT NULL THEN NULL ELSE waiting_reason END, "
            "notes_provider = CASE WHEN kind = 'notes' THEN ? ELSE notes_provider END, "
            "notes_model = CASE WHEN kind = 'notes' THEN ? ELSE notes_model END, "
            "notes_reasoning_effort = CASE WHEN kind = 'notes' THEN ? ELSE notes_reasoning_effort END, "
            "notes_output_language = CASE WHEN kind = 'notes' THEN ? ELSE notes_output_language END, "
            "notes_connection_version = CASE WHEN kind = 'notes' THEN ? ELSE notes_connection_version END, "
            "recipe_version = CASE WHEN kind = 'notes' THEN recipe_version + 1 ELSE recipe_version END, "
            "updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ?",
            (
                prefs["notes_provider"],
                prefs["notes_model"],
                prefs["notes_reasoning_effort"],
                prefs["notes_output_language"],
                confirmed_pin.get("connection_version") if confirmed_pin else None,
                workflow["id"],
            ),
        )
        notes_step = connection.execute(
            "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'",
            (workflow["id"],),
        ).fetchone()
        notes_attempt = (
            connection.execute(
                "SELECT state FROM processing_attempts WHERE id = ?",
                (notes_step["current_attempt_id"],),
            ).fetchone()
            if notes_step is not None and notes_step["current_attempt_id"]
            else None
        )
        if (
            notes_step is not None
            and notes_step["source_snapshot_id"] is not None
            and (notes_attempt is None or notes_attempt["state"] != "waiting")
        ):
            _insert_attempt(
                connection,
                step=notes_step,
                workflow_epoch=new_epoch,
                attempt_number=notes_step["attempt_number"] + 1,
                request_id=_automatic_request_id(
                    workflow["id"], "notes", notes_step["attempt_number"] + 1
                ),
                request_hash=_hash(
                    workflow["id"],
                    "notes",
                    new_epoch,
                    notes_step["source_snapshot_id"],
                    prefs["notes_provider"],
                    prefs["notes_model"],
                    prefs["notes_reasoning_effort"],
                    prefs["notes_output_language"],
                ),
            )
    return processing_state(data_dir, meeting_id) or {}


def use_current_source(
    data_dir: Path,
    meeting_id: str,
    *,
    expected_epoch: object,
    expected_generation_id: object,
    expected_source_version: object,
    expected_metadata_version: object,
    expected_notes_source_version: object,
) -> dict[str, object]:
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 1
    ):
        raise ProcessingError(
            400, "invalid_processing_epoch", "Provide the current processing epoch."
        )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        workflow = _workflow_row(connection, meeting_id)
        if workflow is None:
            raise ProcessingError(404, "processing_not_found", "Processing workflow not found.")
        _assert_actionable_workflow(connection, workflow)
        if not _same_epoch(workflow["epoch"], expected_epoch):
            raise ProcessingError(
                409, "processing_epoch_changed", "Processing changed; reload and retry."
            )
        if (
            isinstance(expected_generation_id, bool)
            or expected_generation_id is not None
            and (not isinstance(expected_generation_id, str) or not expected_generation_id)
        ):
            raise ProcessingError(
                400,
                "invalid_generation_id",
                "Provide the current generation shown to the operator, or null before the first transcript.",
            )
        if (expected_generation_id is None) != (expected_source_version is None):
            raise ProcessingError(
                400,
                "invalid_source_revision",
                "Generation and source revision must both be present or both be null.",
            )
        revision_values = (expected_metadata_version, expected_notes_source_version)
        if expected_source_version is not None:
            revision_values += (expected_source_version,)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in revision_values
        ):
            raise ProcessingError(
                400,
                "invalid_source_revision",
                "Provide the source revisions shown to the operator.",
            )
        meeting = connection.execute(
            "SELECT current_generation_id, metadata_version, notes_source_version FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        generation = (
            connection.execute(
                "SELECT id, source_version FROM transcript_generations WHERE id = ? AND meeting_id = ?",
                (expected_generation_id, meeting_id),
            ).fetchone()
            if expected_generation_id is not None
            else None
        )
        if (
            meeting is None
            or meeting["current_generation_id"] != expected_generation_id
            or meeting["metadata_version"] != expected_metadata_version
            or meeting["notes_source_version"] != expected_notes_source_version
            or expected_generation_id is not None
            and (generation is None or generation["source_version"] != expected_source_version)
        ):
            raise ProcessingError(
                409,
                "source_changed",
                "The current source changed; reload and confirm it before adopting it.",
            )
        new_epoch = expected_epoch + 1
        if expected_generation_id is None:
            transcription = connection.execute(
                "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'transcription'",
                (workflow["id"],),
            ).fetchone()
            notes = connection.execute(
                "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'",
                (workflow["id"],),
            ).fetchone()
            if transcription is None or notes is None:
                raise ProcessingError(
                    404, "processing_not_found", "Processing workflow steps not found."
                )
            connection.execute(
                "UPDATE processing_workflows SET epoch = ?, state = 'waiting', last_error_code = NULL, last_error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_epoch, workflow["id"]),
            )
            connection.execute(
                "UPDATE processing_attempts SET state = 'canceled', updated_at = CURRENT_TIMESTAMP "
                "WHERE id IN (?, ?) AND state IN ('waiting', 'accepted')",
                (transcription["current_attempt_id"], notes["current_attempt_id"]),
            )
            connection.execute(
                "UPDATE processing_steps SET state = 'waiting', waiting_reason = NULL, task_id = NULL, "
                "source_snapshot_id = NULL, source_generation_id = NULL, source_version = NULL, "
                "notes_source_version = ?, metadata_version = ?, asr_choice = ?, asr_language = ?, "
                "asr_model = NULL, asr_model_path = NULL, device = NULL, compute_type = NULL, "
                "diarization_model_path = NULL, last_error_code = NULL, last_error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    expected_notes_source_version,
                    expected_metadata_version,
                    workflow["asr_choice"],
                    workflow["asr_language"],
                    transcription["id"],
                ),
            )
            connection.execute(
                "UPDATE processing_steps SET state = 'waiting', waiting_reason = 'transcript_required', task_id = NULL, "
                "source_snapshot_id = NULL, source_generation_id = NULL, source_version = NULL, "
                "notes_source_version = ?, metadata_version = ?, last_error_code = NULL, last_error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (expected_notes_source_version, expected_metadata_version, notes["id"]),
            )
            transcription = connection.execute(
                "SELECT * FROM processing_steps WHERE id = ?", (transcription["id"],)
            ).fetchone()
            notes = connection.execute(
                "SELECT * FROM processing_steps WHERE id = ?", (notes["id"],)
            ).fetchone()
            transcription_attempt_id = _insert_attempt(
                connection,
                step=transcription,
                workflow_epoch=new_epoch,
                attempt_number=transcription["attempt_number"] + 1,
                request_id=_automatic_request_id(
                    workflow["id"], "transcription", transcription["attempt_number"] + 1
                ),
                request_hash=_hash(meeting_id, "source", "transcription", new_epoch),
            )
            notes_attempt_id = _insert_attempt(
                connection,
                step=notes,
                workflow_epoch=new_epoch,
                attempt_number=notes["attempt_number"] + 1,
                request_id=_automatic_request_id(
                    workflow["id"], "notes", notes["attempt_number"] + 1
                ),
                request_hash=_hash(workflow["id"], "notes", new_epoch, None),
            )
            connection.execute(
                "UPDATE processing_steps SET current_attempt_id = CASE id WHEN ? THEN ? WHEN ? THEN ? END, updated_at = CURRENT_TIMESTAMP WHERE id IN (?, ?)",
                (
                    transcription["id"],
                    transcription_attempt_id,
                    notes["id"],
                    notes_attempt_id,
                    transcription["id"],
                    notes["id"],
                ),
            )
        else:
            snapshot_id = _freeze_notes_source(connection, meeting_id, expected_generation_id)
            step = connection.execute(
                "SELECT * FROM processing_steps WHERE workflow_id = ? AND kind = 'notes'",
                (workflow["id"],),
            ).fetchone()
            connection.execute(
                "UPDATE processing_workflows SET epoch = ?, state = 'waiting', last_error_code = NULL, last_error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_epoch, workflow["id"]),
            )
            connection.execute(
                "UPDATE processing_attempts SET state = 'canceled', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT current_attempt_id FROM processing_steps WHERE workflow_id = ? AND kind = 'transcription') "
                "AND state IN ('waiting', 'accepted')",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE processing_steps SET state = 'succeeded', waiting_reason = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE workflow_id = ? AND kind = 'transcription' "
                "AND state IN ('waiting', 'needs_attention', 'failed')",
                (workflow["id"],),
            )
            connection.execute(
                "UPDATE processing_steps SET state = 'waiting', waiting_reason = NULL, task_id = NULL, source_snapshot_id = ?, source_generation_id = ?, source_version = ?, notes_source_version = ?, metadata_version = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    snapshot_id,
                    expected_generation_id,
                    expected_source_version,
                    expected_notes_source_version,
                    expected_metadata_version,
                    step["id"],
                ),
            )
            step = connection.execute(
                "SELECT * FROM processing_steps WHERE id = ?", (step["id"],)
            ).fetchone()
            attempt_id = _insert_attempt(
                connection,
                step=step,
                workflow_epoch=new_epoch,
                attempt_number=step["attempt_number"] + 1,
                request_id=_automatic_request_id(
                    workflow["id"], "notes", step["attempt_number"] + 1
                ),
                request_hash=_hash(meeting_id, "source", snapshot_id, new_epoch),
            )
            connection.execute(
                "UPDATE processing_steps SET current_attempt_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (attempt_id, step["id"]),
            )
    return processing_state(data_dir, meeting_id) or {}


class AutomaticProcessingScheduler:
    """A bounded FIFO admission loop with a real process-lifetime lock.

    It applies the finite GUI grace budget across reconciliation passes and both
    automatic task kinds. It never takes over a task from a heartbeat timestamp.
    """

    def __init__(
        self,
        settings: Settings,
        chatgpt_provider: Any,
        antigravity_provider: Any,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.chatgpt_provider = chatgpt_provider
        self.antigravity_provider = antigravity_provider
        self.clock = clock
        self.sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._descriptor: int | None = None
        self._automatic_admissions_since_gui_grace = 0
        self._gui_grace_pending = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="automatic-processing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        try:
            write_service_heartbeat(
                self.settings.data_dir, "processing", "starting", role="processing"
            )
            while not self._stop.is_set():
                if self._descriptor is None:
                    self._descriptor = acquire_service_lock(
                        self.settings.data_dir, "processing", retry_seconds=0.1
                    )
                    if self._descriptor is None:
                        self.sleep(0.25)
                        continue
                write_service_heartbeat(
                    self.settings.data_dir, "processing", "idle", role="processing"
                )
                try:
                    self.reconcile_once()
                except Exception:
                    # State transitions are retried on the next pass; a malformed
                    # provider/model is represented by a durable waiting reason.
                    self.sleep(0.25)
                    continue
                self.sleep(0.25)
        finally:
            with suppress(OSError):
                if self._descriptor is not None:
                    import os

                    os.close(self._descriptor)
            self._descriptor = None
            with suppress(OSError):
                write_service_heartbeat(
                    self.settings.data_dir, "processing", "stopped", role="processing"
                )

    def _set_reason(self, step_id: str, reason: str, message: str | None = None) -> None:
        with _connect(self.settings.data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _mark_waiting(connection, step_id, reason, message=message)

    def _record_automatic_admission(self) -> bool:
        self._automatic_admissions_since_gui_grace += 1
        if self._automatic_admissions_since_gui_grace < AUTOMATIC_ADMISSIONS_BEFORE_GUI_GRACE:
            return False
        self._automatic_admissions_since_gui_grace = 0
        self._gui_grace_pending = True
        return True

    def _slot_is_free(self) -> bool:
        with _connect(self.settings.data_dir) as connection:
            slot = connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone()
        return slot is not None and slot["active_task_id"] is None

    def _run_pending_gui_grace(self) -> bool:
        if not self._gui_grace_pending:
            return False
        if not self._slot_is_free():
            return True
        self._gui_grace_pending = False
        self.sleep(GUI_GRACE_SECONDS)
        return True

    def reconcile_once(self) -> int:
        reconcile_ready_sources(self.settings.data_dir)
        self._requeue_failed_steps()
        if self._run_pending_gui_grace():
            return 0
        admitted = 0
        grace_pending = False
        with _connect(self.settings.data_dir) as connection:
            rows = connection.execute(
                "SELECT workflows.*, steps.id AS step_id, steps.state AS step_state, steps.waiting_reason, steps.current_attempt_id, steps.attempt_number, steps.source_snapshot_id, "
                "(SELECT request_id FROM processing_attempts WHERE id = steps.current_attempt_id) AS attempt_request_id "
                "FROM processing_workflows AS workflows JOIN processing_steps AS steps ON steps.workflow_id = workflows.id "
                "WHERE workflows.state IN ('waiting', 'needs_attention') AND steps.kind = 'transcription' AND steps.state = 'waiting' AND steps.next_eligible_at <= ? "
                "ORDER BY workflows.ready_sequence, workflows.id LIMIT ?",
                (self.clock(), MAX_WORKFLOWS),
            ).fetchall()
        for row in rows:
            if admitted >= MAX_STEPS:
                break
            if row["waiting_reason"] in ATTENTION_REASONS:
                continue
            try:
                from asr_studio.runtime import read_runtime_profile
                from asr_studio.worker import is_healthy as worker_is_healthy

                worker_ready = worker_is_healthy(
                    self.settings.data_dir, role="transcription"
                ) or worker_is_healthy(self.settings.data_dir, role="legacy")
                profile = read_runtime_profile(self.settings.data_dir) if worker_ready else None
                if not worker_ready or profile is None:
                    self._set_reason(row["step_id"], "worker_offline")
                    continue
                run = resolve_run(self.settings, profile, row["asr_choice"])
                engine = engine_of(run["asr_model"], Path(run["model_path"]))
                prepared_engine_directory(engine, Path(run["model_path"]))
                prepared_diarization_directory(self.settings.diarization_model_path)
            except (
                ModelConfigurationError,
                DiarizationConfigurationError,
                OSError,
                ValueError,
            ) as error:
                self._set_reason(row["step_id"], "model_unavailable", str(error))
                continue
            with _connect(self.settings.data_dir) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                result = admit_automatic_transcription(
                    connection,
                    workflow_id=row["id"],
                    epoch=row["epoch"],
                    request_id=row["attempt_request_id"]
                    or _automatic_request_id(row["id"], "transcription", row["attempt_number"] + 1),
                    request_hash=_hash(row["id"], "transcription", row["epoch"], run["asr_model"]),
                    model_path=Path(run["model_path"]),
                    asr_model=str(run["asr_model"]),
                    language_hint=str(row["asr_language"]),
                    device=str(run["device"]),
                    compute_type=str(run["compute_type"]),
                    diarization_model_path=self.settings.diarization_model_path,
                    duration_seconds=float(
                        connection.execute(
                            "SELECT duration_seconds FROM recordings WHERE id = ?",
                            (row["recording_id"],),
                        ).fetchone()[0]
                    ),
                )
            if result is not None:
                admitted += 1
                if self._record_automatic_admission():
                    grace_pending = True
                    break
        if grace_pending:
            return admitted
        admitted += self._admit_notes()
        return admitted

    def _admit_notes(self) -> int:
        admitted = 0
        with _connect(self.settings.data_dir) as connection:
            rows = connection.execute(
                "SELECT workflows.*, steps.id AS step_id, steps.current_attempt_id, steps.attempt_number, steps.source_snapshot_id, "
                "(SELECT request_id FROM processing_attempts WHERE id = steps.current_attempt_id) AS attempt_request_id "
                "FROM processing_workflows AS workflows JOIN processing_steps AS steps ON steps.workflow_id = workflows.id "
                "WHERE workflows.state = 'waiting' AND steps.kind = 'notes' AND steps.state = 'waiting' AND steps.source_snapshot_id IS NOT NULL AND steps.next_eligible_at <= ? "
                "ORDER BY workflows.ready_sequence, workflows.id LIMIT ?",
                (self.clock(), MAX_WORKFLOWS),
            ).fetchall()
        for row in rows:
            if row["notes_provider"] is None or row["notes_model"] is None:
                with _connect(self.settings.data_dir) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    _mark_waiting(connection, row["step_id"], "provider_unconfigured")
                continue
            if not notes_service.is_ready(self.settings.data_dir):
                self._set_reason(row["step_id"], "worker_offline")
                continue
            provider = (
                self.chatgpt_provider
                if row["notes_provider"] == "chatgpt"
                else self.antigravity_provider
            )
            try:
                provider.assert_notes_acceptable()
                with provider.store.operation():
                    pin = provider.notes_task_pin()
                    if row["notes_model"] is not None and pin["model"] != row["notes_model"]:
                        raise ProcessingError(
                            409, "recipe_changed", "The pinned notes model is no longer selected."
                        )
                    with _connect(self.settings.data_dir) as connection, connection:
                        connection.execute("BEGIN IMMEDIATE")
                        result = admit_automatic_notes(
                            connection,
                            workflow_id=row["id"],
                            epoch=row["epoch"],
                            request_id=row["attempt_request_id"]
                            or _automatic_request_id(row["id"], "notes", row["attempt_number"] + 1),
                            request_hash=_hash(row["id"], "notes", row["epoch"], pin),
                            pin=pin,
                        )
                if result is not None:
                    admitted += 1
                    if self._record_automatic_admission():
                        break
            except ProcessingError as error:
                self._set_reason(row["step_id"], error.code, error.message)
            except Exception as error:
                self._set_reason(row["step_id"], "provider_unavailable", str(error))
        return admitted

    def _requeue_failed_steps(self) -> None:
        with _connect(self.settings.data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            failed = connection.execute(
                "SELECT steps.*, workflows.epoch, workflows.state AS workflow_state "
                "FROM processing_steps AS steps JOIN processing_workflows AS workflows "
                "ON workflows.id = steps.workflow_id WHERE steps.state = 'failed' "
                "AND workflows.state = 'waiting' ORDER BY workflows.ready_sequence LIMIT ?",
                (MAX_STEPS,),
            ).fetchall()
            for step in failed:
                if step["attempt_number"] >= MAX_RETRIES:
                    continue
                attempt_number = step["attempt_number"] + 1
                try:
                    connection.execute("SAVEPOINT requeue_failed_step")
                    _insert_attempt(
                        connection,
                        step=step,
                        workflow_epoch=step["epoch"],
                        attempt_number=attempt_number,
                        request_id=_automatic_request_id(
                            step["workflow_id"], step["kind"], attempt_number
                        ),
                        request_hash=_hash(
                            step["workflow_id"], step["kind"], step["epoch"], attempt_number
                        ),
                    )
                    connection.execute(
                        "UPDATE processing_steps SET state = 'waiting', waiting_reason = ?, "
                        "next_eligible_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (
                            step["last_error_code"] or "retry",
                            self.clock() + RETRY_BASE_SECONDS * attempt_number,
                            step["id"],
                        ),
                    )
                    connection.execute("RELEASE SAVEPOINT requeue_failed_step")
                except sqlite3.IntegrityError:
                    # A corrupt legacy row must not abort admission for other workflows.
                    with suppress(sqlite3.Error):
                        connection.execute("ROLLBACK TO SAVEPOINT requeue_failed_step")
                        connection.execute("RELEASE SAVEPOINT requeue_failed_step")
