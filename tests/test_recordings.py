from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
import time
import wave

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from media_tools import require_media_tools
from starlette.requests import ClientDisconnect, Request

from asr_studio import recordings as recordings_module
from asr_studio import routes as routes_module
from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.media import MediaProcessingError
from asr_studio.storage import initialize

pytestmark = pytest.mark.anyio


def _wav_bytes(duration_seconds: float = 0.5) -> bytes:
    frame_rate = 16000
    frame_count = int(frame_rate * duration_seconds)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(frame_rate)
        output.writeframes(b"\0\0" * frame_count)
    return audio.getvalue()


async def _authorize(client: AsyncClient, settings: Settings) -> None:
    state = AuthStore(settings.auth_dir).load()
    response = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )
    assert response.status_code == 204


async def _upload_ready(client: AsyncClient, settings: Settings, content: bytes) -> dict:
    _require_media_tools()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "sample.wav", "size_bytes": len(content)},
    )
    assert created.status_code == 201, created.text
    upload = created.json()
    received = await client.put(
        upload["content_url"],
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert received.status_code == 200, received.text
    assert received.json()["status"] == "uploaded"
    complete = await client.post(
        upload["complete_url"],
        headers={"Origin": settings.public_url},
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "ready"
    return upload


_require_media_tools = require_media_tools


async def test_wav_upload_normalizes_attaches_idempotently_and_keeps_date_empty(client, settings):
    await _authorize(client, settings)
    policy = await client.get("/api/uploads/policy")
    assert policy.status_code == 200
    assert policy.json()["extensions"] == [
        "wav",
        "mp3",
        "m4a",
        "mp4",
        "mov",
        "webm",
        "ogg",
        "opus",
        "flac",
    ]
    assert policy.json()["max_size_bytes"] == 4 * 1024**3
    assert policy.json()["max_duration_seconds"] == 4 * 60 * 60
    assert policy.json()["chunk_bytes"] == 64 * 1024**2
    assert policy.json()["output"] == {
        "container": "m4a",
        "audio_codec": "aac",
        "bitrate_kbps": 160,
        "faststart": True,
    }

    source = _wav_bytes()
    upload = await _upload_ready(client, settings, source)
    raw_path = settings.data_dir / "uploads" / f"{upload['id']}.part"
    managed_path = settings.data_dir / "recordings" / f"{upload['id']}.m4a"
    assert not raw_path.exists()
    assert managed_path.is_file()
    assert managed_path.stat().st_mode & 0o777 == 0o600

    attached = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url},
        json={"upload_id": upload["id"], "title": "  Planning  ", "language_hint": "en"},
    )
    assert attached.status_code == 201, attached.text
    meeting = attached.json()
    assert meeting["title"] == "Planning"
    assert meeting["meeting_date"] is None
    assert meeting["language_hint"] == "en"
    assert meeting["metadata_version"] == 1
    assert meeting["recording"]["content_type"] == "audio/mp4"
    assert meeting["recording"]["audio_url"] == f"/api/meetings/{meeting['id']}/audio"

    retry = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url},
        json={"upload_id": upload["id"], "title": "Planning", "language_hint": "en"},
    )
    assert retry.status_code == 201
    assert retry.json() == meeting
    conflict = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url},
        json={"upload_id": upload["id"], "title": "Different", "language_hint": "en"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    upload_state = await client.get(f"/api/uploads/{upload['id']}")
    assert upload_state.json()["status"] == "consumed"
    assert upload_state.json()["meeting_url"] == f"/meetings/{meeting['id']}"
    assert str(settings.data_dir) not in json.dumps(meeting)
    assert str(settings.data_dir) not in json.dumps(upload_state.json())


async def test_corrupt_and_unsupported_uploads_fail_with_safe_codes_and_cleanup(client, settings):
    _require_media_tools()
    await _authorize(client, settings)
    unsupported = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "sample.exe", "size_bytes": 8},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["code"] == "unsupported_extension"
    missing_dot = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "wav", "size_bytes": 8},
    )
    assert missing_dot.status_code == 400
    assert missing_dot.json()["detail"]["code"] == "invalid_filename"

    content = b"not an audio file"
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "corrupt.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    received = await client.put(
        f"/api/uploads/{upload_id}/content",
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert received.status_code == 200
    failed = await client.post(
        f"/api/uploads/{upload_id}/complete",
        headers={"Origin": settings.public_url},
    )
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "invalid_media"
    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "failed"
    assert state.json()["failure_code"] == "invalid_media"
    assert state.json()["filename"] == "removed-upload"
    assert not (settings.data_dir / "uploads" / f"{upload_id}.part").exists()
    assert not (settings.data_dir / "recordings" / f"{upload_id}.m4a").exists()
    assert str(settings.data_dir) not in failed.text


