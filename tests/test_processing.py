# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from notes_helpers import StubNotesProvider

from asr_studio import processing as processing_module
from asr_studio.antigravity import AntigravityProvider
from asr_studio.asr_models import read_default_model, write_default_model
from asr_studio.auth import AuthStore
from asr_studio.chatgpt import ChatGPTProvider
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
    save_asr_preference,
    save_processing_preferences,
    use_current_source,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    accept_transcription_task,
    cancel_task,
    claim_next_task,
    fail_task,
    publish_complete_generation,
)


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


@pytest.mark.anyio
async def test_legacy_put_accepts_dormant_notes_mirrors_and_get_preserves_them(client, settings):
    token = AuthStore(settings.auth_dir).load().token
    origin = {"Origin": settings.public_url}
    assert (
        await client.post("/api/access/session", headers=origin, json={"token": token})
    ).status_code == 204
    legacy = await client.put(
        "/api/processing-preferences",
        headers=origin,
        json={
            "asr_choice": "auto",
            "asr_language": "vi",
            "notes_provider": None,
            "notes_model": "dormant-legacy-model",
            "notes_reasoning_effort": "high",
            "notes_output_language": "en",
        },
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["notes"] == {
        "provider": None,
        "model": "dormant-legacy-model",
        "reasoning_effort": "high",
        "output_language": "en",
    }
    assert (await client.get("/api/processing-preferences")).json() == legacy.json()

    cleared = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": legacy.json()["version"],
            "asr_language": "vi",
            "notes_provider": None,
            "notes_output_language": "en",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["notes"] == {
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "output_language": "en",
    }


@pytest.mark.anyio
async def test_processing_preferences_patch_materializes_owner_pair_without_mutating_owner_stores(
    client, settings
):
    token = AuthStore(settings.auth_dir).load().token
    origin = {"Origin": settings.public_url}
    login = await client.post("/api/access/session", headers=origin, json={"token": token})
    assert login.status_code == 204

    chatgpt = ChatGPTProvider(settings.provider_dir, callback_url="")
    antigravity = AntigravityProvider(settings.provider_dir)
    chatgpt.save_notes_preference(model="gpt-5", reasoning_effort="default")
    antigravity.save_notes_preference("gemini-3.1-pro", "default")
    legacy = await client.put(
        "/api/processing-preferences",
        headers=origin,
        json={
            "asr_choice": "whisper-small",
            "asr_language": "vi",
            "notes_provider": "chatgpt",
            "notes_model": "gpt-5",
            "notes_reasoning_effort": "default",
            "notes_output_language": "en",
        },
    )
    assert legacy.status_code == 200, legacy.text
    owner_before = (await client.get("/api/notes-preferences")).json()

    switched = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": legacy.json()["version"],
            "asr_language": "en",
            "notes_provider": "antigravity",
            "notes_output_language": "vi",
        },
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["asr"] == {"choice": "whisper-small", "language": "en"}
    assert switched.json()["notes"] == {
        "provider": "antigravity",
        "model": "gemini-3.1-pro",
        "reasoning_effort": "high",
        "output_language": "vi",
    }
    assert (await client.get("/api/notes-preferences")).json() == owner_before

    switched_back = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": switched.json()["version"],
            "asr_language": "en",
            "notes_provider": "chatgpt",
            "notes_output_language": "en",
        },
    )
    assert switched_back.status_code == 200, switched_back.text
    assert switched_back.json()["notes"] == {
        "provider": "chatgpt",
        "model": "gpt-5",
        "reasoning_effort": "default",
        "output_language": "en",
    }

    language_only = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": switched_back.json()["version"],
            "asr_language": "vi",
            "notes_provider": "antigravity",
            "notes_output_language": "en",
        },
    )
    assert language_only.status_code == 200, language_only.text
    assert language_only.json()["notes"]["model"] == "gemini-3.1-pro"
    assert language_only.json()["notes"]["reasoning_effort"] == "high"

    unset = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": language_only.json()["version"],
            "asr_language": "vi",
            "notes_provider": None,
            "notes_output_language": "en",
        },
    )
    assert unset.status_code == 200, unset.text
    assert unset.json()["notes"] == {
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "output_language": "en",
    }
    assert (await client.get("/api/notes-preferences")).json() == owner_before


