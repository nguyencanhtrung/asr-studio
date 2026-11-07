from __future__ import annotations

import os
import sqlite3

import pytest
from notes_helpers import StubNotesProvider, seed_completed_transcript

from asr_studio import notes_service, worker
from asr_studio.config import Settings
from asr_studio.notes import accept_notes_task
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

    assert recover_abandoned_tasks(data_dir, role="notes", legacy_worker_active=True) == []
    assert get_task(data_dir, accepted["id"])["status"] == "running"


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


def test_legacy_worker_cannot_start_while_split_transcription_is_live(tmp_path):
    data_dir = tmp_path / "data"
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    worker.write_heartbeat(data_dir, "idle", role="transcription")
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            worker.run_worker(Settings(data_dir=data_dir, auth_dir=tmp_path / "auth"))
        assert worker.worker_role(data_dir) == "transcription"
    finally:
        os.close(worker_fd)


def test_split_transcription_cannot_recover_while_legacy_worker_lock_is_live(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    legacy_fd = worker.acquire_worker_lock(data_dir)
    assert legacy_fd is not None
    worker.write_heartbeat(data_dir, "idle", role="legacy")
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


def test_legacy_worker_reservation_blocks_a_split_notes_start(tmp_path):
    data_dir = tmp_path / "data"
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    probe_fd = acquire_service_probe(data_dir, "notes")
    assert probe_fd is not None
    try:
        assert notes_service.acquire_lock(data_dir, retry_seconds=0) is None
    finally:
        os.close(probe_fd)
        os.close(worker_fd)


def test_split_notes_recovery_preserves_live_legacy_work_by_worker_lock(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="legacy-owner") is not None
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    notes_fd = notes_service.acquire_lock(data_dir)
    assert notes_fd is not None  # models a pre-upgrade legacy process without the probe
    worker.write_heartbeat(data_dir, "idle", role="legacy")
    try:
        assert worker.is_active(data_dir, role="legacy")
        assert (
            recover_abandoned_tasks(
                data_dir,
                role="notes",
                legacy_worker_active=worker.is_active(data_dir, role="legacy"),
            )
            == []
        )
        assert get_task(data_dir, accepted["id"])["status"] == "running"
    finally:
        os.close(notes_fd)
        os.close(worker_fd)


def test_split_notes_recovery_uses_legacy_lock_without_heartbeat(tmp_path):
    data_dir = tmp_path / "data"
    accepted = _accepted_notes(data_dir)
    assert claim_next_task(data_dir, owner_id="legacy-owner") is not None
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    notes_fd = notes_service.acquire_lock(data_dir)
    assert notes_fd is not None
    try:
        assert notes_service._legacy_worker_active(data_dir)
        assert (
            recover_abandoned_tasks(
                data_dir,
                role="notes",
                legacy_worker_active=notes_service._legacy_worker_active(data_dir),
            )
            == []
        )
        assert get_task(data_dir, accepted["id"])["status"] == "running"
    finally:
        os.close(notes_fd)
        os.close(worker_fd)


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
    worker_fd = worker.acquire_worker_lock(data_dir)
    assert worker_fd is not None
    try:
        worker.write_heartbeat(data_dir, "idle", role="transcription")
        assert not notes_service.is_ready(data_dir)
        worker.write_heartbeat(data_dir, "idle", role="legacy")
        assert notes_service.is_ready(data_dir)
    finally:
        os.close(worker_fd)