async def test_oversize_and_byte_mismatch_fail_before_media_processing(client, settings):
    await _authorize(client, settings)
    oversize = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "large.wav", "size_bytes": 4 * 1024**3 + 1},
    )
    assert oversize.status_code == 413
    assert oversize.json()["detail"]["code"] == "upload_too_large"

    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "short.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    mismatch = await client.put(
        f"/api/uploads/{upload_id}/content",
        headers={"Origin": settings.public_url},
        content=content + b"x",
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "size_mismatch"
    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "failed"
    assert state.json()["failure_code"] == "size_mismatch"
    assert state.json()["filename"] == "removed-upload"
    assert not (settings.data_dir / "uploads" / f"{upload_id}.part").exists()


async def test_interrupted_upload_stream_keeps_the_last_committed_chunk(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "disconnect.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]

    async def interrupted_stream():
        yield content[:32]
        raise httpx.ReadError("simulated disconnect")

    interrupted = await client.put(
        f"/api/uploads/{upload_id}/content",
        headers={"Origin": settings.public_url},
        content=interrupted_stream(),
    )
    assert interrupted.status_code == 400
    assert interrupted.json()["detail"]["code"] == "upload_interrupted"
    assert interrupted.json()["detail"]["received_bytes"] == 0

    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "receiving"
    assert state.json()["received_bytes"] == 0
    raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
    assert raw_path.read_bytes() == b""
    resumed = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "uploaded"


async def test_client_disconnect_is_propagated_after_upload_release(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "disconnect.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]

    async def disconnected_stream():
        yield content[:32]
        raise ClientDisconnect

    disconnected = await client.put(
        f"/api/uploads/{upload_id}/content",
        headers={"Origin": settings.public_url},
        content=disconnected_stream(),
    )
    assert disconnected.status_code == 204

    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "receiving"
    assert state.json()["received_bytes"] == 0
    resumed = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "uploaded"


async def test_repeated_cancel_during_cleanup_preserves_committed_bytes(settings, monkeypatch):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        upload = recordings_module.create_upload(
            settings.data_dir,
            filename="cancel-at-commit.wav",
            size_bytes=8,
            max_upload_bytes=settings.max_upload_bytes,
        )
        upload_id = upload["id"]
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/api/uploads/{upload_id}/content"
        )
        real_finish = routes_module.finish_upload
        real_get = routes_module.get_upload_received_bytes
        get_entered = threading.Event()
        get_release = threading.Event()

        def slow_get(*args, **kwargs):
            get_entered.set()
            assert get_release.wait(5)
            return real_get(*args, **kwargs)

        def commit_then_disconnect(*args, **kwargs):
            real_finish(*args, **kwargs)
            raise ClientDisconnect

        monkeypatch.setattr(routes_module, "get_upload_received_bytes", slow_get)
        monkeypatch.setattr(routes_module, "finish_upload", commit_then_disconnect)

        async def receive():
            return {"type": "http.request", "body": b"abcd", "more_body": False}

        first = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    receive,
                ),
                offset=0,
            )
        )
        for _ in range(200):
            if get_entered.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cleanup did not read the durable offset")
        first.cancel()
        first.cancel()
        get_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)

        raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
        assert recordings_module.get_upload(settings.data_dir, upload_id)["received_bytes"] == 4
        assert raw_path.read_bytes() == b"abcd"
        monkeypatch.setattr(routes_module, "finish_upload", real_finish)

        async def receive_resume():
            return {"type": "http.request", "body": b"efgh", "more_body": False}

        resumed_request = Request(
            {"type": "http", "method": "PUT", "path": "/probe", "headers": [], "query_string": b""},
            receive_resume,
        )
        resumed = await endpoint(upload_id, resumed_request, offset=4)
        assert resumed["status"] == "uploaded"
        assert raw_path.read_bytes() == b"abcdefgh"


