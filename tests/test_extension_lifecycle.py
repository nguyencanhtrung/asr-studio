from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_extension_ingest import _create_payload, _identity, _manifest
from test_extension_media import _webm

from asr_studio import extension_ingest as ingest
from asr_studio.media import MediaProcessingError
from asr_studio.meeting_deletion import delete_meeting
from asr_studio.storage import database_path, initialize


def _prepared(settings, content=b"synthetic archived bytes"):
    initialize(settings.data_dir)
    identity = _identity(settings, "owner-lifecycle-0001")
    session, _ = ingest.create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=_create_payload(),
        backend_id="b" * 32,
        settings=settings,
    )
    lease = ingest.begin_chunk(
        settings.data_dir,
        identity=identity,
        session_id=session["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(content).hexdigest(),
        epoch=1,
        declared_length=len(content),
        settings=settings,
    )
    lease.write(content)
    lease.commit()
    ingest.finalize_recording_session(
        settings.data_dir,
        identity=identity,
        session_id=session["id"],
        payload=_manifest(content, epoch=1),
        backend_id="b" * 32,
        settings=settings,
    )
    return identity, session, content


def _prepared_named(settings, *, owner_id, client_id, content):
    initialize(settings.data_dir)
    identity = _identity(settings, owner_id)
    payload = {
        **_create_payload(),
        "client_recording_id": client_id,
        "capture_epoch": f"capture-{client_id}",
    }
    session, _ = ingest.create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=payload,
        backend_id="b" * 32,
        settings=settings,
    )
    lease = ingest.begin_chunk(
        settings.data_dir,
        identity=identity,
        session_id=session["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(content).hexdigest(),
        epoch=1,
        declared_length=len(content),
        settings=settings,
    )
    lease.write(content)
    lease.commit()
    manifest = _manifest(content, epoch=1)
    manifest.update(
        capture_epoch=f"capture-{client_id}",
        finalize_id=f"finalize-{client_id}",
    )
    ingest.finalize_recording_session(
        settings.data_dir,
        identity=identity,
        session_id=session["id"],
        payload=manifest,
        backend_id="b" * 32,
        settings=settings,
    )
    return identity, session, content


def _install_receipt(settings, session_id, content, *, state="archived"):
    raw_path = settings.data_dir / "extension-raw" / f"{session_id}.webm"
    raw_path.write_bytes(content)
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE extension_recording_sessions SET state = ?, raw_key = ?, raw_bytes = ?, "
            "archive_sha256 = ?, receipt_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                state,
                f"extension-raw/{session_id}.webm",
                len(content),
                hashlib.sha256(content).hexdigest(),
                session_id,
            ),
        )


def test_receipt_cleanup_releases_quota_and_rejects_closed_chunk_replay(settings, monkeypatch):
    quota_settings = replace(settings, extension_max_staging_bytes=4)

    def normalize(*args, **kwargs):
        return SimpleNamespace(duration_seconds=1.0)

    monkeypatch.setattr(ingest, "normalize_extension_media", normalize)
    identity, session, content = _prepared(quota_settings, content=b"four")
    _run(quota_settings, session)

    current = _replay(quota_settings, identity, session, content, "status")
    assert current["state"] == "ready"
    chunk = quota_settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"
    assert not chunk.exists()
    with sqlite3.connect(database_path(quota_settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_chunks WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_reservations WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 0
        )

    with pytest.raises(ingest.ExtensionIngestError) as replay:
        ingest.begin_chunk(
            quota_settings.data_dir,
            identity=identity,
            session_id=session["id"],
            sequence=0,
            offset=0,
            sha256=hashlib.sha256(content).hexdigest(),
            epoch=1,
            declared_length=len(content),
            settings=quota_settings,
        )
    assert replay.value.code == "session_state_conflict"

    later, status = ingest.create_recording_session(
        quota_settings.data_dir,
        identity=identity,
        payload={
            **_create_payload(),
            "client_recording_id": "later-recording",
            "capture_epoch": "later-capture",
        },
        backend_id="b" * 32,
        settings=quota_settings,
    )
    assert status == 201
    assert later["state"] == "receiving"


def test_receipt_cleanup_recovery_removes_partial_staging_after_raw_verification(settings):
    identity, session, content = _prepared(settings)
    _install_receipt(settings, session["id"], content)
    chunk = settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"
    chunk.unlink()

    ingest.recover_extension_ingest(settings.data_dir)

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        stored = connection.execute(
            "SELECT state, raw_bytes, archive_sha256, receipt_at "
            "FROM extension_recording_sessions WHERE id = ?",
            (session["id"],),
        ).fetchone()
        assert stored[0] == "archived"
        assert stored[1:] == (len(content), hashlib.sha256(content).hexdigest(), stored[3])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_chunks WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 0
        )
    assert (settings.data_dir / "extension-raw" / f"{session['id']}.webm").read_bytes() == content


