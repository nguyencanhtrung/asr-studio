# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from notes_helpers import StubNotesProvider

from asr_studio import processing as processing_module
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.extension_ingest import publish_ready_derivative
from asr_studio.meeting_deletion import _mark_meeting_for_deletion
from asr_studio.notes_document import PROMPT_VERSION, SCHEMA_VERSION
from asr_studio.processing import (
    AutomaticProcessingScheduler,
    ProcessingError,
    _connect,
    _hash,
    admit_automatic_notes,
    admit_automatic_transcription,
    apply_current_settings,
    cancel_workflow,
    processing_state,
    ready_handoff_tx,
    retry_notes,
    save_processing_preferences,
    use_current_source,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import claim_next_task, publish_complete_generation


@pytest.mark.anyio
async def test_processing_preferences_route_validates_before_provider_mutation(client, settings):
    token = AuthStore(settings.auth_dir).load().token
    login = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": token},
    )
    assert login.status_code == 204
    before = await client.get("/api/processing-preferences")
    assert before.status_code == 200
    valid = await client.put(
        "/api/processing-preferences",
        headers={"Origin": settings.public_url},
        json={
            "asr_choice": "auto",
            "asr_language": "vi",
            "notes_provider": None,
            "notes_model": None,
            "notes_reasoning_effort": None,
            "notes_output_language": "en",
        },
    )
    assert valid.status_code == 200
    assert valid.json()["version"] == before.json()["version"] + 1
    invalid = await client.put(
        "/api/processing-preferences",
        headers={"Origin": settings.public_url},
        json={
            "asr_choice": "not-a-model",
            "asr_language": "vi",
            "notes_provider": None,
            "notes_model": None,
            "notes_reasoning_effort": None,
            "notes_output_language": "en",
        },
    )
    assert invalid.status_code == 400
    after = await client.get("/api/processing-preferences")
    assert after.json()["version"] == valid.json()["version"]


def _ready_source(tmp_path: Path, label: str = "0") -> tuple[Path, str, str]:
    data_dir = tmp_path / "data"
    initialize(data_dir)
    meeting_id = f"meeting-processing-{label}"
    recording_id = f"recording-processing-{label}"
    session_id = ("s" * 31) + label[-1]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES (?, ?, 'vi')",
            (meeting_id, "Processing fixture"),
        )
        connection.execute(
            "INSERT INTO extension_recording_sessions(id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, language_hint, language_hint_omitted, mime_type, state, epoch, next_sequence, next_offset) "
            "VALUES (?, ?, ?, ?, ?, 'vi', 1, 'audio/webm;codecs=opus', 'archived', 1, 0, 0)",
            (session_id, "o" * 16 + label, f"client-{label}", f"capture-{label}", "f" * 64),
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, extension_session_id, storage_key, content_type, duration_seconds) VALUES (?, ?, ?, NULL, NULL, NULL)",
            (recording_id, meeting_id, session_id),
        )
        connection.commit()
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"recordings/{session_id}.m4a",
            duration_seconds=12.0,
        )
        connection.commit()
    return data_dir, meeting_id, session_id


class _ProcessingNotesProvider(StubNotesProvider):
    def __init__(self, *, effort: str = "high") -> None:
        super().__init__()
        self.effort = effort
        self.pin_calls = 0

    def notes_task_pin(self) -> dict[str, object]:
        self.pin_calls += 1
        return {
            "provider": "chatgpt",
            "connection_version": 7,
            "model": "stub-model",
            "reasoning_effort": self.effort,
        }


def _notes_pin(effort: str = "high") -> dict[str, object]:
    return {
        "provider": "chatgpt",
        "connection_version": 7,
        "model": "stub-model",
        "reasoning_effort": effort,
    }