async def test_cancel_during_writer_acquisition_releases_the_guard(settings, monkeypatch):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        upload = recordings_module.create_upload(
            settings.data_dir,
            filename="cancel-during-acquisition.wav",
            size_bytes=4,
            max_upload_bytes=settings.max_upload_bytes,
        )
        upload_id = upload["id"]
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/api/uploads/{upload_id}/content"
        )
        real_begin = routes_module.begin_upload
        begin_entered = threading.Event()
        begin_release = threading.Event()

        def slow_begin(*args, **kwargs):
            result = real_begin(*args, **kwargs)
            begin_entered.set()
            assert begin_release.wait(5)
            return result

        monkeypatch.setattr(routes_module, "begin_upload", slow_begin)

        async def receive():
            return {"type": "http.request", "body": b"abcd", "more_body": False}

        first = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    receive,
                ),
                offset=0,
            )
        )
        assert await asyncio.to_thread(begin_entered.wait, 2)
        first.cancel()
        first.cancel()
        begin_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)

        resumed = await endpoint(
            upload_id,
            Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": "/probe",
                    "headers": [],
                    "query_string": b"",
                },
                receive,
            ),
            offset=0,
        )
        assert resumed["status"] == "uploaded"


async def test_cancel_during_durable_commit_waits_before_releasing_writer(settings, monkeypatch):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        upload = recordings_module.create_upload(
            settings.data_dir,
            filename="cancel-inflight.wav",
            size_bytes=8,
            max_upload_bytes=settings.max_upload_bytes,
        )
        upload_id = upload["id"]
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/api/uploads/{upload_id}/content"
        )
        real_finish = routes_module.finish_upload
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_finish(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            try:
                return real_finish(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(routes_module, "finish_upload", slow_finish)

        async def receive():
            return {"type": "http.request", "body": b"abcd", "more_body": False}

        first = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    receive,
                ),
                offset=0,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()

        async def retry_receive():
            return {"type": "http.request", "body": b"efgh", "more_body": False}

        retry = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    retry_receive,
                ),
                offset=0,
            )
        )
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(retry, timeout=2)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "upload_busy"
        assert not finished.is_set()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)
        assert finished.is_set()

        raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
        assert recordings_module.get_upload(settings.data_dir, upload_id)["received_bytes"] == 4
        assert raw_path.read_bytes() == b"abcd"

        async def receive_resume():
            return {"type": "http.request", "body": b"efgh", "more_body": False}

        resumed = await endpoint(
            upload_id,
            Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": "/probe",
                    "headers": [],
                    "query_string": b"",
                },
                receive_resume,
            ),
            offset=4,
        )
        assert resumed["status"] == "uploaded"
        assert raw_path.read_bytes() == b"abcdefgh"