def test_receipt_cleanup_never_releases_staging_without_durable_raw(settings):
    _identity_value, session, content = _prepared(settings)
    _install_receipt(settings, session["id"], content)
    raw_path = settings.data_dir / "extension-raw" / f"{session['id']}.webm"
    raw_path.unlink()
    chunk = settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"

    ingest.recover_extension_ingest(settings.data_dir)

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_chunks WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 1
        )
    assert chunk.exists()


def test_receipt_cleanup_failure_is_visible_and_retryable_after_recovery(settings, monkeypatch):
    identity, session, content = _prepared(settings)
    original_release = ingest._release_receipted_staging
    failed_once = True

    def fail_first_cleanup(*args, **kwargs):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise OSError("simulated staging cleanup interruption")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(ingest, "_release_receipted_staging", fail_first_cleanup)
    _run(settings, session)

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        stored = connection.execute(
            "SELECT state, error_code, raw_bytes, archive_sha256, receipt_at "
            "FROM extension_recording_sessions WHERE id = ?",
            (session["id"],),
        ).fetchone()
        assert stored[0:2] == ("failed", "staging_cleanup_failed")
        assert stored[2] == len(content)
        assert stored[3] == hashlib.sha256(content).hexdigest()
        assert stored[4] is not None
    assert ingest.ExtensionIngestWorker(settings)._pending_session() is None

    monkeypatch.setattr(ingest, "_release_receipted_staging", original_release)
    ingest.recover_extension_ingest(settings.data_dir)
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_chunks WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 0
        )

    def normalize(*args, **kwargs):
        return SimpleNamespace(duration_seconds=1.0)

    monkeypatch.setattr(ingest, "normalize_extension_media", normalize)
    receipt = _replay(settings, identity, session, content, "status")["receipt"]
    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    enqueued = []
    retried = ingest.retry_recording_session(settings.data_dir, meeting_id, enqueue=enqueued.append)
    assert retried == {
        "meeting_id": meeting_id,
        "recording_state": "finalizing",
        "retry_requested": True,
    }
    assert enqueued == [session["id"]]
    _run(settings, session)
    current = _replay(settings, identity, session, content, "status")
    assert current["state"] == "ready"
    assert current["receipt"] == receipt


def test_deletion_marked_receipted_session_does_not_starve_queue(settings, monkeypatch):
    identity_a, session_a, content_a = _prepared_named(
        settings,
        owner_id="owner-queue-a-0001",
        client_id="queue-a",
        content=b"aaaa",
    )
    _identity_b, session_b, content_b = _prepared_named(
        settings,
        owner_id="owner-queue-b-0001",
        client_id="queue-b",
        content=b"bbbb",
    )
    _install_receipt(settings, session_a["id"], content_a)
    meeting_a = session_a["meeting_url"].rsplit("/", 1)[-1]
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE meetings SET deletion_requested_at = ?, deletion_error_code = ? WHERE id = ?",
            ("2026-11-15T00:00:00Z", "cleanup_failed", meeting_a),
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET updated_at = ? WHERE id = ?",
            ("2026-11-15T00:00:00Z", session_a["id"]),
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET updated_at = ? WHERE id = ?",
            ("2026-11-15T00:00:01Z", session_b["id"]),
        )

    def normalize(*args, **kwargs):
        return SimpleNamespace(duration_seconds=1.0)

    monkeypatch.setattr(ingest, "normalize_extension_media", normalize)
    worker = ingest.ExtensionIngestWorker(settings)
    assert worker._pending_session() == session_b["id"]
    ingest._process_session(
        settings.data_dir,
        session_b["id"],
        settings=settings,
        stop_event=threading.Event(),
    )

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT receipt_at FROM extension_recording_sessions WHERE id = ?",
                (session_b["id"],),
            ).fetchone()[0]
            is not None
        )
        assert (
            connection.execute(
                "SELECT receipt_at FROM extension_recording_sessions WHERE id = ?",
                (session_a["id"],),
            ).fetchone()[0]
            is not None
        )


