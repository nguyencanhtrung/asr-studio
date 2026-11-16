from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from asr_studio import extension_routes
from asr_studio.app import EXTENSION_ORIGIN, create_app
from asr_studio.auth import AuthStore
from asr_studio.extension_auth import ExtensionSessionIdentity
from asr_studio.extension_ingest import (
    ExtensionIngestError,
    begin_chunk,
    cancel_recording_session,
    create_recording_session,
    ensure_extension_storage,
    finalize_recording_session,
    publish_ready_derivative,
    recover_extension_ingest,
    retry_recording_session,
    session_lock,
)
from asr_studio.storage import database_path, initialize

pytestmark = pytest.mark.anyio


async def _operator_login(client: AsyncClient, settings) -> None:
    state = AuthStore(settings.auth_dir).load()
    response = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )
    assert response.status_code == 204


async def _extension_session(client: AsyncClient, settings) -> str:
    await _operator_login(client, settings)
    issued = await client.post(
        "/api/extension/pairing-codes",
        headers={"Origin": settings.public_url},
        json={"device_lifetime": "30d"},
    )
    assert issued.status_code == 200, issued.text
    paired = await client.post(
        "/api/extension/v1/pair",
        headers={"Origin": EXTENSION_ORIGIN},
        json={"code": issued.json()["code"], "device_id": "diagnostic-device"},
    )
    assert paired.status_code == 200, paired.text
    session = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {paired.json()['device_token']}",
        },
        json={},
    )
    assert session.status_code == 200, session.text
    return session.json()["session_token"]


def _create_payload() -> dict[str, object]:
    return {
        "client_recording_id": "client-recording-001",
        "capture_epoch": "capture-epoch-001",
        "mime_type": "audio/webm;codecs=opus",
        "title": "Extension capture",
        "metadata": {"channels": 1, "sample_rate": 48000, "tab_monitor": True},
    }


def _manifest(content: bytes, *, epoch: int) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    text = f"v1\n0:0:{len(content)}:{digest}\n".encode()
    return {
        "finalize_id": "finalize-001",
        "capture_epoch": "capture-epoch-001",
        "epoch": epoch,
        "capture_complete": True,
        "total_bytes": len(content),
        "chunk_count": 1,
        "manifest_hash": hashlib.sha256(text).hexdigest(),
        "chunks": [{"sequence": 0, "offset": 0, "length": len(content), "sha256": digest}],
    }


async def test_authenticated_chunk_replay_and_pending_library_guard(client, settings):
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=_create_payload()
    )
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["state"] == "receiving"
    assert session["recording"]["id"] != session["id"]
    assert session["recording"]["duration_seconds"] is None
    assert session["recording"]["audio_url"] is None
    assert session["limits"]["max_chunk_bytes"] > 0
    assert session["meeting_url"].startswith(settings.public_url)

    content = b"not-a-complete-webm"
    digest = hashlib.sha256(content).hexdigest()
    chunk_headers = {
        **auth,
        "X-Chunk-Offset": "0",
        "X-Chunk-SHA256": digest,
        "X-Writer-Epoch": "1",
    }
    bad_chunk = await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0",
        headers={**chunk_headers, "X-Chunk-SHA256": hashlib.sha256(b"wrong").hexdigest()},
        content=content,
    )
    assert bad_chunk.status_code == 400
    assert bad_chunk.json()["error"]["code"] == "chunk_hash_mismatch"
    chunk = await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0",
        headers=chunk_headers,
        content=content,
    )
    assert chunk.status_code == 200, chunk.text
    assert chunk.json()["next_offset"] == len(content)

    replay = await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0",
        headers=chunk_headers,
        content=content,
    )
    assert replay.status_code == 200
    assert replay.json() == chunk.json()

    wrong_epoch = await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/1",
        headers={**chunk_headers, "X-Chunk-Offset": str(len(content)), "X-Writer-Epoch": "2"},
        content=b"next",
    )
    assert wrong_epoch.status_code == 409
    assert wrong_epoch.json()["error"]["code"] == "writer_epoch_conflict"

    library = await client.get("/api/meetings")
    assert library.status_code == 200
    assert library.json()["meetings"][0]["recording_state"] == "receiving"
    audio = await client.get(f"/api/meetings/{session['meeting_url'].rsplit('/', 1)[-1]}/audio")
    assert audio.status_code == 409
    assert audio.json()["detail"]["code"] == "recording_not_ready"