async def test_repeated_cancel_after_commit_preserves_offset_and_writer_guard(
    settings, monkeypatch
):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        upload = recordings_module.create_upload(
            settings.data_dir,
            filename="cancel-after-commit.wav",
            size_bytes=8,
            max_upload_bytes=settings.max_upload_bytes,
        )
        upload_id = upload["id"]
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/api/uploads/{upload_id}/content"
        )
        real_finish = routes_module.finish_upload
        finish_entered = threading.Event()
        finish_committed = threading.Event()
        finish_release = threading.Event()
        first_finish = True

        def slow_finish(*args, **kwargs):
            nonlocal first_finish
            finish_entered.set()
            result = real_finish(*args, **kwargs)
            if first_finish:
                first_finish = False
                finish_committed.set()
                assert finish_release.wait(5)
            return result

        monkeypatch.setattr(routes_module, "finish_upload", slow_finish)

        async def receive():
            return {"type": "http.request", "body": b"abcd", "more_body": False}

        first = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    receive,
                ),
                offset=0,
            )
        )
        assert await asyncio.to_thread(finish_entered.wait, 2)
        assert await asyncio.to_thread(finish_committed.wait, 2)
        first.cancel()
        first.cancel()

        async def retry_receive():
            return {"type": "http.request", "body": b"efgh", "more_body": False}

        retry = asyncio.create_task(
            endpoint(
                upload_id,
                Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/probe",
                        "headers": [],
                        "query_string": b"",
                    },
                    retry_receive,
                ),
                offset=4,
            )
        )
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(retry, timeout=2)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "upload_busy"
        assert error.value.detail["received_bytes"] == 4

        finish_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)

        raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
        state = recordings_module.get_upload(settings.data_dir, upload_id)
        assert state["status"] == "receiving"
        assert state["received_bytes"] == 4
        assert raw_path.read_bytes() == b"abcd"

        async def receive_resume():
            return {"type": "http.request", "body": b"efgh", "more_body": False}

        resumed = await endpoint(
            upload_id,
            Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": "/probe",
                    "headers": [],
                    "query_string": b"",
                },
                receive_resume,
            ),
            offset=4,
        )
        assert resumed["status"] == "uploaded"
        assert raw_path.read_bytes() == b"abcdefgh"


async def test_chunked_upload_requires_the_durable_received_offset(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "chunked.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    first = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content[:32],
    )
    assert first.status_code == 200
    assert first.json() == {
        "id": upload_id,
        "status": "receiving",
        "size_bytes": len(content),
        "received_bytes": 32,
    }
    mismatch = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content[:32],
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == {
        "code": "offset_mismatch",
        "message": "Resume from received_bytes=32.",
        "received_bytes": 32,
    }
    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "receiving"
    assert state.json()["received_bytes"] == 32
    middle = await client.put(
        f"/api/uploads/{upload_id}/content?offset=32",
        headers={"Origin": settings.public_url},
        content=content[32:64],
    )
    assert middle.status_code == 200
    assert middle.json()["status"] == "receiving"
    assert middle.json()["received_bytes"] == 64
    final = await client.put(
        f"/api/uploads/{upload_id}/content?offset=64",
        headers={"Origin": settings.public_url},
        content=content[64:],
    )
    assert final.status_code == 200
    assert final.json()["status"] == "uploaded"
    assert (settings.data_dir / "uploads" / f"{upload_id}.part").read_bytes() == content