def _replay(settings, identity, session, content, operation):
    options = dict(identity=identity, backend_id="b" * 32, settings=settings)
    if operation == "create":
        return ingest.create_recording_session(
            settings.data_dir, payload=_create_payload(), **options
        )[0]
    if operation == "finalize":
        return ingest.finalize_recording_session(
            settings.data_dir,
            session_id=session["id"],
            payload=_manifest(content, epoch=1),
            **options,
        )
    return ingest.get_recording_session(settings.data_dir, session_id=session["id"], **options)


def _run(settings, session):
    ingest._process_session(
        settings.data_dir, session["id"], settings=settings, stop_event=threading.Event()
    )


@pytest.mark.parametrize("operation", ["finalize", "cancel", "chunk"])
def test_receipt_replay_and_rejection_do_not_wait_for_normalization(
    settings, monkeypatch, operation
):
    identity, session, content = _prepared(settings)
    entered, release = threading.Event(), threading.Event()

    def normalize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise MediaProcessingError("invalid_media")

    monkeypatch.setattr(ingest, "normalize_extension_media", normalize)
    worker = threading.Thread(target=_run, args=(settings, session))
    worker.start()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        assert entered.wait(5)
        receipt = _replay(settings, identity, session, content, "status")["receipt"]
        assert receipt is not None

        def request():
            if operation == "finalize":
                return _replay(settings, identity, session, content, operation)["receipt"]
            try:
                if operation == "cancel":
                    ingest.cancel_recording_session(
                        settings.data_dir,
                        identity=identity,
                        session_id=session["id"],
                        epoch=1,
                        backend_id="b" * 32,
                        settings=settings,
                    )
                else:
                    lease = ingest.begin_chunk(
                        settings.data_dir,
                        identity=identity,
                        session_id=session["id"],
                        sequence=0,
                        offset=0,
                        sha256=hashlib.sha256(content).hexdigest(),
                        epoch=1,
                        declared_length=len(content),
                        settings=settings,
                    )
                    lease.abort()
            except ingest.ExtensionIngestError as error:
                return error.status_code, error.code
            return None

        pending = executor.submit(request)
        try:
            result = pending.result(timeout=1)
        except TimeoutError:
            pytest.fail("A public request waited for paused normalization")
        assert result == (
            receipt
            if operation == "finalize"
            else (409, "too_late")
            if operation == "cancel"
            else (503, "session_busy")
        )
    finally:
        release.set()
        worker.join(5)
        executor.shutdown(wait=True)
    assert not worker.is_alive()


def test_cancel_during_archive_fences_receipt_and_late_state(settings, monkeypatch):
    identity, session, content = _prepared(settings)
    entered, release = threading.Event(), threading.Event()
    original = ingest._copy_archive

    def paused_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "_copy_archive", paused_copy)
    worker = threading.Thread(target=_run, args=(settings, session))
    worker.start()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        assert entered.wait(5)
        pending = executor.submit(
            ingest.cancel_recording_session,
            settings.data_dir,
            identity=identity,
            session_id=session["id"],
            epoch=1,
            backend_id="b" * 32,
            settings=settings,
        )
        try:
            canceled = pending.result(timeout=1)
        except TimeoutError:
            pytest.fail("Cancellation waited for archiving instead of fencing it")
        assert canceled["state"] == "canceled" and canceled["receipt"] is None
        ingest._set_session_state(settings.data_dir, session["id"], state="archiving")
    finally:
        release.set()
        worker.join(5)
        executor.shutdown(wait=True)
    current = _replay(settings, identity, session, content, "status")
    assert current["state"] == "canceled" and current["receipt"] is None
    assert not (settings.data_dir / "extension-raw" / f"{session['id']}.webm").exists()