@pytest.mark.anyio
@pytest.mark.parametrize("provider", [[], {}])
async def test_processing_preferences_patch_rejects_unstructured_provider_without_write(
    client, settings, provider
):
    token = AuthStore(settings.auth_dir).load().token
    origin = {"Origin": settings.public_url}
    assert (
        await client.post("/api/access/session", headers=origin, json={"token": token})
    ).status_code == 204
    before = (await client.get("/api/processing-preferences")).json()
    reply = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": before["version"],
            "asr_language": "vi",
            "notes_provider": provider,
            "notes_output_language": "en",
        },
    )
    assert reply.status_code == 400
    assert reply.json()["detail"]["code"] == "invalid_notes_provider"
    assert (await client.get("/api/processing-preferences")).json() == before


@pytest.mark.anyio
async def test_processing_preferences_patch_requires_selected_owner_without_write(client, settings):
    token = AuthStore(settings.auth_dir).load().token
    origin = {"Origin": settings.public_url}
    assert (
        await client.post("/api/access/session", headers=origin, json={"token": token})
    ).status_code == 204
    before = (await client.get("/api/processing-preferences")).json()
    reply = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": before["version"],
            "asr_language": "vi",
            "notes_provider": "chatgpt",
            "notes_output_language": "en",
        },
    )
    assert reply.status_code == 409
    assert reply.json()["detail"]["code"] == "notes_owner_unconfigured"
    assert (await client.get("/api/processing-preferences")).json() == before


@pytest.mark.anyio
async def test_processing_preferences_patch_rejects_unrepresentable_version_without_write(
    client, settings
):
    token = AuthStore(settings.auth_dir).load().token
    origin = {"Origin": settings.public_url}
    assert (
        await client.post("/api/access/session", headers=origin, json={"token": token})
    ).status_code == 204
    before = (await client.get("/api/processing-preferences")).json()
    reply = await client.patch(
        "/api/processing-preferences",
        headers=origin,
        json={
            "expected_version": 2**64,
            "asr_language": "vi",
            "notes_provider": None,
            "notes_output_language": "en",
        },
    )
    assert reply.status_code == 400
    assert reply.json()["detail"]["code"] == "invalid_preferences_version"
    assert (await client.get("/api/processing-preferences")).json() == before