def _ready_notes_workflow(
    tmp_path: Path, *, effort: str = "high"
) -> tuple[Path, str, dict[str, object]]:
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=f"auto:{state['id']}:transcription:1",
            request_hash=_hash(state["id"], "transcription", 1, "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    claimed = claim_next_task(data_dir, owner_id="processing-fixture-owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    assert (
        publish_complete_generation(
            data_dir,
            claimed["id"],
            owner_id="processing-fixture-owner",
            segments=[{"id": "b" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Ship it."}],
            language_probability=None,
            elapsed_seconds=0.1,
        )
        is not None
    )
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort=effort,
        notes_output_language="en",
    )
    resumed = apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=1,
        expected_preferences_version=preferences["version"],
        confirmed_pin=_notes_pin(effort),
    )
    return data_dir, meeting_id, resumed


def _admit_notes_for_state(
    data_dir: Path, meeting_id: str, pin: dict[str, object]
) -> dict[str, object] | None:
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    notes_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == notes_step["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return admit_automatic_notes(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=notes_attempt["request_id"],
            request_hash=_hash(state["id"], "notes", state["epoch"], pin),
            pin=pin,
        )


def test_ready_handoff_creates_exactly_one_waiting_workflow_without_claiming_slot(tmp_path):
    data_dir, meeting_id, session_id = _ready_source(tmp_path)

    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["state"] == "waiting"
    assert state["epoch"] == 1
    assert len(state["steps"]) == 2
    assert state["steps"][0]["state"] in {"waiting", "needs_attention"}
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT COUNT(*) FROM processing_workflows WHERE extension_session_id = ?",
            (session_id,),
        ).fetchone() == (1,)
        connection.execute("BEGIN IMMEDIATE")
        connection.row_factory = sqlite3.Row
        assert ready_handoff_tx(connection, session_id) is not None
        connection.rollback()


def test_two_automatic_admissions_serialize_on_the_shared_slot(tmp_path):
    first_dir, first_meeting, _ = _ready_source(tmp_path, "1")
    second_dir, second_meeting, _ = _ready_source(tmp_path, "2")

    def admit(data_dir: Path, meeting_id: str) -> dict[str, object] | None:
        state = processing_state(data_dir, meeting_id)
        assert state is not None
        with _connect(data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return admit_automatic_transcription(
                connection,
                workflow_id=state["id"],
                epoch=1,
                request_id=f"auto:{state['id']}:transcription:1",
                request_hash=_hash(state["id"], "transcription", 1, "whisper-small"),
                model_path=Path("/models/whisper-small"),
                asr_model="whisper-small",
                language_hint="vi",
                device="cpu",
                compute_type="int8",
                diarization_model_path=Path("/models/speaker-diarization-community-1"),
                duration_seconds=12.0,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda values: admit(*values),
                ((first_dir, first_meeting), (second_dir, second_meeting)),
            )
        )
    assert sum(result is not None for result in results) == 1
    with sqlite3.connect(database_path(first_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)


def test_ready_handoff_rolls_back_workflow_with_ready_publication(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    meeting_id = "m-rollback"
    session_id = "r" * 32
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES (?, 'Rollback', 'vi')",
            (meeting_id,),
        )
        connection.execute(
            "INSERT INTO extension_recording_sessions(id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, language_hint, language_hint_omitted, mime_type, state, epoch, next_sequence, next_offset) VALUES (?, ?, 'c', 'e', ?, 'vi', 1, 'audio/webm;codecs=opus', 'archived', 1, 0, 0)",
            (session_id, "o" * 16, "a" * 64),
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, extension_session_id, storage_key, content_type, duration_seconds) VALUES ('rr', ?, ?, NULL, NULL, NULL)",
            (meeting_id, session_id),
        )
        connection.commit()
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection, session_id, derivative_key="x.m4a", duration_seconds=1.0
        )
        connection.rollback()
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT state, storage_key FROM extension_recording_sessions JOIN recordings ON recordings.extension_session_id = extension_recording_sessions.id WHERE extension_recording_sessions.id = ?",
            (session_id,),
        ).fetchone() == ("archived", None)
        assert connection.execute("SELECT COUNT(*) FROM processing_workflows").fetchone() == (0,)


