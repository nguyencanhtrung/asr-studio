from __future__ import annotations

import sqlite3
import time

import pytest
from httpx import ASGITransport, AsyncClient
from notes_helpers import StubNotesProvider, seed_cited_notes, seed_completed_transcript

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.chatgpt import DIRECT_SCOPE
from asr_studio.config import Settings
from asr_studio.notes import accept_notes_task, publish_notes_revision
from asr_studio.storage import database_path
from asr_studio.tasks import add_current_generation_speaker, claim_next_task, finish_canceled_task

pytestmark = pytest.mark.anyio


async def test_notes_routes_enforce_auth_and_preserve_saved_revision_contract(
    tmp_path, monkeypatch
):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        meeting_id, generation_id, segment_ids = seed_completed_transcript(settings.data_dir)
        monkeypatch.setattr(
            "asr_studio.notes_routes.worker_is_healthy",
            lambda _path, *, role=None: True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=settings.public_url
        ) as client:
            assert (await client.get(f"/api/meetings/{meeting_id}/notes")).status_code == 401
            assert (await client.get("/assets/notes.js")).status_code == 303
            token = app.state.auth_store.load().token
            origin = {"Origin": settings.public_url}
            authenticated = await client.post(
                "/api/access/session", headers=origin, json={"token": token}
            )
            assert authenticated.status_code == 204

            provider = app.state.chatgpt_provider
            client_id = "oaiapp_test-123456"
            with provider.store.locked(write=True) as state:
                state["connection_status"] = "connected"
                state["connection_version"] = 9
                state["active_client_id"] = client_id
                state["registrations"][client_id] = {
                    "access_token": "local-test-access-token",
                    "refresh_token": "local-test-refresh-token",
                    "expires_at": time.time() + 3600,
                    "scopes": [DIRECT_SCOPE],
                    "subject": "test-subject",
                    "label": "Test account",
                }
                state["notes_provider"] = "chatgpt"
                state["selected_model"] = "gpt-6.1-sol"
                state["selected_reasoning_effort"] = "high"

            base_payload = {
                "request_id": "api-notes-request-invalid",
                "expected_generation_id": generation_id,
                "expected_source_version": 1,
                "expected_metadata_version": 1,
            }
            for language in (None, True, "fr"):
                invalid = await client.post(
                    f"/api/meetings/{meeting_id}/notes-runs",
                    headers=origin,
                    json={**base_payload, "output_language": language},
                )
                assert invalid.status_code == 400
                assert invalid.json()["detail"]["code"] == "invalid_output_language"
            unknown = await client.post(
                f"/api/meetings/{meeting_id}/notes-runs",
                headers=origin,
                json={**base_payload, "unexpected": "rejected"},
            )
            assert unknown.status_code == 400

            accepted = await client.post(
                f"/api/meetings/{meeting_id}/notes-runs",
                headers=origin,
                json={
                    "request_id": "api-notes-request-001",
                    "expected_generation_id": generation_id,
                    "expected_source_version": 1,
                    "expected_metadata_version": 1,
                },
            )
            assert accepted.status_code == 202
            task = accepted.json()
            assert task["kind"] == "notes"
            assert task["status"] == "accepted"
            assert task["result_scope"] == "notes_only"
            assert task["output_language"] == "en"

            same_english_request = await client.post(
                f"/api/meetings/{meeting_id}/notes-runs",
                headers=origin,
                json={
                    "request_id": "api-notes-request-001",
                    "expected_generation_id": generation_id,
                    "expected_source_version": 1,
                    "expected_metadata_version": 1,
                    "output_language": "en",
                },
            )
            assert same_english_request.status_code == 202
            assert same_english_request.json()["id"] == task["id"]
            language_conflict = await client.post(
                f"/api/meetings/{meeting_id}/notes-runs",
                headers=origin,
                json={
                    "request_id": "api-notes-request-001",
                    "expected_generation_id": generation_id,
                    "expected_source_version": 1,
                    "expected_metadata_version": 1,
                    "output_language": "vi",
                },
            )
            assert language_conflict.status_code == 409
            assert language_conflict.json()["detail"]["code"] == "idempotency_conflict"

            listing = await client.get(f"/api/meetings/{meeting_id}/notes")
            assert listing.status_code == 200
            assert listing.json()["revisions"] == []
            assert listing.json()["task"]["id"] == task["id"]
            assert listing.json()["source_locked"] is True
            meeting = await client.get(f"/api/meetings/{meeting_id}")
            assert meeting.json()["notes_source_locked"] is True
            with sqlite3.connect(database_path(settings.data_dir)) as connection:
                source_id = connection.execute(
                    "SELECT notes_source_id FROM tasks WHERE id = ?", (task["id"],)
                ).fetchone()[0]

            claimed = claim_next_task(settings.data_dir, owner_id="api-notes-worker")
            assert claimed is not None and claimed["id"] == task["id"]
            citations = [
                {
                    "section": "Overview",
                    "text": "Saved, complete text\\.",
                    "sources": [{"id": segment_ids[0], "start_seconds": 1.0}],
                }
            ]
            saved = publish_notes_revision(
                settings.data_dir,
                task["id"],
                owner_id="api-notes-worker",
                markdown="# Notes\n\nSaved, complete text.\n",
                elapsed_seconds=2.0,
                citations=citations,
            )
            assert saved is not None
            assert saved["output_language"] == "en"

            source = await client.get(f"/api/notes-sources/{source_id}")
            assert source.status_code == 200
            assert source.json()["segments"][0]["text"] == "We need a launch plan."
            detail = await client.get(f"/api/notes/{saved['id']}")
            assert detail.status_code == 200
            assert detail.json()["source_state"] == "current"
            assert detail.json()["citations"] == citations
            export = await client.get(f"/api/notes/{saved['id']}/export")
            assert export.status_code == 200
            assert export.text == "# Notes\n\nSaved, complete text.\n"

            conflict = await client.patch(
                f"/api/notes/{saved['id']}",
                headers=origin,
                json={"expected_edit_version": 2, "markdown": "Do not overwrite."},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "notes_edit_version_changed"
            updated = await client.patch(
                f"/api/notes/{saved['id']}",
                headers=origin,
                json={"expected_edit_version": 1, "markdown": "# Edited locally\n"},
            )
            assert updated.status_code == 200
            assert updated.json()["edit_version"] == 2
            assert updated.json()["markdown"] == "# Edited locally\n"
            assert updated.json()["citations"] == citations  # an edit never rewrites them

            vietnamese = await client.post(
                f"/api/meetings/{meeting_id}/notes-runs",
                headers=origin,
                json={
                    "request_id": "api-notes-request-vi-001",
                    "expected_generation_id": generation_id,
                    "expected_source_version": 1,
                    "expected_metadata_version": 1,
                    "output_language": "vi",
                },
            )
            assert vietnamese.status_code == 202
            assert vietnamese.json()["output_language"] == "vi"
            claim_next_task(settings.data_dir, owner_id="api-notes-worker-vi")
            vietnamese_revision = publish_notes_revision(
                settings.data_dir,
                vietnamese.json()["id"],
                owner_id="api-notes-worker-vi",
                markdown="## Tổng quan\n\nĐã lưu.\n",
                elapsed_seconds=1.0,
            )
            assert vietnamese_revision is not None
            assert vietnamese_revision["output_language"] == "vi"
            history = await client.get(f"/api/meetings/{meeting_id}/notes")
            assert history.json()["revisions"][0]["output_language"] == "vi"
            assert history.json()["revisions"][0]["citations"] == []  # none were kept for it
            assert history.json()["revisions"][1]["citations"] == citations
            assert history.json()["task"]["output_language"] == "vi"


async def _authorize(client, settings) -> dict[str, str]:
    headers = {"Origin": settings.public_url}
    token = AuthStore(settings.auth_dir).load().token
    response = await client.post("/api/access/session", headers=headers, json={"token": token})
    assert response.status_code == 204
    return headers


async def test_patch_keeps_a_broken_source_link_verbatim_and_answers_with_a_warning(
    client, settings
):
    headers = await _authorize(client, settings)
    meeting_id, revision, snapshot, _segment_ids = seed_cited_notes(settings.data_dir)
    broken = f"/meetings/{meeting_id}?notes_source={snapshot['id']}&segment={'f' * 32}"
    markdown = f"## Overview\n\n- Kept [00:00:09]({broken}) as typed\n"
    warnings = [{"code": "source_link_invalid", "reason": "unknown_segment", "href": broken}]

    saved = await client.patch(
        f"/api/notes/{revision['id']}",
        headers=headers,
        json={"expected_edit_version": 1, "markdown": markdown},
    )

    assert saved.status_code == 200
    detail = (await client.get(f"/api/notes/{revision['id']}")).json()
    listed = (await client.get(f"/api/meetings/{meeting_id}/notes")).json()["revisions"][0]
    for body in (saved.json(), detail, listed):
        assert body["markdown"] == markdown and body["warnings"] == warnings
    export = await client.get(f"/api/notes/{revision['id']}/export")
    assert export.status_code == 200 and export.text == markdown


async def test_source_edit_routes_answer_409_until_the_stopped_worker_releases_the_lock(
    client, settings
):
    headers = await _authorize(client, settings)
    meeting_id, generation_id, segment_ids = seed_completed_transcript(settings.data_dir)
    riley = add_current_generation_speaker(
        settings.data_dir,
        meeting_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        display_name="Riley",
    )
    accepted = accept_notes_task(
        settings.data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="api-lock-request-001",
        expected_generation_id=generation_id,
        expected_source_version=riley["source_version"],
        expected_metadata_version=1,
        worker_ready=True,
    )
    assert claim_next_task(settings.data_dir, owner_id="api-lock-worker") is not None
    meeting = f"/api/meetings/{meeting_id}"
    generation = f"{meeting}/transcript-generations/{generation_id}"
    edit_text = (
        "PATCH",
        f"{meeting}/transcript-segments/{segment_ids[0]}/text",
        {
            "expected_generation_id": generation_id,
            "expected_source_version": 2,
            "text": "Locked out.",
        },
    )
    locked_requests = [
        edit_text,
        (
            "PATCH",
            f"{meeting}/transcript-segments/{segment_ids[0]}/speaker",
            {
                "expected_generation_id": generation_id,
                "expected_source_version": 2,
                "expected_assignment_version": 1,
                "speaker_id": riley["speaker_id"],
            },
        ),
        ("POST", f"{generation}/speakers", {"expected_source_version": 2, "display_name": "Casey"}),
        (
            "PATCH",
            f"{generation}/speakers/SPEAKER_00",
            {"expected_source_version": 2, "display_name": "Dana"},
        ),
        ("PATCH", meeting, {"expected_metadata_version": 1, "meeting_date": "2026-10-20"}),
        ("PATCH", meeting, {"expected_metadata_version": 1, "meeting_date": None}),
    ]

    async def answers() -> list[tuple[int, str]]:
        responses = [
            await client.request(method, url, headers=headers, json=body)
            for method, url, body in locked_requests
        ]
        return [(item.status_code, item.json()["detail"]["code"]) for item in responses]

    refused = [(409, "notes_source_locked")] * len(locked_requests)
    assert await answers() == refused  # running
    stopping = await client.post(f"/api/tasks/{accepted['id']}/cancel", headers=headers)
    assert stopping.status_code == 200 and stopping.json()["status"] == "stopping"
    # The cancel request is not the worker having stopped.
    assert await answers() == refused
    assert (await client.get(meeting)).json()["notes_source_locked"] is True
    assert (await client.get(f"{meeting}/notes")).json()["source_locked"] is True
    title = await client.patch(
        meeting, headers=headers, json={"expected_metadata_version": 1, "title": "Still free"}
    )
    assert title.status_code == 200

    assert finish_canceled_task(
        settings.data_dir, accepted["id"], owner_id="api-lock-worker", elapsed_seconds=0.1
    )
    assert (await client.get(meeting)).json()["notes_source_locked"] is False
    assert (await client.get(f"{meeting}/notes")).json()["source_locked"] is False
    method, url, body = edit_text
    released = await client.request(method, url, headers=headers, json=body)
    assert released.status_code == 200 and released.json()["source_version"] == 3