async def test_symlinked_data_root_supports_lifespan_and_ingest_round_trip(
    settings, tmp_path, monkeypatch
):
    real_data_dir = tmp_path / "physical-data"
    real_data_dir.mkdir()
    configured_data_dir = tmp_path / "configured-data"
    configured_data_dir.symlink_to(real_data_dir, target_is_directory=True)
    symlinked_settings = replace(settings, data_dir=configured_data_dir)

    def fake_normalize(_source, output, **_kwargs):
        Path(output).write_bytes(b"normalized-through-symlink")
        return SimpleNamespace(duration_seconds=0.25)

    monkeypatch.setattr("asr_studio.extension_ingest.normalize_extension_media", fake_normalize)
    app = create_app(symlinked_settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=symlinked_settings.public_url
        ) as client,
    ):
        token = await _extension_session(client, symlinked_settings)
        auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
        content = b"symlinked-root-round-trip"
        created = await client.post(
            "/api/extension/v1/recording-sessions",
            headers=auth,
            json=_create_payload(),
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        digest = hashlib.sha256(content).hexdigest()
        chunk = await client.put(
            f"/api/extension/v1/recording-sessions/{session_id}/chunks/0",
            headers={
                **auth,
                "X-Chunk-Offset": "0",
                "X-Chunk-SHA256": digest,
                "X-Writer-Epoch": "1",
            },
            content=content,
        )
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["next_offset"] == len(content)

        finalized = await client.post(
            f"/api/extension/v1/recording-sessions/{session_id}/finalize",
            headers=auth,
            json=_manifest(content, epoch=1),
        )
        assert finalized.status_code == 202, finalized.text

        status = None
        for _ in range(200):
            status = await client.get(
                f"/api/extension/v1/recording-sessions/{session_id}", headers=auth
            )
            if status.json()["state"] == "ready":
                break
            await asyncio.sleep(0.01)
        assert status is not None
        assert status.json()["state"] == "ready", status.text

    assert (real_data_dir / "extension-raw" / f"{session_id}.webm").read_bytes() == content
    assert (
        real_data_dir / "extension-derivatives" / f"{session_id}.m4a"
    ).read_bytes() == b"normalized-through-symlink"


def test_managed_child_symlink_is_rejected_under_configured_root(tmp_path):
    real_data_dir = tmp_path / "physical-data"
    real_data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (real_data_dir / "extension-staging").symlink_to(outside, target_is_directory=True)
    configured_data_dir = tmp_path / "configured-data"
    configured_data_dir.symlink_to(real_data_dir, target_is_directory=True)

    with pytest.raises(ExtensionIngestError) as rejected:
        ensure_extension_storage(configured_data_dir)

    assert rejected.value.code == "storage_unavailable"
    assert not (outside / "chunks").exists()


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("deadline", ["idle", "total"])
async def test_stalled_chunk_body_releases_lease_and_preserves_replay(
    client, settings, monkeypatch, replay, deadline
):
    monkeypatch.setattr(extension_routes, "EXTENSION_CHUNK_BODY_IDLE_SECONDS", 0.05)
    monkeypatch.setattr(extension_routes, "EXTENSION_CHUNK_BODY_TOTAL_SECONDS", 0.2)
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=_create_payload()
    )
    assert created.status_code == 201, created.text
    session = created.json()
    content = b"stalled-body-content"
    digest = hashlib.sha256(content).hexdigest()
    chunk_url = f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0"
    chunk_headers = {
        **auth,
        "X-Chunk-Offset": "0",
        "X-Chunk-SHA256": digest,
        "X-Writer-Epoch": "1",
    }

    if replay:
        uploaded = await client.put(chunk_url, headers=chunk_headers, content=content)
        assert uploaded.status_code == 200, uploaded.text

    async def stalled_body():
        yield content[:4]
        if deadline == "idle":
            await asyncio.sleep(1)
            return
        for _ in range(len(content) - 4):
            await asyncio.sleep(0.02)
            yield b"x"

    timed_out = await asyncio.wait_for(
        client.put(chunk_url, headers=chunk_headers, content=stalled_body()), timeout=2
    )
    assert timed_out.status_code == 408, timed_out.text
    assert timed_out.json()["error"]["code"] == "chunk_timeout"

    chunk_path = settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_recording_reservations WHERE session_id = ?",
                (session["id"],),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM extension_recording_chunks WHERE session_id = ?",
            (session["id"],),
        ).fetchone()[0] == (1 if replay else 0)
    if replay:
        assert chunk_path.read_bytes() == content
    else:
        assert not chunk_path.exists()

    retried = await client.put(chunk_url, headers=chunk_headers, content=content)
    assert retried.status_code == 200, retried.text
    assert retried.json()["next_offset"] == len(content)

    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    deleted = await asyncio.wait_for(
        client.delete(f"/api/meetings/{meeting_id}", headers={"Origin": settings.public_url}),
        timeout=2,
    )
    assert deleted.status_code == 200, deleted.text