@pytest.mark.parametrize("operation", ["create", "status", "finalize"])
@pytest.mark.parametrize("damage", ["missing", "same_size_corruption"])
@pytest.mark.parametrize("recover", [False, True])
def test_damaged_raw_never_authorizes_local_cleanup(settings, operation, damage, recover):
    identity, session, content = _prepared(settings)
    _run(settings, session)
    original = _replay(settings, identity, session, content, "status")["receipt"]
    assert original is not None
    raw = settings.data_dir / "extension-raw" / f"{session['id']}.webm"
    if damage == "missing":
        raw.unlink()
    else:
        raw.write_bytes(b"x" * len(content))
    if recover:
        ingest.recover_extension_ingest(settings.data_dir)
    with pytest.raises(ingest.ExtensionIngestError) as rejected:
        _replay(settings, identity, session, content, operation)
    assert rejected.value.code == "raw_corrupt"
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        stored = connection.execute(
            "SELECT archive_sha256, raw_bytes FROM extension_recording_sessions WHERE id = ?",
            (session["id"],),
        ).fetchone()
    assert stored == (original["archive_sha256"], original["total_bytes"])


@pytest.mark.parametrize("operation", ["create", "status", "finalize"])
def test_receipt_replay_hashes_without_database_writer_lock(settings, monkeypatch, operation):
    identity, session, content = _prepared(settings)
    _run(settings, session)
    original_hash = ingest._hash_file

    def hash_with_other_writer(*args, **kwargs):
        with sqlite3.connect(database_path(settings.data_dir), timeout=0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(ingest, "_hash_file", hash_with_other_writer)
    assert _replay(settings, identity, session, content, operation)["receipt"] is not None


def test_delete_marker_cancels_media_before_cleanup_waits(settings, monkeypatch):
    identity, session, content = _prepared(settings)
    entered, canceled = threading.Event(), threading.Event()

    def normalize(*args, should_cancel, **kwargs):
        entered.set()
        for _ in range(500):
            if should_cancel():
                canceled.set()
                raise MediaProcessingError("media_processing_cancelled")
            threading.Event().wait(0.01)
        pytest.fail("Media did not observe the deletion marker")

    monkeypatch.setattr(ingest, "normalize_extension_media", normalize)
    worker = threading.Thread(target=_run, args=(settings, session))
    worker.start()
    assert entered.wait(5)
    try:
        result = delete_meeting(settings.data_dir, session["meeting_url"].rsplit("/", 1)[-1])
        assert result["status"] == "deleted" and canceled.is_set()
    finally:
        worker.join(6)
    assert not worker.is_alive()
    assert not (settings.data_dir / "extension-raw" / f"{session['id']}.webm").exists()


def test_real_webm_receipt_media_retry_and_ready_library_share_one_meeting(settings, tmp_path):
    source = _webm(tmp_path)
    content = source.read_bytes()
    identity, session, _ = _prepared(settings, content)
    _run(settings, session)
    ready = _replay(settings, identity, session, content, "status")
    assert ready["state"] == "ready" and ready["recording"]["audio_url"]
    assert ready["recording"]["duration_seconds"] > 0
    assert ready["receipt"]["archive_sha256"] == hashlib.sha256(content).hexdigest()
    raw = settings.data_dir / "extension-raw" / f"{session['id']}.webm"
    assert raw.read_bytes() == content
    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE extension_recording_sessions SET state='failed' WHERE id=?", (session["id"],)
        )
    ingest.retry_recording_session(settings.data_dir, meeting_id)
    _run(settings, session)
    again = _replay(settings, identity, session, content, "status")
    assert again["state"] == "ready" and again["receipt"] == ready["receipt"]
    assert again["recording"]["id"] == ready["recording"]["id"]
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1


@pytest.mark.parametrize("epoch", [True, 1.0, "1", None])
def test_finalize_epoch_requires_integer_not_coercible_value(settings, epoch):
    identity, session, content = _prepared(settings)
    manifest = _manifest(content, epoch=1)
    manifest["epoch"] = epoch
    with pytest.raises(ingest.ExtensionIngestError) as rejected:
        ingest.finalize_recording_session(
            settings.data_dir,
            identity=identity,
            session_id=session["id"],
            payload=manifest,
            backend_id="b" * 32,
            settings=settings,
        )
    assert rejected.value.status_code == 400 and rejected.value.code == "invalid_epoch"