async def test_resume_truncates_uncommitted_raw_bytes_before_appending(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "stale-bytes.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    first = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content[:32],
    )
    assert first.status_code == 200
    raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
    raw_path.write_bytes(content + b"uncommitted-junk")

    resumed = await client.put(
        f"/api/uploads/{upload_id}/content?offset=32",
        headers={"Origin": settings.public_url},
        content=content[32:],
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "uploaded"
    assert raw_path.read_bytes() == content


async def test_empty_chunk_is_rejected_without_advancing_upload(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "empty-chunk.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    empty = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=b"",
    )
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "size_mismatch"
    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "failed"
    assert state.json()["received_bytes"] == 0
    assert not (settings.data_dir / "uploads" / f"{upload_id}.part").exists()


async def test_concurrent_content_requests_keep_the_single_writer_guard(client, settings):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "concurrent.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def held_stream():
        yield content[:32]
        stream_started.set()
        await release_stream.wait()
        yield content[32:]

    first = asyncio.create_task(
        client.put(
            f"/api/uploads/{upload_id}/content?offset=0",
            headers={"Origin": settings.public_url},
            content=held_stream(),
        )
    )
    await asyncio.wait_for(stream_started.wait(), timeout=2)
    second = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert second.status_code == 409
    assert second.json()["detail"] == {
        "code": "upload_busy",
        "message": "Another upload content request is already running.",
        "received_bytes": 0,
    }
    release_stream.set()
    completed = await asyncio.wait_for(first, timeout=2)
    assert completed.status_code == 200
    assert completed.json()["status"] == "uploaded"


async def test_range_head_and_unsatisfiable_range_are_exact(client, settings):
    await _authorize(client, settings)
    upload = await _upload_ready(client, settings, _wav_bytes())
    attached = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url},
        json={"upload_id": upload["id"], "title": "Range test", "language_hint": "vi"},
    )
    meeting_id = attached.json()["id"]
    full = await client.get(f"/api/meetings/{meeting_id}/audio")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "audio/mp4"
    assert int(full.headers["content-length"]) == len(full.content)

    partial = await client.get(f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=0-5"})
    assert partial.status_code == 206
    assert partial.content == full.content[:6]
    assert partial.headers["content-range"] == f"bytes 0-5/{len(full.content)}"
    assert partial.headers["content-length"] == "6"

    suffix = await client.get(f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=-5"})
    assert suffix.status_code == 206
    assert suffix.content == full.content[-5:]
    assert (
        suffix.headers["content-range"]
        == f"bytes {len(full.content) - 5}-{len(full.content) - 1}/{len(full.content)}"
    )

    head = await client.head(f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=2-7"})
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-range"] == f"bytes 2-7/{len(full.content)}"
    assert head.headers["content-length"] == "6"

    invalid = await client.get(
        f"/api/meetings/{meeting_id}/audio", headers={"Range": f"bytes={len(full.content)}-"}
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(full.content)}"
    multiple = await client.get(
        f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=0-1,4-5"}
    )
    assert multiple.status_code == 200
    assert multiple.content == full.content
    for ignored_range in ("items=0-1", "bytes=bad", "bytes=7-2"):
        ignored = await client.get(
            f"/api/meetings/{meeting_id}/audio", headers={"Range": ignored_range}
        )
        assert ignored.status_code == 200
        assert ignored.content == full.content
    zero_suffix = await client.get(
        f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=-0"}
    )
    assert zero_suffix.status_code == 416


async def test_audio_survives_app_recreation_and_original_is_gone(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        await _authorize(client, settings)
        upload = await _upload_ready(client, settings, _wav_bytes())
        meeting = await client.post(
            "/api/meetings",
            headers={"Origin": settings.public_url},
            json={
                "upload_id": upload["id"],
                "title": "Persistent",
                "language_hint": "en",
            },
        )
        meeting_id = meeting.json()["id"]
        persisted_audio = await client.get(f"/api/meetings/{meeting_id}/audio")
        assert persisted_audio.status_code == 200
        assert not (settings.data_dir / "uploads" / f"{upload['id']}.part").exists()

    reopened_app = create_app(settings)
    async with (
        reopened_app.router.lifespan_context(reopened_app),
        AsyncClient(
            transport=ASGITransport(app=reopened_app), base_url=settings.public_url
        ) as client,
    ):
        await _authorize(client, settings)
        meeting_response = await client.get(f"/api/meetings/{meeting_id}")
        audio_response = await client.get(f"/api/meetings/{meeting_id}/audio")
        assert meeting_response.status_code == 200
        assert audio_response.status_code == 200
        assert audio_response.content == persisted_audio.content


async def test_startup_recovery_fails_unattached_uploads_and_keeps_consumed_audio(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        await _authorize(client, settings)
        upload = await _upload_ready(client, settings, _wav_bytes())
        ready_upload = await _upload_ready(client, settings, _wav_bytes())
        meeting = await client.post(
            "/api/meetings",
            headers={"Origin": settings.public_url},
            json={
                "upload_id": upload["id"],
                "title": "Keep me",
                "language_hint": "en",
            },
        )
        meeting_id = meeting.json()["id"]
        stale_ids = []
        for status in ("receiving", "uploaded", "validating"):
            stale = await client.post(
                "/api/uploads",
                headers={"Origin": settings.public_url},
                json={"filename": f"{status}.wav", "size_bytes": len(_wav_bytes())},
            )
            stale_ids.append(stale.json()["id"])
        with sqlite3.connect(settings.data_dir / "app.sqlite3") as connection:
            for stale_id, status in zip(
                stale_ids, ("receiving", "uploaded", "validating"), strict=True
            ):
                connection.execute("UPDATE uploads SET status = ? WHERE id = ?", (status, stale_id))
        stale_paths = [settings.data_dir / "uploads" / f"{stale_id}.part" for stale_id in stale_ids]
        temp_dir = settings.data_dir / ".recording-tmp"
        temp_path = temp_dir / "orphan.tmp"
        temp_path.write_bytes(b"temporary")
        orphan_upload = settings.data_dir / "uploads" / "orphan.part"
        orphan_audio = settings.data_dir / "recordings" / f"{'f' * 32}.m4a"
        orphan_upload.write_bytes(b"partial")
        orphan_audio.write_bytes(b"unreferenced")

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        assert all(not path.exists() for path in stale_paths)
        assert not temp_path.exists()
        assert not orphan_upload.exists()
        assert not orphan_audio.exists()
        assert (settings.data_dir / "recordings" / f"{upload['id']}.m4a").exists()
        assert not (settings.data_dir / "recordings" / f"{ready_upload['id']}.m4a").exists()
        with sqlite3.connect(settings.data_dir / "app.sqlite3") as connection:
            statuses = [
                connection.execute(
                    "SELECT status FROM uploads WHERE id = ?", (stale_id,)
                ).fetchone()[0]
                for stale_id in stale_ids
            ]
        assert statuses == ["failed", "failed", "failed"]
        filenames = [
            connection.execute(
                "SELECT filename, failure_code FROM uploads WHERE id = ?", (stale_id,)
            ).fetchone()
            for stale_id in stale_ids
        ]
        assert filenames == [("removed-upload", "interrupted")] * 3
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url=settings.public_url
        ) as client:
            await _authorize(client, settings)
            response = await client.get(f"/api/meetings/{meeting_id}/audio")
            ready_response = await client.get(f"/api/uploads/{ready_upload['id']}")
            assert response.status_code == 200
            assert ready_response.json()["status"] == "failed"


async def test_new_upload_sweeps_stale_unattached_files(client, settings):
    await _authorize(client, settings)
    stale = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "stale.wav", "size_bytes": 8},
    )
    stale_id = stale.json()["id"]
    stale_path = settings.data_dir / "uploads" / f"{stale_id}.part"
    stale_path.write_bytes(b"partial")
    with sqlite3.connect(settings.data_dir / "app.sqlite3") as connection:
        connection.execute(
            "UPDATE uploads SET created_at = datetime('now', '-2 days') WHERE id = ?",
            (stale_id,),
        )

    replacement = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "replacement.wav", "size_bytes": 8},
    )

    assert replacement.status_code == 201
    stale_state = await client.get(f"/api/uploads/{stale_id}")
    assert stale_state.json()["status"] == "failed"
    assert stale_state.json()["filename"] == "removed-upload"
    assert stale_state.json()["failure_code"] == "expired"
    assert not stale_path.exists()