async def test_oversized_replay_body_releases_lease_without_erasing_chunk(client, settings):
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=_create_payload()
    )
    assert created.status_code == 201, created.text
    session = created.json()
    content = b"committed-replay"
    chunk_url = f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0"
    chunk_headers = {
        **auth,
        "X-Chunk-Offset": "0",
        "X-Chunk-SHA256": hashlib.sha256(content).hexdigest(),
        "X-Writer-Epoch": "1",
    }
    uploaded = await client.put(chunk_url, headers=chunk_headers, content=content)
    assert uploaded.status_code == 200, uploaded.text

    async def oversized_body():
        yield content
        yield b"!"

    rejected = await asyncio.wait_for(
        client.put(chunk_url, headers=chunk_headers, content=oversized_body()), timeout=2
    )
    assert rejected.status_code == 413, rejected.text
    assert rejected.json()["error"]["code"] == "chunk_too_large"
    chunk_path = settings.data_dir / "extension-staging" / "chunks" / session["id"] / "0.chunk"
    assert chunk_path.read_bytes() == content

    replay = await client.put(chunk_url, headers=chunk_headers, content=content)
    assert replay.status_code == 200, replay.text
    assert replay.json() == uploaded.json()


async def test_finalize_receipt_precedes_media_failure_and_delete_tombstones(
    client, settings, monkeypatch
):
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    payload = _create_payload()
    created = await client.post("/api/extension/v1/recording-sessions", headers=auth, json=payload)
    session = created.json()
    content = b"raw-exact-bytes"
    digest = hashlib.sha256(content).hexdigest()
    await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0",
        headers={
            **auth,
            "X-Chunk-Offset": "0",
            "X-Chunk-SHA256": digest,
            "X-Writer-Epoch": "1",
        },
        content=content,
    )

    from asr_studio import extension_ingest
    from asr_studio.media import MediaProcessingError

    def fail_media(*args, **kwargs):
        raise MediaProcessingError("invalid_media")

    monkeypatch.setattr(extension_ingest, "normalize_extension_media", fail_media)
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_manifest(content, epoch=1),
    )
    assert finalized.status_code == 202

    status = None
    for _ in range(100):
        status = await client.get(
            f"/api/extension/v1/recording-sessions/{session['id']}", headers=auth
        )
        if status.json().get("receipt") is not None and status.json()["state"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert status is not None and status.json()["receipt"] is not None
    assert status.json()["state"] == "failed"
    receipt = status.json()["receipt"]
    assert receipt["recording_id"] == session["id"]
    assert status.json()["recording"]["id"] != receipt["recording_id"]
    raw_path = settings.data_dir / "extension-raw" / f"{session['id']}.webm"
    assert raw_path.read_bytes() == content

    finalize_replay = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_manifest(content, epoch=1),
    )
    assert finalize_replay.status_code == 202
    assert finalize_replay.json()["state"] == "failed"
    assert finalize_replay.json()["receipt"] == receipt

    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    bearer_attempt = await client.post(
        f"/api/meetings/{meeting_id}/recording/retry",
        headers={"Origin": settings.public_url, "Authorization": f"Bearer {token}"},
    )
    assert bearer_attempt.status_code == 401
    await _operator_login(client, settings)
    app = client._transport.app
    enqueued: list[str] = []
    monkeypatch.setattr(app.state.extension_ingest_worker, "enqueue", enqueued.append)
    retried = await client.post(
        f"/api/meetings/{meeting_id}/recording/retry",
        headers={"Origin": settings.public_url},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json() == {
        "meeting_id": meeting_id,
        "recording_state": "finalizing",
        "retry_requested": True,
    }
    assert enqueued == [session["id"]]
    assert (
        await client.get(f"/api/extension/v1/recording-sessions/{session['id']}", headers=auth)
    ).json()["receipt"] == receipt

    deleted = await client.delete(
        f"/api/meetings/{meeting_id}", headers={"Origin": settings.public_url}
    )
    assert deleted.status_code == 200, deleted.text
    old_status = await client.get(
        f"/api/extension/v1/recording-sessions/{session['id']}", headers=auth
    )
    assert old_status.status_code == 410
    replay_create = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=payload
    )
    assert replay_create.status_code == 410
    assert not raw_path.exists()

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute(
            "SELECT state FROM extension_recording_tombstones WHERE session_id = ?",
            (session["id"],),
        ).fetchone() == ("deleted",)