def test_corrupt_acknowledged_chunk_rejects_replay_and_recovery(settings):
    identity, session, content = _prepared(settings)
    chunk = settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"
    chunk.write_bytes(b"x" * len(content))
    with pytest.raises(ingest.ExtensionIngestError) as rejected:
        ingest.begin_chunk(
            settings.data_dir,
            identity=identity,
            session_id=session["id"],
            sequence=0,
            offset=0,
            sha256=hashlib.sha256(content).hexdigest(),
            epoch=1,
            declared_length=len(content),
            settings=settings,
        )
    assert rejected.value.code == "chunk_corrupt"
    ingest.recover_extension_ingest(settings.data_dir)
    current = _replay(settings, identity, session, content, "status")
    assert current["receipt"] is None and current["state"] == "failed"
    assert current["error"]["code"] == "chunk_corrupt"
    assert chunk.exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_uncommitted_chunk_is_never_acknowledged_after_crash_or_cancel(settings, cancel):
    initialize(settings.data_dir)
    identity = _identity(settings, "owner-partial-0001")
    session, _ = ingest.create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=_create_payload(),
        backend_id="b" * 32,
        settings=settings,
    )
    content = b"partial"
    lease = ingest.begin_chunk(
        settings.data_dir,
        identity=identity,
        session_id=session["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(content).hexdigest(),
        epoch=1,
        declared_length=len(content),
        settings=settings,
    )
    lease.write(content)
    if cancel:
        ingest.cancel_recording_session(
            settings.data_dir,
            identity=identity,
            session_id=session["id"],
            epoch=1,
            backend_id="b" * 32,
            settings=settings,
        )
        with pytest.raises(ingest.ExtensionIngestError):
            lease.commit()
    else:
        # Simulate a process ending after writing a temporary file but before ACK.
        lease.close()
    ingest.recover_extension_ingest(settings.data_dir)
    current = _replay(settings, identity, session, content, "status")
    assert current["receipt"] is None and current["next_offset"] == 0
    assert current["next_sequence"] == 0
    assert current["state"] == ("canceled" if cancel else "receiving")
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM extension_recording_chunks").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM extension_recording_reservations").fetchone()[
                0
            ]
            == 0
        )


def test_disk_exhaustion_creating_derivative_retains_receipt_and_worker_progress(
    settings, monkeypatch
):
    import errno

    identity, session, content = _prepared(settings)
    original = ingest.tempfile.mkstemp

    def exhaust_derivative_space(*args, **kwargs):
        if kwargs.get("dir") == settings.data_dir / "extension-derivatives" / ".tmp":
            raise OSError(errno.ENOSPC, "Synthetic disk exhaustion")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest.tempfile, "mkstemp", exhaust_derivative_space)
    _run(settings, session)
    status = _replay(settings, identity, session, content, "status")
    assert status["state"] == "failed" and status["error"]["code"] == "storage_failed"
    assert status["receipt"] is not None
    assert (settings.data_dir / "extension-raw" / f"{session['id']}.webm").read_bytes() == content


def test_waiting_media_worker_acquires_ownership_after_previous_owner_releases(
    settings, monkeypatch
):
    import os

    from asr_studio.service_runtime import acquire_service_lock

    _identity_value, session, _content = _prepared(settings)
    held = acquire_service_lock(settings.data_dir, "media", retry_seconds=0)
    assert held is not None
    processed = threading.Event()

    def process(data_dir, session_id, **kwargs):
        ingest._set_session_state(data_dir, session_id, state="failed", error_code="synthetic")
        processed.set()

    monkeypatch.setattr(ingest, "_process_session", process)
    worker = ingest.ExtensionIngestWorker(settings)
    worker.start()
    try:
        assert not processed.wait(0.5)
        os.close(held)
        held = None
        assert processed.wait(3), "A waiting application never retried the released ownership lock"
    finally:
        if held is not None:
            os.close(held)
        worker.stop()