def test_save_asr_preference_updates_only_asr_when_file_metadata_diverges(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", auth_dir=tmp_path / "auth")
    initialize(settings.data_dir)
    write_default_model(settings, "whisper-small")
    before = save_processing_preferences(
        settings.data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="committed-owner-model",
        notes_reasoning_effort="default",
        notes_output_language="en",
    )

    after = save_asr_preference(settings.data_dir, "whisper-large-v3")

    assert read_default_model(settings) == "whisper-small"
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        stored_updated_at = connection.execute(
            "SELECT updated_at FROM processing_preferences WHERE id = 1"
        ).fetchone()[0]
    assert after == {
        "asr": {"choice": "whisper-large-v3", "language": "vi"},
        "notes": {
            "provider": "chatgpt",
            "model": "committed-owner-model",
            "reasoning_effort": "default",
            "output_language": "en",
        },
        "version": before["version"] + 1,
        "updated_at": stored_updated_at,
    }
    assert isinstance(after["updated_at"], str)
    assert after["updated_at"] == stored_updated_at


def test_workflow_patch_acknowledges_its_transaction_snapshot_under_concurrency(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from threading import Event

    initialize(tmp_path)
    connect = processing_module._connect
    before = processing_module.processing_preferences(tmp_path)
    committed = Event()
    second_finished = Event()

    class ObservedTransaction:
        def __init__(self, connection):
            self.connection = connection
            self.wrote_preferences = False

        def execute(self, statement, values=()):
            if statement.startswith("UPDATE processing_preferences"):
                self.wrote_preferences = True
            return self.connection.execute(statement, values)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            result = self.connection.__exit__(exc_type, exc, traceback)
            if self.wrote_preferences and exc_type is None:
                committed.set()
                assert second_finished.wait(5)
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def observed_connect(directory):
        with connect(directory) as connection:
            yield ObservedTransaction(connection)

    def commit_later_change():
        assert committed.wait(5)
        with connect(tmp_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE processing_preferences SET asr_language = 'vi', "
                "notes_output_language = 'vi', version = version + 1 WHERE id = 1"
            )
        second_finished.set()

    monkeypatch.setattr(processing_module, "_connect", observed_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        later = pool.submit(commit_later_change)
        saved = processing_module.save_workflow_preferences(
            tmp_path,
            expected_version=before["version"],
            asr_language="en",
            notes_provider=None,
            notes_output_language="en",
        )
        later.result(timeout=5)

    current = processing_module.processing_preferences(tmp_path)
    assert current["version"] == before["version"] + 2
    assert current["asr"]["language"] == "vi"
    assert current["notes"]["output_language"] == "vi"
    assert saved["version"] == before["version"] + 1
    assert saved["asr"]["language"] == "en"
    assert saved["notes"]["output_language"] == "en"


def test_asr_only_acknowledges_its_transaction_snapshot_under_concurrency(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event

    initialize(tmp_path)
    connect = processing_module._connect
    before = processing_module.processing_preferences(tmp_path)
    committed = Event()
    second_finished = Event()

    class ObservedTransaction:
        def __init__(self, connection):
            self.connection = connection
            self.wrote_preferences = False

        def execute(self, statement, values=()):
            if statement.startswith("UPDATE processing_preferences"):
                self.wrote_preferences = True
            return self.connection.execute(statement, values)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            result = self.connection.__exit__(exc_type, exc, traceback)
            if self.wrote_preferences and exc_type is None:
                committed.set()
                assert second_finished.wait(5)
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def observed_connect(directory):
        with connect(directory) as connection:
            yield ObservedTransaction(connection)

    def commit_later_change():
        assert committed.wait(5)
        with connect(tmp_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE processing_preferences SET asr_language = 'en', "
                "version = version + 1 WHERE id = 1"
            )
        second_finished.set()

    monkeypatch.setattr(processing_module, "_connect", observed_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        later = pool.submit(commit_later_change)
        saved = processing_module.save_asr_preference(tmp_path, "whisper-large-v3")
        later.result(timeout=5)

    current = processing_module.processing_preferences(tmp_path)
    assert current["version"] == before["version"] + 2
    assert current["asr"] == {"choice": "whisper-large-v3", "language": "en"}
    assert saved["version"] == before["version"] + 1
    assert saved["asr"] == {"choice": "whisper-large-v3", "language": "vi"}


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


def _patch_scheduler_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(processing_module.notes_service, "is_ready", lambda _data_dir: True)


def _publish_manual_transcript(
    data_dir: Path, meeting_id: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    monkeypatch.setattr("asr_studio.tasks.prepared_engine_directory", lambda *_args: None)
    monkeypatch.setattr("asr_studio.tasks.prepared_diarization_directory", lambda *_args: None)
    accepted = accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id="manual-source-0001",
        model_path=Path("/models/whisper-small"),
        language_hint="vi",
        diarization_model_path=Path("/models/speaker-diarization-community-1"),
        device="cpu",
        compute_type="int8",
        worker_ready=True,
        asr_model="whisper-small",
        asr_choice="whisper-small",
    )
    claimed = claim_next_task(data_dir, owner_id="manual-source-owner", role="transcription")
    assert claimed is not None and claimed["id"] == accepted["id"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    generation = publish_complete_generation(
        data_dir,
        claimed["id"],
        owner_id="manual-source-owner",
        segments=[{"id": "d" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Manual."}],
        language_probability=None,
        elapsed_seconds=0.1,
    )
    assert generation is not None
    return generation


def _ready_notes_workflow(
    tmp_path: Path, *, effort: str = "high"
) -> tuple[Path, str, dict[str, object]]:
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
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    assert (
        transcription["source_generation_id"],
        transcription["source_version"],
        transcription["notes_source_version"],
        transcription["metadata_version"],
    ) == (None, None, 1, 1)
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
        transcription_step = next(
            step for step in state["steps"] if step["kind"] == "transcription"
        )
        transcription_attempt = next(
            attempt
            for attempt in state["attempts"]
            if attempt["step_id"] == transcription_step["id"]
        )
        with _connect(data_dir) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return admit_automatic_transcription(
                connection,
                workflow_id=state["id"],
                epoch=1,
                request_id=transcription_attempt["request_id"],
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
    transcription_step = next(step for step in state["steps"] if step["kind"] == "transcription")
    request_id = next(
        attempt["request_id"]
        for attempt in state["attempts"]
        if attempt["step_id"] == transcription_step["id"]
    )
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


def test_automatic_admission_requires_source_confirmation_after_manual_transcript(
    tmp_path, monkeypatch
):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    _publish_manual_transcript(data_dir, meeting_id, monkeypatch)
    state = processing_state(data_dir, meeting_id)
    assert state is not None and state["current_source"]["generation_id"] is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        result = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash=_hash(state["id"], "transcription", state["epoch"], "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert result is None
    fenced = processing_state(data_dir, meeting_id)
    assert fenced is not None
    assert fenced["state"] == "needs_attention"
    fenced_step = next(step for step in fenced["steps"] if step["kind"] == "transcription")
    assert fenced_step["state"] == "needs_attention"
    assert fenced_step["waiting_reason"] == "source_confirmation_required"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)


def test_use_current_source_terminalizes_a_waiting_transcription_step(tmp_path, monkeypatch):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    _publish_manual_transcript(data_dir, meeting_id, monkeypatch)
    state = processing_state(data_dir, meeting_id)
    assert state is not None and state["current_source"] is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_workflows SET state = 'needs_attention' WHERE id = ?",
            (state["id"],),
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'waiting', waiting_reason = 'source_confirmation_required' "
            "WHERE workflow_id = ? AND kind = 'transcription'",
            (state["id"],),
        )
        connection.commit()
    source = state["current_source"]
    adopted = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=state["epoch"],
        expected_generation_id=source["generation_id"],
        expected_source_version=source["source_version"],
        expected_metadata_version=source["metadata_version"],
        expected_notes_source_version=source["notes_source_version"],
    )
    transcription = next(step for step in adopted["steps"] if step["kind"] == "transcription")
    assert transcription["state"] == "succeeded"
    attempt = next(
        attempt for attempt in adopted["attempts"] if attempt["step_id"] == transcription["id"]
    )
    assert attempt["state"] == "canceled"


def test_use_current_source_recovers_metadata_only_pretranscript_workflow(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path, "metadata-only")
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE meetings SET metadata_version = 2 WHERE id = ?", (meeting_id,))
        connection.commit()

    transcription_step = next(step for step in state["steps"] if step["kind"] == "transcription")
    transcription_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription_step["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert (
            admit_automatic_transcription(
                connection,
                workflow_id=state["id"],
                epoch=state["epoch"],
                request_id=transcription_attempt["request_id"],
                request_hash=_hash(state["id"], "transcription", state["epoch"], "whisper-small"),
                model_path=Path("/models/whisper-small"),
                asr_model="whisper-small",
                language_hint="vi",
                device="cpu",
                compute_type="int8",
                diarization_model_path=Path("/models/speaker-diarization-community-1"),
                duration_seconds=12.0,
            )
            is None
        )
    fenced = processing_state(data_dir, meeting_id)
    assert fenced is not None and fenced["state"] == "needs_attention"
    assert fenced["current_source"] == {
        "generation_id": None,
        "source_version": None,
        "metadata_version": 2,
        "notes_source_version": 1,
    }

    with pytest.raises(ProcessingError) as stale:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=1,
            expected_generation_id=None,
            expected_source_version=None,
            expected_metadata_version=1,
            expected_notes_source_version=1,
        )
    assert stale.value.code == "source_changed"

    adopted = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=1,
        expected_generation_id=None,
        expected_source_version=None,
        expected_metadata_version=2,
        expected_notes_source_version=1,
    )
    assert adopted["state"] == "waiting"
    assert adopted["epoch"] == 2
    transcription = next(step for step in adopted["steps"] if step["kind"] == "transcription")
    notes = next(step for step in adopted["steps"] if step["kind"] == "notes")
    assert {
        key: transcription[key]
        for key in (
            "state",
            "waiting_reason",
            "task_id",
            "source_generation_id",
            "source_version",
            "notes_source_version",
            "metadata_version",
        )
    } == {
        "state": "waiting",
        "waiting_reason": None,
        "task_id": None,
        "source_generation_id": None,
        "source_version": None,
        "notes_source_version": 1,
        "metadata_version": 2,
    }
    assert {
        key: notes[key]
        for key in (
            "state",
            "waiting_reason",
            "task_id",
            "source_snapshot_id",
            "source_generation_id",
            "source_version",
            "notes_source_version",
            "metadata_version",
        )
    } == {
        "state": "waiting",
        "waiting_reason": "transcript_required",
        "task_id": None,
        "source_snapshot_id": None,
        "source_generation_id": None,
        "source_version": None,
        "notes_source_version": 1,
        "metadata_version": 2,
    }
    current_transcription_attempt = next(
        attempt
        for attempt in adopted["attempts"]
        if attempt["step_id"] == transcription["id"] and attempt["attempt_number"] == 2
    )
    assert current_transcription_attempt["state"] == "waiting"
    assert (
        next(
            attempt
            for attempt in adopted["attempts"]
            if attempt["step_id"] == transcription["id"] and attempt["attempt_number"] == 1
        )["state"]
        == "canceled"
    )

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=adopted["id"],
            epoch=adopted["epoch"],
            request_id=current_transcription_attempt["request_id"],
            request_hash=_hash(adopted["id"], "transcription", adopted["epoch"], "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None


def test_repeated_source_adoption_keeps_notes_attempt_keys_in_their_kind_namespace(
    tmp_path, monkeypatch
):
    data_dir, meeting_id, _ = _ready_source(tmp_path, "repeated-source")
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE meetings SET metadata_version = 2 WHERE id = ?", (meeting_id,))
        connection.commit()
    metadata_confirmed = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=initial["epoch"],
        expected_generation_id=None,
        expected_source_version=None,
        expected_metadata_version=2,
        expected_notes_source_version=1,
    )
    assert metadata_confirmed["epoch"] == 2

    _patch_scheduler_runtime(monkeypatch)
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        None,
        None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert scheduler.reconcile_once() == 1
    transcription = claim_next_task(
        data_dir, owner_id="repeated-source-transcription", role="transcription"
    )
    assert transcription is not None
    assert fail_task(
        data_dir,
        transcription["id"],
        owner_id="repeated-source-transcription",
        error_code="test_failure",
        message="test failure",
        elapsed_seconds=0.1,
    )
    assert scheduler.reconcile_once() == 0
    after_retry = processing_state(data_dir, meeting_id)
    assert after_retry is not None
    transcription_step = next(
        step for step in after_retry["steps"] if step["kind"] == "transcription"
    )
    notes_step = next(step for step in after_retry["steps"] if step["kind"] == "notes")
    assert transcription_step["attempt_number"] == 3
    assert notes_step["attempt_number"] == 2

    _publish_manual_transcript(data_dir, meeting_id, monkeypatch)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_workflows SET state = 'needs_attention' WHERE meeting_id = ?",
            (meeting_id,),
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'waiting', waiting_reason = 'source_confirmation_required' "
            "WHERE workflow_id = (SELECT id FROM processing_workflows WHERE meeting_id = ?) "
            "AND kind = 'transcription'",
            (meeting_id,),
        )
        connection.commit()
    before_adoption = processing_state(data_dir, meeting_id)
    assert before_adoption is not None and before_adoption["current_source"] is not None
    source = before_adoption["current_source"]
    first_adoption = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=before_adoption["epoch"],
        expected_generation_id=source["generation_id"],
        expected_source_version=source["source_version"],
        expected_metadata_version=source["metadata_version"],
        expected_notes_source_version=source["notes_source_version"],
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE meetings SET metadata_version = 3 WHERE id = ?", (meeting_id,))
        connection.execute(
            "UPDATE processing_workflows SET state = 'needs_attention' WHERE meeting_id = ?",
            (meeting_id,),
        )
        connection.commit()
    second_source = first_adoption["current_source"]
    assert second_source is not None
    second_adoption = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=first_adoption["epoch"],
        expected_generation_id=second_source["generation_id"],
        expected_source_version=second_source["source_version"],
        expected_metadata_version=3,
        expected_notes_source_version=second_source["notes_source_version"],
    )

    notes_attempts = [
        attempt
        for attempt in second_adoption["attempts"]
        if attempt["step_id"]
        == next(step for step in second_adoption["steps"] if step["kind"] == "notes")["id"]
    ]
    transcription_attempts = [
        attempt
        for attempt in second_adoption["attempts"]
        if attempt["step_id"]
        == next(step for step in second_adoption["steps"] if step["kind"] == "transcription")["id"]
    ]
    assert [attempt["attempt_number"] for attempt in notes_attempts[-2:]] == [3, 4]
    assert [attempt["request_id"] for attempt in notes_attempts[-2:]] == [
        f"auto:{second_adoption['id']}:notes:attempt:3",
        f"auto:{second_adoption['id']}:notes:attempt:4",
    ]
    assert transcription_attempts[-1]["request_id"] == (
        f"auto:{second_adoption['id']}:transcription:attempt:3"
    )
    assert (
        len(
            {
                attempt["request_id"]
                for attempt in (*notes_attempts[-2:], transcription_attempts[-1])
            }
        )
        == 3
    )


def test_use_current_source_rejects_mixed_null_generation_and_source_version(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path, "metadata-mixed")
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE meetings SET metadata_version = 2 WHERE id = ?", (meeting_id,))
        connection.commit()
    with pytest.raises(ProcessingError) as error:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=1,
            expected_generation_id=None,
            expected_source_version=1,
            expected_metadata_version=2,
            expected_notes_source_version=1,
        )
    assert error.value.code == "invalid_source_revision"


def test_automatic_publication_rechecks_metadata_source_and_releases_blocked_task(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash=_hash(state["id"], "transcription", state["epoch"], "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    claimed = claim_next_task(data_dir, owner_id="publication-owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
        connection.execute("UPDATE meetings SET metadata_version = 2 WHERE id = ?", (meeting_id,))
        connection.commit()
    assert (
        publish_complete_generation(
            data_dir,
            claimed["id"],
            owner_id="publication-owner",
            segments=[{"id": "e" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Blocked."}],
            language_probability=None,
            elapsed_seconds=0.1,
        )
        is None
    )
    fenced = processing_state(data_dir, meeting_id)
    assert fenced is not None and fenced["state"] == "needs_attention"
    assert (
        next(step for step in fenced["steps"] if step["kind"] == "transcription")["waiting_reason"]
        == "source_confirmation_required"
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transcript_generations").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (claimed["id"],)
        ).fetchone() == ("interrupted",)
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)


def test_automatic_admission_rejects_a_nonwaiting_attempt(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_attempts SET state = 'failed' WHERE id = ?", (attempt["id"],)
        )
        connection.commit()
    with pytest.raises(ProcessingError) as error, _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash=_hash(state["id"], "transcription", state["epoch"], "whisper-small"),
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/models/speaker-diarization-community-1"),
            duration_seconds=12.0,
        )
    assert error.value.code == "processing_attempt_changed"


def test_successful_transcript_freezes_source_and_notes_retry_reuses_it(tmp_path):
    data_dir, meeting_id, _ = _ready_source(tmp_path)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    workflow_id = state["id"]
    transcription_step = next(step for step in state["steps"] if step["kind"] == "transcription")
    transcription_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription_step["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        result = admit_automatic_transcription(
            connection,
            workflow_id=workflow_id,
            epoch=1,
            request_id=transcription_attempt["request_id"],
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


def test_settings_adoption_replaces_a_terminal_notes_attempt(tmp_path):
    data_dir, meeting_id, state = _ready_notes_workflow(tmp_path, effort="high")
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    notes_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == notes_step["id"]
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_attempts SET state = 'failed' WHERE id = ?", (notes_attempt["id"],)
        )
        connection.commit()
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort="high",
        notes_output_language="en",
    )
    adopted = apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=state["epoch"],
        expected_preferences_version=preferences["version"],
        confirmed_pin=_notes_pin("high"),
    )
    assert len(adopted["attempts"]) == len(state["attempts"]) + 1
    current_notes = next(step for step in adopted["steps"] if step["kind"] == "notes")
    current_attempt = max(
        (attempt for attempt in adopted["attempts"] if attempt["step_id"] == current_notes["id"]),
        key=lambda attempt: attempt["attempt_number"],
    )
    assert current_attempt["state"] == "waiting"


def test_scheduler_requeues_legacy_notes_attempt_after_metadata_confirmation_and_keeps_queue_open(
    tmp_path, monkeypatch
):
    data_dir, meeting_id, _ = _ready_source(tmp_path, "legacy-requeue")
    preferences = save_processing_preferences(
        data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model="stub-model",
        notes_reasoning_effort="high",
        notes_output_language="en",
    )
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    adopted_settings = apply_current_settings(
        data_dir,
        meeting_id,
        expected_epoch=initial["epoch"],
        expected_preferences_version=preferences["version"],
        confirmed_pin=_notes_pin("high"),
    )
    assert adopted_settings["epoch"] == initial["epoch"] + 1
    assert len(adopted_settings["attempts"]) == len(initial["attempts"])

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE meetings SET metadata_version = 2 WHERE id = ?", (meeting_id,))
        connection.commit()
    confirmed = use_current_source(
        data_dir,
        meeting_id,
        expected_epoch=adopted_settings["epoch"],
        expected_generation_id=None,
        expected_source_version=None,
        expected_metadata_version=2,
        expected_notes_source_version=1,
    )
    assert confirmed["epoch"] == adopted_settings["epoch"] + 1
    notes_step = next(step for step in confirmed["steps"] if step["kind"] == "notes")
    legacy_request_id = f"auto:{confirmed['id']}:notes:3"
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_attempts SET request_id = ? WHERE id = ?",
            (
                legacy_request_id,
                next(
                    attempt["id"]
                    for attempt in confirmed["attempts"]
                    if attempt["step_id"] == notes_step["id"]
                    and attempt["attempt_number"] == notes_step["attempt_number"]
                ),
            ),
        )
        connection.commit()

    _patch_scheduler_runtime(monkeypatch)
    provider = _ProcessingNotesProvider(effort="high")
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        provider,
        provider,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert scheduler.reconcile_once() == 1
    transcription = claim_next_task(
        data_dir, owner_id="legacy-requeue-transcription", role="transcription"
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
            owner_id="legacy-requeue-transcription",
            segments=[{"id": "e" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Legacy."}],
            language_probability=None,
            elapsed_seconds=0.1,
        )
        is not None
    )
    assert scheduler.reconcile_once() == 1
    notes_task = claim_next_task(data_dir, owner_id="legacy-requeue-notes", role="notes")
    assert notes_task is not None
    assert fail_task(
        data_dir,
        notes_task["id"],
        owner_id="legacy-requeue-notes",
        error_code="test_failure",
        message="test failure",
        elapsed_seconds=0.1,
    )

    _, unrelated_meeting, _ = _ready_source(tmp_path, "unrelated")
    assert scheduler.reconcile_once() == 0
    assert scheduler.reconcile_once() == 1
    unrelated = processing_state(data_dir, unrelated_meeting)
    assert unrelated is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        admitted = connection.execute(
            "SELECT id, processing_workflow_id, request_id FROM tasks "
            "WHERE processing_workflow_id = ? AND status = 'accepted'",
            (unrelated["id"],),
        ).fetchone()
    assert admitted is not None
    first_after_retry = processing_state(data_dir, meeting_id)
    assert first_after_retry is not None
    current_notes_step = next(
        step for step in first_after_retry["steps"] if step["kind"] == "notes"
    )
    current_notes_attempt = max(
        (
            attempt
            for attempt in first_after_retry["attempts"]
            if attempt["step_id"] == current_notes_step["id"]
        ),
        key=lambda attempt: attempt["attempt_number"],
    )
    assert current_notes_attempt["attempt_number"] == 3
    assert current_notes_attempt["request_id"] != legacy_request_id
    assert current_notes_attempt["request_id"].endswith(":attempt:3")
    cancel_task(data_dir, admitted[0])


def test_scheduler_isolates_a_malformed_failed_step_from_unrelated_admission(tmp_path, monkeypatch):
    data_dir, malformed_meeting, _ = _ready_source(tmp_path, "malformed")
    _, unrelated_meeting, _ = _ready_source(tmp_path, "unrelated-malformed-x")
    malformed = processing_state(data_dir, malformed_meeting)
    assert malformed is not None
    transcription = next(step for step in malformed["steps"] if step["kind"] == "transcription")
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_steps SET state = 'failed', last_error_code = 'test_failure' "
            "WHERE id = ?",
            (transcription["id"],),
        )
        connection.execute(
            "INSERT INTO processing_attempts(id, step_id, attempt_number, request_id, request_hash, workflow_epoch, state) "
            "VALUES (?, ?, 2, ?, ?, 1, 'failed')",
            (
                uuid.uuid4().hex,
                transcription["id"],
                "malformed-attempt-0001",
                "f" * 64,
            ),
        )
        connection.commit()

    _patch_scheduler_runtime(monkeypatch)
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        None,
        None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert scheduler.reconcile_once() == 1
    unrelated = processing_state(data_dir, unrelated_meeting)
    assert unrelated is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        admitted = connection.execute(
            "SELECT id, processing_workflow_id FROM tasks "
            "WHERE processing_workflow_id = ? AND status = 'accepted'",
            (unrelated["id"],),
        ).fetchone()
    assert admitted is not None
    still_malformed = processing_state(data_dir, malformed_meeting)
    assert still_malformed is not None
    malformed_step = next(
        step for step in still_malformed["steps"] if step["kind"] == "transcription"
    )
    assert malformed_step["state"] == "failed"
    cancel_task(data_dir, admitted[0])


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
    monkeypatch.setattr("asr_studio.tasks.prepared_engine_directory", lambda *_args: None)
    monkeypatch.setattr("asr_studio.tasks.prepared_diarization_directory", lambda *_args: None)
    grace_sleeps: list[float] = []
    manual_task: dict[str, object] = {}

    def grace_sleep(seconds: float) -> None:
        grace_sleeps.append(seconds)
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone() == (None,)
        manual_task.update(
            accept_transcription_task(
                data_dir,
                meeting_id=meeting_id,
                request_id="manual-grace-0001",
                model_path=Path("/models/whisper-small"),
                language_hint="vi",
                diarization_model_path=Path("/models/speaker-diarization-community-1"),
                device="cpu",
                compute_type="int8",
                worker_ready=True,
                asr_model="whisper-small",
                asr_choice="whisper-small",
            )
        )

    provider = _ProcessingNotesProvider(effort="high")
    scheduler = AutomaticProcessingScheduler(
        Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
        provider,
        provider,
        sleep=grace_sleep,
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
    notes_task = claim_next_task(data_dir, owner_id="grace-notes-owner", role="notes")
    assert notes_task is not None
    assert fail_task(
        data_dir,
        notes_task["id"],
        owner_id="grace-notes-owner",
        error_code="test_failure",
        message="test failure",
        elapsed_seconds=0.1,
    )
    assert scheduler.reconcile_once() == 0
    assert grace_sleeps == [1.0]
    assert manual_task["status"] == "accepted"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (3,)
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (manual_task["id"],)
    cancel_task(data_dir, manual_task["id"])


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