async def test_extension_capabilities_and_cors_only_add_chunk_put_contract(client, settings):
    capabilities = await client.get(
        "/api/extension/v1/capabilities", headers={"Origin": EXTENSION_ORIGIN}
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["web_url"] == settings.public_url
    limits = capabilities.json()["limits"]
    assert limits["max_active_sessions"] > 0
    assert limits["max_global_staging_bytes"] > 0
    assert limits["max_finalize_body_bytes"] > 16 * 1024

    allowed = await client.options(
        "/api/extension/v1/recording-sessions/x/chunks/0",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": (
                "Authorization, Content-Type, X-Chunk-Offset, X-Chunk-SHA256, X-Writer-Epoch"
            ),
        },
    )
    assert allowed.status_code == 204
    assert "PUT" in allowed.headers["access-control-allow-methods"]

    unrelated_method = await client.options(
        "/api/extension/v1/recording-sessions/x/chunks/0",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    unrelated_header = await client.options(
        "/api/extension/v1/recording-sessions/x/chunks/0",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-Not-Allowed",
        },
    )
    assert unrelated_method.status_code == 403
    assert unrelated_header.status_code == 403


async def test_create_language_omission_is_immutable_and_type_checked(client, settings):
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    payload = _create_payload()
    created = await client.post("/api/extension/v1/recording-sessions", headers=auth, json=payload)
    assert created.status_code == 201, created.text
    replay = await client.post("/api/extension/v1/recording-sessions", headers=auth, json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]

    for language_hint in ("en", "vi"):
        changed = await client.post(
            "/api/extension/v1/recording-sessions",
            headers=auth,
            json={**payload, "language_hint": language_hint},
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "idempotency_conflict"

    for invalid_language in ([], {}):
        invalid = await client.post(
            "/api/extension/v1/recording-sessions",
            headers=auth,
            json={
                **payload,
                "client_recording_id": f"invalid-{len(invalid_language)}",
                "language_hint": invalid_language,
            },
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_language_hint"


def _identity(settings, owner_id: str) -> ExtensionSessionIdentity:
    return ExtensionSessionIdentity(
        device_record_id=f"device-{owner_id}",
        owner_id=owner_id,
        device_id=f"diagnostic-{owner_id}",
        server_url=settings.public_url,
        generation=1,
        scope="ingest",
    )


def _direct_payload(client_id: str) -> dict[str, object]:
    return {
        "client_recording_id": client_id,
        "capture_epoch": f"capture-{client_id}",
        "mime_type": "audio/webm;codecs=opus",
        "title": "Direct extension capture",
        "metadata": {},
    }


def test_global_staging_quota_counts_reservations_and_retained_canceled_chunks(settings):
    quota_settings = replace(settings, extension_max_staging_bytes=4)
    initialize(quota_settings.data_dir)
    owner_a = _identity(quota_settings, "owner-a-0000000001")
    owner_b = _identity(quota_settings, "owner-b-0000000001")
    session_a, status = create_recording_session(
        quota_settings.data_dir,
        identity=owner_a,
        payload=_direct_payload("a"),
        backend_id="a" * 32,
        settings=quota_settings,
    )
    assert status == 201
    session_b, status = create_recording_session(
        quota_settings.data_dir,
        identity=owner_b,
        payload=_direct_payload("b"),
        backend_id="b" * 32,
        settings=quota_settings,
    )
    assert status == 201

    lease = begin_chunk(
        quota_settings.data_dir,
        identity=owner_a,
        session_id=session_a["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(b"four").hexdigest(),
        epoch=1,
        declared_length=4,
        settings=quota_settings,
    )
    with pytest.raises(ExtensionIngestError, match="local byte limit") as reservation_error:
        begin_chunk(
            quota_settings.data_dir,
            identity=owner_b,
            session_id=session_b["id"],
            sequence=0,
            offset=0,
            sha256=hashlib.sha256(b"x").hexdigest(),
            epoch=1,
            declared_length=1,
            settings=quota_settings,
        )
    assert reservation_error.value.code == "local_byte_limit"
    lease.abort()

    lease = begin_chunk(
        quota_settings.data_dir,
        identity=owner_a,
        session_id=session_a["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(b"four").hexdigest(),
        epoch=1,
        declared_length=4,
        settings=quota_settings,
    )
    lease.write(b"four")
    ack = lease.commit()
    replay = begin_chunk(
        quota_settings.data_dir,
        identity=owner_a,
        session_id=session_a["id"],
        sequence=0,
        offset=0,
        sha256=hashlib.sha256(b"four").hexdigest(),
        epoch=1,
        declared_length=4,
        settings=quota_settings,
    )
    assert replay.commit() == ack

    cancel_recording_session(
        quota_settings.data_dir,
        identity=owner_a,
        session_id=session_a["id"],
        epoch=1,
        backend_id="a" * 32,
        settings=quota_settings,
    )
    with pytest.raises(ExtensionIngestError) as retained_error:
        begin_chunk(
            quota_settings.data_dir,
            identity=owner_b,
            session_id=session_b["id"],
            sequence=0,
            offset=0,
            sha256=hashlib.sha256(b"x").hexdigest(),
            epoch=1,
            declared_length=1,
            settings=quota_settings,
        )
    assert retained_error.value.code == "local_byte_limit"


def test_active_session_limit_is_global(settings):
    quota_settings = replace(settings, extension_max_active_sessions=1)
    initialize(quota_settings.data_dir)
    owner_a = _identity(quota_settings, "owner-a-0000000002")
    owner_b = _identity(quota_settings, "owner-b-0000000002")
    create_recording_session(
        quota_settings.data_dir,
        identity=owner_a,
        payload=_direct_payload("a"),
        backend_id="a" * 32,
        settings=quota_settings,
    )
    with pytest.raises(ExtensionIngestError) as error:
        create_recording_session(
            quota_settings.data_dir,
            identity=owner_b,
            payload=_direct_payload("b"),
            backend_id="b" * 32,
            settings=quota_settings,
        )
    assert error.value.code == "session_limit"


def test_recovery_waits_for_publication_lock_and_rereads_durable_receipt(settings, monkeypatch):
    initialize(settings.data_dir)
    identity = _identity(settings, "owner-recovery-0001")
    session, status = create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=_direct_payload("recovery"),
        backend_id="r" * 32,
        settings=settings,
    )
    assert status == 201
    session_id = session["id"]
    raw_path = settings.data_dir / "extension-raw" / f"{session_id}.webm"
    temporary_path = settings.data_dir / "extension-raw" / ".tmp" / f".{session_id}-publish.webm"
    attempted = threading.Event()
    done = threading.Event()
    errors: list[BaseException] = []

    import asr_studio.extension_ingest as ingest

    original_open = ingest._open_session_lock

    def observed_open(data_dir: Path, current_session_id: str) -> int:
        if current_session_id == session_id:
            attempted.set()
        return original_open(data_dir, current_session_id)

    monkeypatch.setattr(ingest, "_open_session_lock", observed_open)

    def recover() -> None:
        try:
            recover_extension_ingest(settings.data_dir)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    with session_lock(settings.data_dir, session_id):
        worker = threading.Thread(target=recover)
        worker.start()
        assert attempted.wait(1)
        assert not done.wait(0.05)
        temporary_path.write_bytes(b"published-before-db-commit")
        temporary_path.replace(raw_path)
        with sqlite3.connect(database_path(settings.data_dir)) as connection:
            connection.execute(
                "UPDATE extension_recording_sessions SET state = 'archived', raw_key = ?, "
                "raw_bytes = ?, archive_sha256 = ?, receipt_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    f"extension-raw/{session_id}.webm",
                    len(b"published-before-db-commit"),
                    hashlib.sha256(b"published-before-db-commit").hexdigest(),
                    session_id,
                ),
            )
        assert raw_path.exists()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert raw_path.read_bytes() == b"published-before-db-commit"


def test_ready_publication_helper_is_caller_transactional(settings):
    initialize(settings.data_dir)
    identity = _identity(settings, "owner-publication-0001")
    session, status = create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=_direct_payload("publication"),
        backend_id="p" * 32,
        settings=settings,
    )
    assert status == 201
    session_id = session["id"]
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'archived' WHERE id = ?",
            (session_id,),
        )

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"extension-derivatives/{session_id}.m4a",
            duration_seconds=1.25,
        )
        assert (
            connection.execute(
                "SELECT state FROM extension_recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            == "ready"
        )
        connection.rollback()

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT state FROM extension_recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            == "archived"
        )
        assert (
            connection.execute(
                "SELECT storage_key FROM recordings WHERE extension_session_id = ?", (session_id,)
            ).fetchone()[0]
            is None
        )

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"extension-derivatives/{session_id}.m4a",
            duration_seconds=1.25,
        )
        connection.commit()
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert (
            connection.execute(
                "SELECT state FROM extension_recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            == "ready"
        )


def test_operator_retry_rejects_incomplete_raw_archive_and_foreign_meeting(settings):
    initialize(settings.data_dir)
    identity = _identity(settings, "owner-retry-0001")
    session, status = create_recording_session(
        settings.data_dir,
        identity=identity,
        payload=_direct_payload("retry"),
        backend_id="q" * 32,
        settings=settings,
    )
    assert status == 201
    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'failed', raw_key = ?, "
            "raw_bytes = 4, archive_sha256 = ?, receipt_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                f"extension-raw/{session['id']}.webm",
                hashlib.sha256(b"raw!").hexdigest(),
                session["id"],
            ),
        )
    with pytest.raises(ExtensionIngestError) as incomplete:
        retry_recording_session(settings.data_dir, meeting_id)
    assert incomplete.value.code == "recording_retry_unavailable"
    with pytest.raises(ExtensionIngestError) as foreign:
        retry_recording_session(settings.data_dir, "foreign-meeting")
    assert foreign.value.code == "meeting_not_found"