def test_automatic_transcription_replay_claim_and_atomic_late_cancel_fence(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    workflow_id = state["id"]
    request_id = f"auto:{workflow_id}:transcription:1"
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=workflow_id,
            epoch=1,
            request_id=request_id,
            request_hash=_hash(workflow_id, "transcription", 1, "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
        assert admitted is not None and admitted["replay"] is False
        replay = admit_automatic_transcription(
            connection,
            workflow_id=workflow_id,
            epoch=1,
            request_id=request_id,
            request_hash=_hash(workflow_id, "transcription", 1, "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
        assert replay == {"task_id": admitted["task_id"], "replay": True}
    with pytest.raises(ProcessingError) as active_error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=1,
            expected_preferences_version=1,
        )
    assert active_error.value.code == "processing_active"
    claimed = claim_next_task(data_dir, owner_id="transcription-owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    canceled = cancel_workflow(
        data_dir, meeting_id, request_id="cancel-processing-1", expected_epoch=1
    )
    assert canceled["state"] == "canceled"
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET status = 'running', stage = 'publishing' WHERE id = ?",
            (claimed["id"],),
        )
        connection.commit()
    assert (
        publish_complete_generation(
            data_dir,
            claimed["id"],
            owner_id="transcription-owner",
            segments=[{"id": "a" * 32, "start_seconds": 0, "end_seconds": 1, "text": "hello"}],
            language_probability=None,
            elapsed_seconds=0.1,
        )
        is None
    )
    with pytest.raises(ProcessingError) as error, _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admit_automatic_transcription(
            connection,
            workflow_id=workflow_id,
            epoch=1,
            request_id="late-admission-1",
            request_hash="b" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert error.value.code == "processing_epoch_changed"


def test_successful_transcript_freezes_source_and_notes_retry_reuses_it(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    workflow_id = state["id"]
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        result = admit_automatic_transcription(
            connection,
            workflow_id=workflow_id,
            epoch=1,
            request_id=f"auto:{workflow_id}:transcription:1",
            request_hash=_hash(workflow_id, "transcription", 1, "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert result is not None
    claimed = claim_next_task(data_dir, owner_id="owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    generation = publish_complete_generation(
        data_dir,
        claimed["id"],
        owner_id="owner",
        segments=[{"id": "b" * 32, "start_seconds": 0, "end_seconds": 1, "text": "frozen"}],
        language_probability=None,
        elapsed_seconds=0.1,
    )
    assert generation is not None
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    assert notes_step["source_snapshot_id"] is not None
    assert notes_step["waiting_reason"] == "provider_unconfigured"
    with pytest.raises(ProcessingError) as source_error:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=1,
            expected_generation_id=state["current_source"]["generation_id"],
            expected_source_version=state["current_source"]["source_version"] + 1,
            expected_metadata_version=state["current_source"]["metadata_version"],
            expected_notes_source_version=state["current_source"]["notes_source_version"],
        )
    assert source_error.value.code == "source_changed"
    attempts_before = len(state["attempts"])
    retried = retry_notes(
        data_dir,
        meeting_id,
        request_id="notes-retry-0001",
        expected_epoch=1,
        source_snapshot_id=notes_step["source_snapshot_id"],
        confirmed_recipe_version=1,
    )
    assert retried["epoch"] == 2
    assert len(retried["attempts"]) == attempts_before + 1
    assert sum(step["kind"] == "transcription" for step in retried["steps"]) == 1
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort="default",
        notes_output_language="en",
    )
    with pytest.raises(ProcessingError) as preferences_error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=2,
            expected_preferences_version=preferences["version"] - 1,
        )
    assert preferences_error.value.code == "preferences_changed"
    resumed = apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=2,
        expected_preferences_version=preferences["version"],
        confirmed_pin={
            "provider": "chatgpt",
            "model": "stub-model",
            "reasoning_effort": "default",
            "connection_version": 7,
        },
    )
    resumed_notes = next(step for step in resumed["steps"] if step["kind"] == "notes")
    assert resumed_notes["waiting_reason"] is None
    assert len(resumed["attempts"]) == len(retried["attempts"])


def test_automatic_notes_uses_production_contract_with_real_notes_runner(
    tmp_path, monkeypatch, capsys
):
    from asr_studio import notes_runner

    data_dir, meeting_id, _ = _ready_notes_workflow(tmp_path, effort="high")
    admitted = _admit_notes_for_state(data_dir, meeting_id, _notes_pin("high"))
    assert admitted is not None
    task = claim_next_task(data_dir, owner_id="notes-contract-owner", role="notes")
    assert task is not None
    assert task["notes_prompt_version"] == PROMPT_VERSION
    assert task["notes_schema_version"] == SCHEMA_VERSION
    monkeypatch.setenv("ASR_PROVIDER_DIR", str(tmp_path / "provider"))
    provider_calls: list[dict[str, object]] = []

    class ExternalProviderDouble:
        def __init__(self, _directory, *, callback_url):
            assert callback_url == ""

        def generate_notes_document(self, *, snapshot, model, reasoning_effort, **_kwargs):
            provider_calls.append(
                {
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "segment_count": len(snapshot["segments"]),
                }
            )
            source_ids = [segment["id"] for segment in snapshot["segments"]]
            return {
                "schema_version": SCHEMA_VERSION,
                "overview": [],
                "key_topics": [],
                "decisions": [{"text": "Ship it.", "source_ids": source_ids}],
                "open_questions": [],
                "action_items": [],
                "confirmation_items": [],
            }

    monkeypatch.setattr(notes_runner, "ChatGPTProvider", ExternalProviderDouble)

    assert notes_runner.run_notes_task(data_dir, task["id"], "notes-contract-owner") == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert provider_calls == [
        {"model": "stub-model", "reasoning_effort": "high", "segment_count": 1}
    ]
    assert events[-1]["type"] == "result"
    assert "- Ship it." in events[-1]["markdown"]
    assert events[-1]["citations"][0]["sources"][0]["id"] == "b" * 32


def test_automatic_notes_rejects_changed_effort_until_settings_are_adopted(tmp_path):
    data_dir, meeting_id, before = _ready_notes_workflow(tmp_path, effort="high")
    rejected = _admit_notes_for_state(data_dir, meeting_id, _notes_pin("low"))
    assert rejected is None
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    assert state["pinned"]["notes_reasoning_effort"] == "high"
    assert notes_step["waiting_reason"] == "recipe_changed"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'notes'").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)

    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort="low",
        notes_output_language="en",
    )
    adopted = apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=before["epoch"],
        expected_preferences_version=preferences["version"],
        confirmed_pin=_notes_pin("low"),
    )
    assert adopted["pinned"]["notes_reasoning_effort"] == "low"
    accepted = _admit_notes_for_state(data_dir, meeting_id, _notes_pin("low"))
    assert accepted is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT notes_reasoning_effort FROM tasks WHERE id = ?", (accepted["task_id"],)
        ).fetchone() == ("low",)


