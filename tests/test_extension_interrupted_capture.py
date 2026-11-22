"""Saving the recorded prefix of an interrupted extension capture (#239)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from test_extension_ingest import _create_payload, _extension_session, _manifest

from asr_studio.app import EXTENSION_ORIGIN, create_app

pytestmark = pytest.mark.anyio

CONTENT = b"interrupted-prefix-audio"


def _interrupted_manifest(code: object = "source_ended") -> dict[str, object]:
    return {**_manifest(CONTENT, epoch=1), "capture_complete": False, "interruption_code": code}


@pytest.fixture
async def ingest_client(settings, monkeypatch):
    def fake_normalize(_source, output, **_kwargs):
        Path(output).write_bytes(b"normalized")
        return SimpleNamespace(duration_seconds=1.5)

    monkeypatch.setattr("asr_studio.extension_ingest.normalize_extension_media", fake_normalize)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        token = await _extension_session(client, settings)
        client.headers["Origin"] = settings.public_url
        yield client, {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}


async def _session_with_chunk(client: AsyncClient, auth: dict[str, str]) -> dict[str, object]:
    created = await client.post(
        "/api/extension/v1/recording-sessions", headers=auth, json=_create_payload()
    )
    assert created.status_code == 201, created.text
    session = created.json()
    chunk = await client.put(
        f"/api/extension/v1/recording-sessions/{session['id']}/chunks/0",
        headers={
            **auth,
            "X-Chunk-Offset": "0",
            "X-Chunk-SHA256": hashlib.sha256(CONTENT).hexdigest(),
            "X-Writer-Epoch": "1",
        },
        content=CONTENT,
    )
    assert chunk.status_code == 200, chunk.text
    return session


async def _interrupt(
    client: AsyncClient,
    auth: dict[str, str],
    session_id: str,
    code: object = "source_ended",
    epoch: object = 1,
):
    return await client.post(
        f"/api/extension/v1/recording-sessions/{session_id}/interrupt",
        headers=auth,
        json={"epoch": epoch, "interruption_code": code},
    )


async def _wait_ready(client: AsyncClient, auth: dict[str, str], session_id: str) -> dict:
    status = None
    for _ in range(300):
        status = (
            await client.get(f"/api/extension/v1/recording-sessions/{session_id}", headers=auth)
        ).json()
        if status["state"] == "ready":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(status)


async def test_saved_prefix_is_labeled_and_waits_for_explicit_start(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    assert (await _interrupt(client, auth, session["id"])).status_code == 200
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_interrupted_manifest(),
    )
    assert finalized.status_code == 202, finalized.text
    ready = await _wait_ready(client, auth, session["id"])
    assert ready["receipt"]["total_bytes"] == len(CONTENT)

    meeting_id = ready["meeting_url"].rsplit("/", 1)[-1]
    meeting = (await client.get(f"/api/meetings/{meeting_id}")).json()
    assert meeting["recording"]["state"] == "ready"
    assert meeting["recording"]["interrupted"] == {"code": "source_ended"}
    history = (await client.get("/api/meetings")).json()["meetings"]
    assert history[0]["recording_interruption_code"] == "source_ended"

    processing = meeting["processing"]
    assert processing["state"] == "needs_attention"
    steps = {step["kind"]: step for step in processing["steps"]}
    assert steps["transcription"]["waiting_reason"] == "interrupted_recording"
    assert processing["actions"]["use_current_source"] is True

    source = processing["current_source"]
    started = await client.post(
        f"/api/meetings/{meeting_id}/processing/use-current-source",
        json={
            "expected_pipeline_epoch": processing["epoch"],
            "expected_generation_id": None,
            "expected_source_version": None,
            "expected_metadata_version": source["metadata_version"],
            "expected_notes_source_version": source["notes_source_version"],
        },
    )
    assert started.status_code == 200, started.text
    after = (await client.get(f"/api/meetings/{meeting_id}")).json()["processing"]
    assert after["state"] == "waiting"
    assert {step["kind"]: step for step in after["steps"]}["transcription"][
        "waiting_reason"
    ] != "interrupted_recording"
    # The label is durable: confirming the start does not erase it.
    meeting = (await client.get(f"/api/meetings/{meeting_id}")).json()
    assert meeting["recording"]["interrupted"] == {"code": "source_ended"}


async def test_complete_capture_is_unlabeled_and_starts_automatically(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_manifest(CONTENT, epoch=1),
    )
    assert finalized.status_code == 202, finalized.text
    ready = await _wait_ready(client, auth, session["id"])
    meeting = (await client.get(f"/api/meetings/{ready['meeting_url'].rsplit('/', 1)[-1]}")).json()
    assert meeting["recording"]["interrupted"] is None
    assert meeting["processing"]["state"] != "needs_attention"


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"capture_complete": False}, 409, "capture_incomplete"),
        ({"capture_complete": False, "interruption_code": "made_up"}, 409, "capture_incomplete"),
        # A recognized code is not enough: the session never recorded an interruption.
        (
            {"capture_complete": False, "interruption_code": "source_ended"},
            409,
            "capture_incomplete",
        ),
        (
            {"capture_complete": False, "interruption_code": ["source_ended"]},
            400,
            "invalid_request",
        ),
        ({"capture_complete": False, "interruption_code": {"a": 1}}, 400, "invalid_request"),
        ({"capture_complete": False, "interruption_code": None}, 400, "invalid_request"),
        ({"capture_complete": True, "interruption_code": "source_ended"}, 400, "invalid_request"),
        ({"capture_complete": 0, "interruption_code": "source_ended"}, 400, "invalid_request"),
    ],
)
async def test_incomplete_finalize_requires_known_interruption(
    ingest_client, payload, status, code
):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    body = {**_manifest(CONTENT, epoch=1), **payload}
    if "interruption_code" not in payload:
        body.pop("interruption_code", None)
    response = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize", headers=auth, json=body
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    state = (
        await client.get(f"/api/extension/v1/recording-sessions/{session['id']}", headers=auth)
    ).json()["state"]
    assert state == "receiving"


async def test_saved_prefix_is_verified_like_a_complete_finalize(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    assert (await _interrupt(client, auth, session["id"])).status_code == 200
    tampered = _interrupted_manifest()
    tampered["manifest_hash"] = hashlib.sha256(b"other").hexdigest()
    response = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=tampered,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "manifest_conflict"

    conflicting = _interrupted_manifest()
    accepted = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=conflicting,
    )
    assert accepted.status_code == 202, accepted.text
    other_reason = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_interrupted_manifest("recorder_error"),
    )
    assert other_reason.status_code == 409
    assert other_reason.json()["error"]["code"] == "capture_incomplete"


async def test_interruption_is_recorded_once_for_the_owned_session(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    first = await _interrupt(client, auth, session["id"], "source_ended")
    assert first.status_code == 200, first.text
    again = await _interrupt(client, auth, session["id"], "source_ended")
    assert again.status_code == 200, again.text
    other = await _interrupt(client, auth, session["id"], "recorder_error")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "interruption_conflict"
    # The recorded reason, not the finalize body, decides the saved label.
    mismatched = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_interrupted_manifest("recorder_error"),
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["error"]["code"] == "capture_incomplete"
    # A complete finalize cannot reuse a session that recorded an interruption.
    complete = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_manifest(CONTENT, epoch=1),
    )
    assert complete.status_code == 409
    assert complete.json()["error"]["code"] == "capture_incomplete"


@pytest.mark.parametrize(
    ("code", "epoch", "status", "error"),
    [
        ("made_up", 1, 400, "invalid_request"),
        (["source_ended"], 1, 400, "invalid_request"),
        ({"a": 1}, 1, 400, "invalid_request"),
        ("source_ended", 2, 409, "writer_epoch_conflict"),
        ("source_ended", True, 400, "invalid_epoch"),
    ],
)
async def test_interruption_rejects_invalid_input_without_changing_the_session(
    ingest_client, code, epoch, status, error
):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    response = await _interrupt(client, auth, session["id"], code, epoch)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == error
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_interrupted_manifest(),
    )
    assert finalized.status_code == 409


async def test_interruption_is_refused_after_finalize(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    done = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_manifest(CONTENT, epoch=1),
    )
    assert done.status_code == 202, done.text
    late = await _interrupt(client, auth, session["id"])
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "too_late"


def test_extension_and_server_accept_the_same_interruption_codes():
    import re

    from asr_studio.extension_ingest import CAPTURE_INTERRUPTION_CODES

    source = (Path(__file__).parent.parent / "extensions/chrome/capture-core.js").read_text()
    block = re.search(r"INTERRUPTION_CODES = Object\.freeze\(\[(.*?)\]\)", source, re.S)
    assert block, "capture-core.js no longer lists INTERRUPTION_CODES"
    assert set(re.findall(r'"(\w+)"', block.group(1))) == CAPTURE_INTERRUPTION_CODES


async def test_edited_meeting_details_do_not_hide_the_interrupted_start(ingest_client):
    client, auth = ingest_client
    session = await _session_with_chunk(client, auth)
    meeting_id = session["meeting_url"].rsplit("/", 1)[-1]
    renamed = await client.patch(
        f"/api/meetings/{meeting_id}", json={"expected_metadata_version": 1, "title": "Edited"}
    )
    assert renamed.status_code == 200, renamed.text
    assert (await _interrupt(client, auth, session["id"])).status_code == 200
    finalized = await client.post(
        f"/api/extension/v1/recording-sessions/{session['id']}/finalize",
        headers=auth,
        json=_interrupted_manifest(),
    )
    assert finalized.status_code == 202, finalized.text
    await _wait_ready(client, auth, session["id"])

    processing = (await client.get(f"/api/meetings/{meeting_id}")).json()["processing"]
    assert processing["state"] == "needs_attention"
    reasons = {step["kind"]: step["waiting_reason"] for step in processing["steps"]}
    assert reasons == {"transcription": "interrupted_recording", "notes": "interrupted_recording"}