@pytest.mark.parametrize("operation", ["chunk", "finalize", "cancel"])
@pytest.mark.parametrize("target", ["unknown", "foreign"])
def test_unauthorized_recording_ids_do_not_allocate_lock_files(settings, operation, target):
    initialize(settings.data_dir)
    owner = _identity(settings, "owner-lock-budget-0001")
    outsider = _identity(settings, "owner-lock-budget-0002")
    session, _ = create_recording_session(
        settings.data_dir,
        identity=owner,
        payload=_direct_payload("lock-budget"),
        backend_id="b" * 32,
        settings=settings,
    )
    session_id = session["id"] if target == "foreign" else "f" * 32
    locks = settings.data_dir / "extension-locks"
    before = set(locks.iterdir())
    with pytest.raises(ExtensionIngestError) as rejected:
        if operation == "chunk":
            begin_chunk(
                settings.data_dir,
                identity=outsider,
                session_id=session_id,
                sequence=0,
                offset=0,
                sha256=hashlib.sha256(b"x").hexdigest(),
                epoch=1,
                declared_length=1,
                settings=settings,
            )
        elif operation == "finalize":
            finalize_recording_session(
                settings.data_dir,
                identity=outsider,
                session_id=session_id,
                payload=_manifest(b"x", epoch=1),
                backend_id="b" * 32,
                settings=settings,
            )
        else:
            cancel_recording_session(
                settings.data_dir,
                identity=outsider,
                session_id=session_id,
                epoch=1,
                backend_id="b" * 32,
                settings=settings,
            )
    assert rejected.value.code == "recording_not_found"
    assert set(locks.iterdir()) == before


