from __future__ import annotations

import os
import sqlite3

import pytest
from notes_helpers import StubNotesProvider, seed_completed_transcript

from asr_studio import notes_service, worker
from asr_studio.config import Settings
from asr_studio.notes import accept_notes_task
from asr_studio.runtime import RuntimeProfile
from asr_studio.service_runtime import acquire_service_probe
from asr_studio.storage import database_path
from asr_studio.tasks import claim_next_task, get_task, recover_abandoned_tasks


def _accepted_notes(data_dir, request_id: str = "service-notes-request-001"):
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    return accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )


def test_role_scoped_claim_does_not_claim_a_notes_task_for_transcription(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)

    assert claim_next_task(data_dir, owner_id="transcription-owner", role="transcription") is None
    claimed = claim_next_task(data_dir, owner_id="notes-owner", role="notes")

    assert claimed is not None and claimed["id"] == accepted["id"]
    assert claimed["owner_role"] == "notes"


def test_transcription_recovery_leaves_a_notes_task_and_its_shared_slot_alone(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="notes-owner", role="notes") is not None

    assert recover_abandoned_tasks(data_dir, role="transcription") == []
    assert get_task(data_dir, accepted["id"])["status"] == "running"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (accepted["id"],)


def test_notes_recovery_does_not_interrupt_a_live_legacy_combined_worker(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="legacy-owner") is not None
    legacy_fd = worker.acquire_worker_lock(data_dir)
    assert legacy_fd is not None
    try:
        with pytest.raises(notes_service.NotesServiceAlreadyRunning):
            notes_service.run_notes_service(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
        assert get_task(data_dir, accepted["id"])["status"] == "running"
    finally:
        os.close(legacy_fd)


def test_notes_service_has_a_separate_lifetime_lock_and_heartbeat(tmp_path):
    data_dir = tmp_path / "data"
    lock_fd = notes_service.acquire_lock(data_dir)
    assert lock_fd is not None
    try:
        notes_service.write_heartbeat(data_dir, "idle")
        assert notes_service.is_healthy(data_dir)
        assert not worker.is_healthy(data_dir)
    finally:
        os.close(lock_fd)


def test_legacy_worker_cannot_start_while_the_split_notes_lock_is_live(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    gate_fd = acquire_service_probe(data_dir, "worker", retry_seconds=0)
    assert gate_fd is not None
    notes_fd = notes_service.acquire_lock(data_dir)
    assert notes_fd is not None
    monkeypatch.setattr(
        worker,
        "select_runtime",
        lambda _settings: pytest.fail("legacy startup reached model initialization"),
    )
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            worker.run_worker(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
    finally:
        os.close(notes_fd)
        os.close(gate_fd)


def test_legacy_worker_cannot_start_while_split_transcription_is_live(tmp_path):
    data_dir = tmp_path / "data"
    gate_fd = acquire_service_probe(data_dir, "worker", retry_seconds=0)
    assert gate_fd is not None
    worker_fd = worker.acquire_transcription_lock(data_dir)
    assert worker_fd is not None
    worker.write_transcription_heartbeat(data_dir, "idle")
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            worker.run_worker(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
        assert worker.is_healthy(data_dir, role="transcription")
    finally:
        os.close(worker_fd)
        os.close(gate_fd)


def test_split_transcription_cannot_recover_while_legacy_worker_lock_is_live(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    legacy_fd = worker.acquire_worker_lock(data_dir)
    assert legacy_fd is not None
    worker.write_heartbeat(data_dir, "idle", role="legacy")
    monkeypatch.setattr(
        worker,
        "select_runtime",
        lambda _settings: RuntimeProfile("cpu", "int8", tmp_path / "model"),
    )
    monkeypatch.setattr(worker, "prepared_diarization_directory", lambda _path: None)
    monkeypatch.setattr(worker, "write_runtime_profile", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "recover_abandoned_tasks",
        lambda *_args, **_kwargs: pytest.fail("split startup reached recovery"),
    )
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            worker.run_worker(
                Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
                role="transcription",
            )
    finally:
        os.close(legacy_fd)


def test_transcription_startup_uses_own_liveness_and_both_executor_locks(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, auth_dir=tmp_path / "auth")
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        worker,
        "select_runtime",
        lambda _settings: RuntimeProfile("cpu", "int8", tmp_path / "model"),
    )
    monkeypatch.setattr(worker, "prepared_diarization_directory", lambda _path: None)
    monkeypatch.setattr(worker, "write_runtime_profile", lambda *_args: None)

    def inspect_recovery(directory, **kwargs):
        observed["recovery_role"] = kwargs["role"]
        observed["starting"] = (
            worker.transcription_heartbeat_path(directory)
            .read_text(encoding="ascii")
            .startswith("starting ")
        )

    def inspect_loop(_settings, lock_fd, _stop_event, **kwargs):
        observed["lock_fd"] = lock_fd
        observed["loop_role"] = kwargs["role"]
        assert isinstance(lock_fd, tuple) and len(lock_fd) == 2
        assert worker.acquire_transcription_lock(data_dir, retry_seconds=0) is None
        assert worker.acquire_worker_lock(data_dir, retry_seconds=0) is None

    monkeypatch.setattr(worker, "recover_abandoned_tasks", inspect_recovery)
    monkeypatch.setattr(worker, "_worker_loop", inspect_loop)
    worker.run_worker(settings, role="transcription")

    assert observed["recovery_role"] == "transcription"
    assert observed["loop_role"] == "transcription"
    assert observed["starting"] is True
    assert (
        worker.transcription_heartbeat_path(data_dir)
        .read_text(encoding="ascii")
        .startswith("stopped ")
    )
    assert not worker.heartbeat_path(data_dir).exists()


def test_legacy_gate_blocks_recovery_for_old_sql_claim_with_new_role_marker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET status = 'running', stage = 'starting', owner_id = ? WHERE id = ?",
            ("old-sql-owner", accepted["id"]),
        )
        before = connection.execute(
            "SELECT status, owner_id, owner_role FROM tasks WHERE id = ?",
            (accepted["id"],),
        ).fetchone()
        slot_before = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()
    legacy_fd = worker.acquire_worker_lock(data_dir)
    assert legacy_fd is not None
    monkeypatch.setattr(
        worker,
        "recover_abandoned_tasks",
        lambda *_args, **_kwargs: pytest.fail("split startup reached recovery"),
    )
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            worker.run_worker(
                Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"),
                role="transcription",
            )
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert (
                connection.execute(
                    "SELECT status, owner_id, owner_role FROM tasks WHERE id = ?",
                    (accepted["id"],),
                ).fetchone()
                == before
            )
            assert (
                connection.execute(
                    "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
                ).fetchone()
                == slot_before
            )
    finally:
        os.close(legacy_fd)


def test_notes_startup_lock_failure_does_not_overwrite_live_notes_heartbeat(tmp_path):
    data_dir = tmp_path / "data"
    notes_fd = notes_service.acquire_lock(data_dir)
    assert notes_fd is not None
    notes_service.write_heartbeat(data_dir, "idle")
    try:
        with pytest.raises(notes_service.NotesServiceAlreadyRunning):
            notes_service.run_notes_service(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
        assert (
            notes_service.heartbeat_path(data_dir).read_text(encoding="ascii").startswith("idle ")
        )
    finally:
        os.close(notes_fd)


def test_legacy_worker_gate_blocks_a_split_notes_start(tmp_path):
    data_dir = tmp_path / "data"
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    try:
        with pytest.raises(notes_service.NotesServiceAlreadyRunning):
            notes_service.run_notes_service(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
    finally:
        os.close(worker_fd)


def test_split_notes_start_rejects_live_legacy_without_heartbeat(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="legacy-owner") is not None
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    try:
        with pytest.raises(notes_service.NotesServiceAlreadyRunning):
            notes_service.run_notes_service(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
        assert get_task(data_dir, accepted["id"])["status"] == "running"
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone() == (accepted["id"],)
    finally:
        os.close(worker_fd)


def test_split_notes_recovery_is_role_scoped_and_does_not_clear_other_slot(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="notes-owner", role="notes") is not None
    assert recover_abandoned_tasks(data_dir, role="transcription") == []
    assert get_task(data_dir, accepted["id"])["status"] == "running"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (accepted["id"],)


def test_split_roles_hold_distinct_locks_and_shared_legacy_gate(tmp_path):
    data_dir = tmp_path / "data"
    transcription_fd = worker.acquire_transcription_lock(data_dir)
    assert transcription_fd is not None
    transcription_gate_fd = acquire_service_probe(data_dir, "worker", retry_seconds=0)
    assert transcription_gate_fd is not None
    notes_fd = notes_service.acquire_lock(data_dir)
    assert notes_fd is not None
    notes_gate_fd = acquire_service_probe(data_dir, "worker", retry_seconds=0)
    assert notes_gate_fd is not None
    try:
        assert worker.acquire_transcription_lock(data_dir, retry_seconds=0) is None
        assert notes_service.acquire_lock(data_dir, retry_seconds=0) is None
        assert worker.acquire_worker_lock(data_dir, retry_seconds=0) is None
    finally:
        os.close(notes_gate_fd)
        os.close(notes_fd)
        os.close(transcription_gate_fd)
        os.close(transcription_fd)


def test_pre_split_accepted_notes_task_remains_pending_for_split_service(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET owner_role = 'legacy' WHERE id = ?", (accepted["id"],))

    assert recover_abandoned_tasks(data_dir, role="notes") == []
    assert get_task(data_dir, accepted["id"])["status"] == "accepted"
    claimed = claim_next_task(data_dir, owner_id="notes-owner", role="notes")
    assert claimed is not None and claimed["id"] == accepted["id"]
    assert claimed["owner_role"] == "notes"


def test_notes_readiness_rejects_transcription_only_worker_but_accepts_legacy(tmp_path):
    data_dir = tmp_path / "data"
    worker_fd = worker.acquire_transcription_lock(data_dir)
    assert worker_fd is not None
    try:
        worker.write_transcription_heartbeat(data_dir, "idle")
        assert not notes_service.is_ready(data_dir)
    finally:
        os.close(worker_fd)

    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    try:
        worker.write_heartbeat(data_dir, "idle", role="legacy")
        assert notes_service.is_ready(data_dir)
    finally:
        os.close(worker_fd)


def test_worker_health_defaults_to_either_role_but_legacy_is_explicit(tmp_path):
    data_dir = tmp_path / "data"
    transcription_fd = worker.acquire_transcription_lock(data_dir)
    assert transcription_fd is not None
    worker.write_transcription_heartbeat(data_dir, "idle")
    try:
        assert worker.is_healthy(data_dir)
        assert worker.is_active(data_dir)
        assert not worker.is_healthy(data_dir, role="legacy")
        assert not worker.is_active(data_dir, role="legacy")
    finally:
        os.close(transcription_fd)

    legacy_fd = worker.acquire_worker_lock(data_dir)
    assert legacy_fd is not None
    worker.write_heartbeat(data_dir, "idle")
    try:
        assert worker.is_healthy(data_dir)
        assert worker.is_healthy(data_dir, role="legacy")
    finally:
        os.close(legacy_fd)