@pytest.mark.parametrize("extra_failed_count", [0, 100])
def test_startup_cleanup_backfills_legacy_failed_filenames_without_touching_live_names(
    settings, extra_failed_count
):
    initialize(settings.data_dir)
    legacy = recordings_module.create_upload(
        settings.data_dir,
        filename="legacy-original.wav",
        size_bytes=8,
        max_upload_bytes=settings.max_upload_bytes,
    )
    ready = recordings_module.create_upload(
        settings.data_dir,
        filename="still-ready.wav",
        size_bytes=8,
        max_upload_bytes=settings.max_upload_bytes,
    )
    consumed = recordings_module.create_upload(
        settings.data_dir,
        filename="still-consumed.wav",
        size_bytes=8,
        max_upload_bytes=settings.max_upload_bytes,
    )
    legacy_ids = [legacy["id"]]
    for index in range(extra_failed_count):
        upload = recordings_module.create_upload(
            settings.data_dir,
            filename=f"legacy-original-{index}.wav",
            size_bytes=8,
            max_upload_bytes=settings.max_upload_bytes,
        )
        legacy_ids.append(upload["id"])
    with sqlite3.connect(settings.data_dir / "app.sqlite3") as connection:
        connection.executemany(
            "UPDATE uploads SET status = 'failed', failure_code = 'invalid_media' WHERE id = ?",
            [(upload_id,) for upload_id in legacy_ids],
        )
        connection.execute("UPDATE uploads SET status = 'ready' WHERE id = ?", (ready["id"],))
        connection.execute("UPDATE uploads SET status = 'consumed' WHERE id = ?", (consumed["id"],))

    recordings_module.cleanup_stale_uploads(settings.data_dir)

    legacy_state = recordings_module.get_upload(settings.data_dir, legacy["id"])
    ready_state = recordings_module.get_upload(settings.data_dir, ready["id"])
    consumed_state = recordings_module.get_upload(settings.data_dir, consumed["id"])
    assert legacy_state["filename"] == "removed-upload"
    assert legacy_state["failure_code"] == "invalid_media"
    with sqlite3.connect(settings.data_dir / "app.sqlite3") as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM uploads WHERE status = 'failed' AND filename != ?",
            ("removed-upload",),
        ).fetchone()[0]
    assert remaining == 0
    assert ready_state["filename"] == "still-ready.wav"
    assert consumed_state["filename"] == "still-consumed.wav"