@pytest.mark.parametrize("state", ["failed", "archived", "finalizing"])
def test_retry_archive_verification_allows_other_database_writers(settings, monkeypatch, state):
    from asr_studio import extension_ingest

    initialize(settings.data_dir)
    session, _ = create_recording_session(
        settings.data_dir,
        identity=_identity(settings, "owner-retry-write-lock"),
        payload=_direct_payload("retry-write-lock"),
        backend_id="b" * 32,
        settings=settings,
    )
    content = b"complete archived source"
    raw_key = f"extension-raw/{session['id']}.webm"
    (settings.data_dir / raw_key).write_bytes(content)
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE extension_recording_sessions SET state = ?, raw_key = ?, raw_bytes = ?, "
            "archive_sha256 = ?, receipt_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state, raw_key, len(content), hashlib.sha256(content).hexdigest(), session["id"]),
        )
    original_hash = extension_ingest._hash_file

    def hash_with_concurrent_writer(*args, **kwargs):
        # Large audio verification must not monopolize SQLite's writer slot.
        with sqlite3.connect(database_path(settings.data_dir), timeout=0) as other_writer:
            other_writer.execute("BEGIN IMMEDIATE")
            other_writer.rollback()
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(extension_ingest, "_hash_file", hash_with_concurrent_writer)
    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    enqueued = []
    result = retry_recording_session(settings.data_dir, meeting_id, enqueue=enqueued.append)
    assert result["recording_state"] == ("finalizing" if state == "failed" else state)
    assert enqueued == [session["id"]]