def test_notes_scheduler_waits_for_worker_then_admits_once_on_recovery(tmp_path, monkeypatch):
    data_dir, meeting_id, _ = _ready_notes_workflow(tmp_path, effort="high")
    provider = _ProcessingNotesProvider(effort="high")
    notes_ready = False
    monkeypatch.setattr(
        processing_module.notes_service,
        "is_ready",
        lambda _data_dir: notes_ready,
    )
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        provider,
        provider,
        sleep=lambda _seconds: None,
    )

    assert scheduler.reconcile_once() == 0
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    assert notes_step["waiting_reason"] == "worker_offline"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'notes'").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)

    notes_ready = True
    assert scheduler.reconcile_once() == 1
    assert scheduler.reconcile_once() == 0
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'notes'").fetchone() == (
            1,
        )
        assert (
            connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone()[0]
            is not None
        )
    assert provider.pin_calls == 1


def test_scheduler_gui_grace_spans_sequential_asr_and_notes_passes(tmp_path, monkeypatch):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    monkeypatch.setattr("asr_studio.worker.is_healthy", lambda _data_dir, role=None: True)
    monkeypatch.setattr("asr_studio.runtime.read_runtime_profile", lambda _data_dir: object())
    monkeypatch.setattr(
        processing_module,
        "resolve_run",
        lambda *_args: {
            "asr_model": "whisper-small",
            "model_path": "/models/whisper-small",
            "device": "cpu",
            "compute_type": "int8",
        },
    )
    monkeypatch.setattr(processing_module, "engine_of", lambda *_args: "whisper")
    monkeypatch.setattr(processing_module, "prepared_engine_directory", lambda *_args: None)
    monkeypatch.setattr(processing_module, "prepared_diarization_directory", lambda *_args: None)
    grace_sleeps: list[float] = []
    provider = _ProcessingNotesProvider(effort="high")
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        provider,
        provider,
        sleep=grace_sleeps.append,
    )

    assert scheduler.reconcile_once() == 1
    transcription = claim_next_task(
        data_dir, owner_id="grace-transcription-owner", role="transcription"
    )
    assert transcription is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET stage = 'publishing' WHERE id = ?", (transcription["id"],)
        )
    assert (
        publish_complete_generation(
            data_dir,
            transcription["id"],
            owner_id="grace-transcription-owner",
            segments=[{"id": "c" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Continue."}],
            language_probability=None,
            elapsed_seconds=0.1,
        )
        is not None
    )
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort="high",
        notes_output_language="en",
    )
    apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=1,
        expected_preferences_version=preferences["version"],
        confirmed_pin=_notes_pin("high"),
    )
    monkeypatch.setattr(processing_module.notes_service, "is_ready", lambda _data_dir: True)

    assert scheduler.reconcile_once() == 1
    assert grace_sleeps == [1.0]
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (2,)
        assert connection.execute("SELECT kind FROM tasks WHERE kind = 'notes'").fetchone() == (
            "notes",
        )