async def test_delete_cancels_active_validation_and_removes_artifacts(
    client, settings, monkeypatch
):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "cancel.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    received = await client.put(
        f"/api/uploads/{upload_id}/content",
        headers={"Origin": settings.public_url},
        content=content,
    )
    assert received.status_code == 200
    validation_started = threading.Event()

    def cancellable_normalize(
        source_path,
        output_path,
        *,
        max_duration_seconds,
        should_cancel,
    ):
        validation_started.set()
        deadline = time.monotonic() + 2
        while not should_cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not should_cancel():
            raise AssertionError("DELETE did not cancel the active validation")
        raise MediaProcessingError("media_processing_cancelled")

    monkeypatch.setattr(recordings_module, "normalize_media", cancellable_normalize)
    completion_task = asyncio.create_task(
        client.post(
            f"/api/uploads/{upload_id}/complete",
            headers={"Origin": settings.public_url},
        )
    )
    assert await asyncio.to_thread(validation_started.wait, 2)

    deleted = await client.delete(
        f"/api/uploads/{upload_id}", headers={"Origin": settings.public_url}
    )
    completed = await asyncio.wait_for(completion_task, timeout=2)

    assert deleted.status_code == 200
    assert deleted.json()["status"] == "failed"
    assert completed.status_code == 409
    assert completed.json()["detail"]["code"] == "media_processing_cancelled"
    deleted_state = await client.get(f"/api/uploads/{upload_id}")
    assert deleted_state.json()["filename"] == "removed-upload"
    assert deleted_state.json()["failure_code"] == "deleted"
    assert not (settings.data_dir / "uploads" / f"{upload_id}.part").exists()
    assert not (settings.data_dir / "recordings" / f"{upload_id}.m4a").exists()