def test_ready_source_check_and_session_trigger_require_derivative(settings):
    initialize(settings.data_dir)
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        recording_columns = {row[1] for row in connection.execute("PRAGMA table_info(recordings)")}
        assert not recording_columns.intersection(
            {"state", "artifact_error", "artifact_error_code", "artifact_error_message"}
        )
        session_id = "a" * 32
        meeting_id = "m" * 32
        connection.execute(
            "INSERT INTO meetings(id, title, meeting_date, language_hint) VALUES (?, ?, ?, ?)",
            (meeting_id, "Pending", "2026-11-15", "vi"),
        )
        connection.execute(
            "INSERT INTO extension_recording_sessions("
            "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, "
            "language_hint, language_hint_omitted, mime_type, state, epoch, next_sequence, "
            "next_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'receiving', 1, 0, 0)",
            (
                session_id,
                "b" * 16,
                "client",
                "capture",
                "c" * 64,
                "vi",
                1,
                "audio/webm;codecs=opus",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO extension_recording_sessions("
                "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, "
                "language_hint, language_hint_omitted, mime_type, state, epoch, "
                "next_sequence, next_offset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', 1, 0, 0)",
                (
                    "d" * 32,
                    "e" * 16,
                    "ready-client",
                    "ready-capture",
                    "f" * 64,
                    "vi",
                    1,
                    "audio/webm;codecs=opus",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE extension_recording_sessions SET state = 'ready' WHERE id = ?",
                (session_id,),
            )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, extension_session_id, storage_key, "
            "content_type, duration_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "r" * 32,
                meeting_id,
                session_id,
                f"extension-derivatives/{session_id}.m4a",
                "audio/mp4",
                1.0,
            ),
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'ready' WHERE id = ?",
            (session_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE recordings SET duration_seconds = NULL WHERE id = ?",
                ("r" * 32,),
            )
        connection.execute(
            "INSERT INTO meetings(id, title, meeting_date, language_hint) VALUES (?, ?, ?, ?)",
            ("legacy-meeting", "Legacy", "2026-11-15", "vi"),
        )
        connection.execute(
            "INSERT INTO uploads(id, filename, declared_size, status, raw_key, managed_key) "
            "VALUES ('u', 'legacy.wav', 1, 'consumed', 'uploads/u.part', 'recordings/u.m4a')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO recordings(id, meeting_id, upload_id, storage_key, content_type, "
                "duration_seconds) "
                "VALUES ('legacy', 'legacy-meeting', 'u', 'recordings/u.m4a', 'audio/mp4', NULL)"
            )


async def test_large_finalize_manifest_reaches_durable_chunk_validation(client, settings):
    token = await _extension_session(client, settings)
    auth = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=_create_payload()
    )
    assert created.status_code == 201
    count = 14400
    digest = hashlib.sha256(b"x").hexdigest()
    manifest = _manifest(b"x", epoch=1)
    manifest.update(
        total_bytes=count,
        chunk_count=count,
        chunks=[{"sequence": i, "offset": i, "length": 1, "sha256": digest} for i in range(count)],
        manifest_hash=hashlib.sha256(
            ("v1\n" + "".join(f"{i}:{i}:1:{digest}\n" for i in range(count))).encode()
        ).hexdigest(),
    )
    assert len(json.dumps(manifest)) > 1024 * 1024
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{created.json()['id']}/finalize",
        headers=auth,
        json=manifest,
    )
    # Metadata-sized stress case only: missing audio must still prevent a receipt.
    assert finalized.status_code == 409, finalized.text
    assert finalized.json()["error"]["code"] == "manifest_conflict"
    assert "durable chunk metadata" in finalized.json()["error"]["message"]