def test_scheduler_records_durable_worker_waiting_reason_and_does_not_claim_slot(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    settings = Settings(data_dir=data_dir, auth_dir=data_dir / "auth")
    scheduler = AutomaticProcessingScheduler(settings, None, None, sleep=lambda _: None)
    assert scheduler.reconcile_once() == 0
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    assert transcription["waiting_reason"] == "worker_offline"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)


def test_operator_actions_fence_active_canceled_and_deletion_pending_workflows(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription_step = next(step for step in state["steps"] if step["kind"] == "transcription")
    transcription_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription_step["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=transcription_attempt["request_id"],
            request_hash="c" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None

    action_kwargs = {
        "expected_generation_id": "",
        "expected_source_version": 1,
        "expected_metadata_version": 1,
        "expected_notes_source_version": 1,
    }
    with pytest.raises(ProcessingError) as error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=1,
            expected_preferences_version=1,
        )
    assert error.value.code == "processing_active"
    with pytest.raises(ProcessingError) as error:
        use_current_source(data_dir, meeting_id, expected_epoch=1, **action_kwargs)
    assert error.value.code == "processing_active"

    canceled = cancel_workflow(
        data_dir, meeting_id, request_id="cancel:operator-guards", expected_epoch=1
    )
    assert canceled["state"] == "canceled"
    for action in (
        lambda: apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=2,
            expected_preferences_version=1,
        ),
        lambda: use_current_source(data_dir, meeting_id, expected_epoch=2, **action_kwargs),
    ):
        with pytest.raises(ProcessingError) as error:
            action()
        assert error.value.code == "processing_canceled"

    pending_dir, pending_meeting, _ = _ready_source(tmp_path, "pending-delete")
    with sqlite3.connect(database_path(pending_dir)) as connection:
        connection.execute(
            "UPDATE meetings SET deletion_requested_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pending_meeting,),
        )
        connection.commit()
    for action in (
        lambda: apply_current_settings(
            pending_dir,
            pending_meeting,
            expected_epoch=1,
            expected_preferences_version=1,
        ),
        lambda: use_current_source(pending_dir, pending_meeting, expected_epoch=1, **action_kwargs),
    ):
        with pytest.raises(ProcessingError) as error:
            action()
        assert error.value.code == "meeting_deletion_pending"

    deleted_dir, deleted_meeting, _ = _ready_source(tmp_path, "deleted")
    _mark_meeting_for_deletion(deleted_dir, deleted_meeting)
    deleted_state = processing_state(deleted_dir, deleted_meeting)
    assert deleted_state is not None and deleted_state["state"] == "deleted"
    with pytest.raises(ProcessingError) as error, _connect(deleted_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        deleted_transcription = next(
            step for step in deleted_state["steps"] if step["kind"] == "transcription"
        )
        deleted_attempt = next(
            attempt
            for attempt in deleted_state["attempts"]
            if attempt["step_id"] == deleted_transcription["id"]
        )
        admit_automatic_transcription(
            connection,
            workflow_id=deleted_state["id"],
            epoch=deleted_state["epoch"],
            request_id=deleted_attempt["request_id"],
            request_hash="d" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert error.value.code == "processing_canceled"


@pytest.mark.parametrize("provider", ["antigravity", "chatgpt"])
def test_apply_settings_uses_provider_effort_resolution(tmp_path, provider):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider=provider,
        notes_model="stub-model",
        notes_reasoning_effort="default" if provider == "antigravity" else "low",
        notes_output_language="en",
    )
    state = processing_state(data_dir, meeting_id)
    pin = {
        "provider": provider,
        "model": "stub-model",
        "reasoning_effort": "high",
        "connection_version": 4,
    }
    if provider == "chatgpt":
        with pytest.raises(ProcessingError) as rejected:
            apply_current_settings(
                data_dir,
                meeting_id,
                expected_epoch=state["epoch"],
                expected_preferences_version=preferences["version"],
                confirmed_pin=pin,
            )
        assert rejected.value.code == "provider_confirmation_required"
        assert processing_state(data_dir, meeting_id)["epoch"] == state["epoch"]
    else:
        updated = apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=state["epoch"],
            expected_preferences_version=preferences["version"],
            confirmed_pin=pin,
        )
        assert updated["epoch"] == state["epoch"] + 1
        assert updated["pinned"]["notes_reasoning_effort"] == "default"