async def test_upload_idle_timeout_releases_the_upload_and_preserves_progress(
    client, settings, monkeypatch
):
    await _authorize(client, settings)
    content = _wav_bytes()
    created = await client.post(
        "/api/uploads",
        headers={"Origin": settings.public_url},
        json={"filename": "stalled.wav", "size_bytes": len(content)},
    )
    upload_id = created.json()["id"]
    committed = await client.put(
        f"/api/uploads/{upload_id}/content?offset=0",
        headers={"Origin": settings.public_url},
        content=content[:32],
    )
    assert committed.status_code == 200
    assert committed.json()["received_bytes"] == 32
    monkeypatch.setattr(routes_module, "_UPLOAD_IDLE_TIMEOUT_SECONDS", 0.02)

    async def stalled_stream():
        yield content[32:64]
        await asyncio.Event().wait()

    timed_out = await asyncio.wait_for(
        client.put(
            f"/api/uploads/{upload_id}/content?offset=32",
            headers={"Origin": settings.public_url},
            content=stalled_stream(),
        ),
        timeout=2,
    )

    assert timed_out.status_code == 408
    assert timed_out.json()["detail"]["code"] == "upload_timeout"
    assert timed_out.json()["detail"]["received_bytes"] == 32
    state = await client.get(f"/api/uploads/{upload_id}")
    assert state.json()["status"] == "receiving"
    assert state.json()["received_bytes"] == 32
    raw_path = settings.data_dir / "uploads" / f"{upload_id}.part"
    assert raw_path.read_bytes() == content[:32]
    resumed = await client.put(
        f"/api/uploads/{upload_id}/content?offset=32",
        headers={"Origin": settings.public_url},
        content=content[32:],
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "uploaded"


async def test_declared_duration_limit_is_enforced(tmp_path):
    _require_media_tools()
    settings = Settings(
        data_dir=tmp_path / "limited-data",
        auth_dir=tmp_path / "limited-auth",
        public_url="http://127.0.0.1:8765",
        max_recording_seconds=1,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        await _authorize(client, settings)
        upload = await client.post(
            "/api/uploads",
            headers={"Origin": settings.public_url},
            json={"filename": "long.wav", "size_bytes": len(_wav_bytes(1.5))},
        )
        upload_id = upload.json()["id"]
        await client.put(
            f"/api/uploads/{upload_id}/content",
            headers={"Origin": settings.public_url},
            content=_wav_bytes(1.5),
        )
        complete = await client.post(
            f"/api/uploads/{upload_id}/complete",
            headers={"Origin": settings.public_url},
        )
        assert complete.status_code == 413
        assert complete.json()["detail"]["code"] == "duration_limit"
    assert complete.json()["detail"]["max_duration_seconds"] == 1
    assert "1 seconds" in complete.json()["detail"]["message"]


async def test_meeting_details_validate_title_date_and_language(client, settings):
    await _authorize(client, settings)
    upload = await _upload_ready(client, settings, _wav_bytes())
    base_payload = {"upload_id": upload["id"], "title": "Valid", "language_hint": "en"}
    invalids = [
        {**base_payload, "title": "   "},
        {**base_payload, "title": "x" * 201},
        {**base_payload, "meeting_date": "2026-02-30"},
        {**base_payload, "meeting_date": "0999-10-15"},
        {**base_payload, "meeting_date": "10000-10-15"},
        {**base_payload, "language_hint": "fr"},
    ]
    for payload in invalids:
        response = await client.post(
            "/api/meetings", headers={"Origin": settings.public_url}, json=payload
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"].startswith("invalid_")


async def test_shell_route_and_script_are_protected_and_refresh_uses_shell(client, settings):
    unauthenticated = await client.get("/meetings/known-id", follow_redirects=False)
    assert unauthenticated.status_code == 303
    await _authorize(client, settings)
    page = await client.get("/meetings/known-id")
    assert page.status_code == 200
    assert "ASR Studio" in page.text
    for name in (
        "shell.js",
        "shell-core.js",
        "store.js",
        "channel.js",
        "notes-core.js",
        "notes.js",
        "history.js",
        "history-core.js",
        "task-core.js",
        "transcript-core.js",
        "playback-core.js",
        "provider-core.js",
        "processing-stage-policy-core.js",
        "connections.js",
        "antigravity.js",
    ):
        script = await client.get(f"/assets/{name}")
        assert script.status_code == 200, name
        assert script.headers["content-type"].startswith("text/javascript"), name
