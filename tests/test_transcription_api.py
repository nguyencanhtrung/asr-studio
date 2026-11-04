from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from seeding import prepare_diarization_model, seed_meeting

from asr_studio import routes
from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.runtime import RuntimeProfile, write_runtime_profile
from asr_studio.tasks import (
    accept_transcription_task,
    claim_next_task,
    publish_complete_generation,
    transcription_state,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.anyio


def _prepared_model(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(b"model")
    (path / "config.json").write_text(
        json.dumps({"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []}),
        encoding="utf-8",
    )
    (path / "tokenizer.json").write_text(json.dumps({"model": {"type": "BPE"}}), encoding="utf-8")
    (path / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "feature_size": 128,
                "sampling_rate": 16000,
                "hop_length": 160,
                "n_fft": 400,
                "chunk_length": 30,
            }
        ),
        encoding="utf-8",
    )
    return path


async def _authorize(client: AsyncClient, settings: Settings) -> dict[str, str]:
    token = AuthStore(settings.auth_dir).load().token
    headers = {"Origin": settings.public_url}
    response = await client.post("/api/access/session", headers=headers, json={"token": token})
    assert response.status_code == 204
    return headers


async def _run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


async def test_task_routes_accept_poll_cancel_and_rediscover_meeting_state(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "run_in_threadpool", _run_inline)
    diarization_model_dir = prepare_diarization_model(tmp_path / "diarization-model")
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=_prepared_model(tmp_path / "model"),
        diarization_model_path=diarization_model_dir,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            headers = await _authorize(client, settings)
            meeting_id = seed_meeting(settings.data_dir, "Task routes")
            lock_fd = acquire_worker_lock(settings.data_dir)
            assert lock_fd is not None
            write_runtime_profile(
                settings.data_dir,
                RuntimeProfile(
                    device="cuda", compute_type="float16", model_path=settings.asr_model_path
                ),
            )
            write_heartbeat(settings.data_dir, "idle")
            try:
                payload = {"meeting_id": meeting_id, "request_id": "api-request-001"}
                invalid_model = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={**payload, "model_id": "cloud-model", "hf_token": "hidden"},
                )
                assert invalid_model.status_code == 400
                unknown_model = await client.post(
                    "/api/tasks", headers=headers, json={**payload, "asr_model": "cloud-model"}
                )
                assert unknown_model.status_code == 400
                assert unknown_model.json()["detail"]["code"] == "invalid_asr_model"
                unprepared = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={**payload, "asr_model": "pho-whisper-large"},
                )
                assert unprepared.status_code == 503
                assert unprepared.json()["detail"]["code"] == "model_unavailable"
                accepted = await client.post("/api/tasks", headers=headers, json=payload)
                retry = await client.post("/api/tasks", headers=headers, json=payload)
                assert accepted.status_code == 202
                assert retry.status_code == 202
                assert retry.json()["id"] == accepted.json()["id"]
                assert accepted.json()["request_id"] == payload["request_id"]
                assert accepted.json()["worker_state"] == "checking-worker"
                assert accepted.json()["device"] == "cuda"
                assert accepted.json()["compute_type"] == "float16"
                assert accepted.json()["model_name"] == "model"

                detail = (await client.get(f"/api/meetings/{meeting_id}")).json()
                assert detail["transcription"]["task"]["id"] == accepted.json()["id"]
                assert detail["transcription"]["generation"] is None
                renamed = await client.patch(
                    f"/api/meetings/{meeting_id}",
                    headers=headers,
                    json={
                        "expected_metadata_version": detail["metadata_version"],
                        "title": "Task routes renamed",
                    },
                )
                assert renamed.status_code == 200
                # The page replaces its meeting with this answer: it must not lose these.
                reread = (await client.get(f"/api/meetings/{meeting_id}")).json()
                assert (
                    renamed.json()["transcription"]["asr_models"]
                    == (reread["transcription"]["asr_models"])
                )
                assert (
                    renamed.json()["transcription"]["runtime"]
                    == (reread["transcription"]["runtime"])
                )
                choices = {c["id"]: c for c in detail["transcription"]["asr_models"]}
                assert choices["auto"]["ready"] is True
                assert choices["pho-whisper-large"]["reason"] == "not_prepared"
                assert detail["transcription"]["runtime"] == {
                    "device": "cuda",
                    "compute_type": "float16",
                    "model_name": "model",
                }

                claimed = claim_next_task(settings.data_dir, owner_id="api-test-worker")
                assert claimed and claimed["id"] == accepted.json()["id"]
                running = await client.get(f"/api/tasks/{accepted.json()['id']}")
                assert running.json()["status"] == "running"
                assert running.json()["worker_state"] == "healthy"
                stopping = await client.post(
                    f"/api/tasks/{accepted.json()['id']}/cancel", headers=headers, json={}
                )
                assert stopping.json()["status"] == "stopping"
                assert (
                    transcription_state(settings.data_dir, meeting_id)["task"]["status"]
                    == "stopping"
                )
            finally:
                os.close(lock_fd)


async def test_task_route_persists_per_run_language_and_checks_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "run_in_threadpool", _run_inline)
    diarization_model_dir = prepare_diarization_model(tmp_path / "diarization-model")
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=_prepared_model(tmp_path / "model"),
        diarization_model_path=diarization_model_dir,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            headers = await _authorize(client, settings)
            meeting_id = seed_meeting(settings.data_dir, "Per-run language", language_hint="en")
            lock_fd = acquire_worker_lock(settings.data_dir)
            assert lock_fd is not None
            write_runtime_profile(
                settings.data_dir,
                RuntimeProfile(
                    device="cpu", compute_type="int8", model_path=settings.asr_model_path
                ),
            )
            write_heartbeat(settings.data_dir, "idle")
            try:
                payload = {
                    "meeting_id": meeting_id,
                    "request_id": "language-api-001",
                    "language_hint": "vi",
                }
                accepted = await client.post("/api/tasks", headers=headers, json=payload)
                assert accepted.status_code == 202
                assert accepted.json()["language_hint"] == "vi"

                retry = await client.post("/api/tasks", headers=headers, json=payload)
                assert retry.status_code == 202
                assert retry.json()["id"] == accepted.json()["id"]
                assert retry.json()["language_hint"] == "vi"

                conflict = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={**payload, "language_hint": "en"},
                )
                assert conflict.status_code == 409
                assert conflict.json()["detail"]["code"] == "idempotency_conflict"

                invalid = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={
                        "meeting_id": meeting_id,
                        "request_id": "language-api-002",
                        "language_hint": "fr",
                    },
                )
                assert invalid.status_code == 400
                assert invalid.json()["detail"]["code"] == "invalid_language_hint"
            finally:
                os.close(lock_fd)


async def test_speaker_reassignment_api_checks_generation_and_assignment_versions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(routes, "run_in_threadpool", _run_inline)
    diarization_model_dir = prepare_diarization_model(tmp_path / "diarization-model")
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=_prepared_model(tmp_path / "model"),
        diarization_model_path=diarization_model_dir,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            headers = await _authorize(client, settings)
            meeting_id = seed_meeting(settings.data_dir, "Speaker reassignment")
            lock_fd = acquire_worker_lock(settings.data_dir)
            assert lock_fd is not None
            write_runtime_profile(
                settings.data_dir,
                RuntimeProfile(
                    device="cpu", compute_type="int8", model_path=settings.asr_model_path
                ),
            )
            write_heartbeat(settings.data_dir, "idle")
            try:
                accepted = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={"meeting_id": meeting_id, "request_id": "speaker-api-0001"},
                )
                assert accepted.status_code == 202
                task = claim_next_task(settings.data_dir, owner_id="speaker-api-worker")
                assert task and task["id"] == accepted.json()["id"]
                assert update_task_progress(
                    settings.data_dir,
                    task["id"],
                    owner_id="speaker-api-worker",
                    stage="publishing",
                    elapsed_seconds=1,
                    transcript_end_seconds=2,
                    segment_count=1,
                )
                generation = publish_complete_generation(
                    settings.data_dir,
                    task["id"],
                    owner_id="speaker-api-worker",
                    segments=[
                        {
                            "id": "b" * 32,
                            "start_seconds": 1.0,
                            "end_seconds": 2.0,
                            "text": "Sprint planning.",
                            "original_speaker_id": "SPEAKER_00",
                            "speaker_id": "SPEAKER_00",
                        }
                    ],
                    speakers=[
                        {
                            "speaker_id": "SPEAKER_00",
                            "display_name": "Speaker 1",
                            "first_seen_seconds": 0,
                            "last_seen_seconds": 2,
                        },
                        {
                            "speaker_id": "SPEAKER_01",
                            "display_name": "Speaker 2",
                            "first_seen_seconds": 2,
                            "last_seen_seconds": 3,
                        },
                    ],
                    language_probability=0.9,
                    elapsed_seconds=1,
                )
                assert generation is not None
                endpoint = f"/api/meetings/{meeting_id}/transcript-segments/{'b' * 32}/speaker"
                payload = {
                    "expected_generation_id": generation["id"],
                    "expected_source_version": 1,
                    "expected_assignment_version": 1,
                    "speaker_id": "SPEAKER_01",
                }
                reassigned = await client.patch(endpoint, headers=headers, json=payload)
                assert reassigned.status_code == 200
                assert reassigned.json()["original_speaker_id"] == "SPEAKER_00"
                assert reassigned.json()["speaker_id"] == "SPEAKER_01"
                assert reassigned.json()["assignment_version"] == 2
                assert reassigned.json()["source_version"] == 2
                detail = await client.get(f"/api/meetings/{meeting_id}")
                assert detail.json()["transcription"]["generation"]["source_version"] == 2
                saved_segment = detail.json()["transcription"]["generation"]["segments"][0]
                assert saved_segment["original_speaker_id"] == "SPEAKER_00"
                assert saved_segment["speaker_id"] == "SPEAKER_01"

                stale = await client.patch(endpoint, headers=headers, json=payload)
                assert stale.status_code == 409
                assert stale.json()["detail"]["code"] == "source_version_changed"
                stale_assignment = await client.patch(
                    endpoint,
                    headers=headers,
                    json={**payload, "expected_source_version": 2},
                )
                assert stale_assignment.status_code == 409
                assert stale_assignment.json()["detail"]["code"] == "assignment_version_changed"
                outdated_generation = await client.patch(
                    endpoint,
                    headers=headers,
                    json={**payload, "expected_generation_id": "old-generation"},
                )
                assert outdated_generation.status_code == 409

                text_endpoint = f"/api/meetings/{meeting_id}/transcript-segments/{'b' * 32}/text"
                no_op_text = await client.patch(
                    text_endpoint,
                    headers=headers,
                    json={
                        "expected_generation_id": generation["id"],
                        "expected_source_version": 2,
                        "text": "Sprint planning.",
                    },
                )
                assert no_op_text.status_code == 200
                assert no_op_text.json()["source_version"] == 2
                text_edit = await client.patch(
                    text_endpoint,
                    headers=headers,
                    json={
                        "expected_generation_id": generation["id"],
                        "expected_source_version": 2,
                        "text": "Updated transcript text.",
                    },
                )
                assert text_edit.status_code == 200
                assert text_edit.json()["source_version"] == 3

                speaker_base = (
                    f"/api/meetings/{meeting_id}/transcript-generations/{generation['id']}/speakers"
                )
                renamed = await client.patch(
                    f"{speaker_base}/SPEAKER_01",
                    headers=headers,
                    json={"expected_source_version": 3, "display_name": "Alice"},
                )
                assert renamed.status_code == 200
                assert renamed.json()["display_name"] == "Alice"
                assert renamed.json()["source_version"] == 4
                no_op_name = await client.patch(
                    f"{speaker_base}/SPEAKER_01",
                    headers=headers,
                    json={"expected_source_version": 4, "display_name": "Alice"},
                )
                assert no_op_name.status_code == 200
                assert no_op_name.json()["source_version"] == 4
                reset = await client.patch(
                    f"{speaker_base}/SPEAKER_01",
                    headers=headers,
                    json={"expected_source_version": 4, "display_name": " "},
                )
                assert reset.status_code == 200
                assert reset.json()["display_name"] == "Speaker 2"
                assert reset.json()["source_version"] == 5

                duplicate_name = await client.patch(
                    f"{speaker_base}/SPEAKER_00",
                    headers=headers,
                    json={"expected_source_version": 5, "display_name": "Speaker 2"},
                )
                assert duplicate_name.status_code == 409
                assert duplicate_name.json()["detail"]["code"] == "speaker_name_conflict"

                speaker_one_renamed = await client.patch(
                    f"{speaker_base}/SPEAKER_00",
                    headers=headers,
                    json={"expected_source_version": 5, "display_name": "Alice"},
                )
                assert speaker_one_renamed.status_code == 200
                assert speaker_one_renamed.json()["source_version"] == 6
                speaker_two_renamed = await client.patch(
                    f"{speaker_base}/SPEAKER_01",
                    headers=headers,
                    json={"expected_source_version": 6, "display_name": "Speaker 1"},
                )
                assert speaker_two_renamed.status_code == 200
                assert speaker_two_renamed.json()["source_version"] == 7
                reset_with_collision = await client.patch(
                    f"{speaker_base}/SPEAKER_00",
                    headers=headers,
                    json={"expected_source_version": 7, "display_name": " "},
                )
                assert reset_with_collision.status_code == 200
                assert reset_with_collision.json()["display_name"] == "Speaker 2"
                assert reset_with_collision.json()["source_version"] == 8

                added_speaker = await client.post(
                    speaker_base,
                    headers=headers,
                    json={"expected_source_version": 8},
                )
                assert added_speaker.status_code == 200
                assert added_speaker.json()["speaker_id"].startswith("MANUAL_")
                assert added_speaker.json()["display_name"] == "Speaker 3"
                assert added_speaker.json()["source_version"] == 9
                named_speaker = await client.post(
                    speaker_base,
                    headers=headers,
                    json={"expected_source_version": 9, "display_name": "Witness"},
                )
                assert named_speaker.status_code == 200
                assert named_speaker.json()["display_name"] == "Witness"
                assert named_speaker.json()["source_version"] == 10
                blank_named_speaker = await client.post(
                    speaker_base,
                    headers=headers,
                    json={"expected_source_version": 10, "display_name": ""},
                )
                assert blank_named_speaker.status_code == 200
                assert blank_named_speaker.json()["display_name"] == "Speaker 5"
                assert blank_named_speaker.json()["source_version"] == 11
                stale_text = await client.patch(
                    text_endpoint,
                    headers=headers,
                    json={
                        "expected_generation_id": generation["id"],
                        "expected_source_version": 8,
                        "text": "Stale draft.",
                    },
                )
                assert stale_text.status_code == 409
                assert stale_text.json()["detail"]["code"] == "source_version_changed"

                detail = await client.get(f"/api/meetings/{meeting_id}")
                updated_generation = detail.json()["transcription"]["generation"]
                assert updated_generation["source_version"] == 11
                saved_segment = updated_generation["segments"][0]
                assert saved_segment["text"] == "Updated transcript text."
                assert (saved_segment["start_seconds"], saved_segment["end_seconds"]) == (1.0, 2.0)
                assert updated_generation["speakers"][1]["display_name"] == "Speaker 1"
                assert (
                    updated_generation["speakers"][2]["speaker_id"]
                    == added_speaker.json()["speaker_id"]
                )
                assert updated_generation["speakers"][3]["display_name"] == "Witness"
                assert updated_generation["speakers"][4]["display_name"] == "Speaker 5"

                next_task = accept_transcription_task(
                    settings.data_dir,
                    meeting_id=meeting_id,
                    request_id="speaker-api-0002",
                    model_path=settings.asr_model_path,
                    diarization_model_path=settings.diarization_model_path,
                    device="cpu",
                    compute_type="int8",
                    worker_ready=True,
                )
                next_claim = claim_next_task(settings.data_dir, owner_id="speaker-api-worker-2")
                assert next_claim and next_claim["id"] == next_task["id"]
                assert update_task_progress(
                    settings.data_dir,
                    next_task["id"],
                    owner_id="speaker-api-worker-2",
                    stage="publishing",
                    elapsed_seconds=1,
                    transcript_end_seconds=2,
                    segment_count=1,
                )
                next_generation = publish_complete_generation(
                    settings.data_dir,
                    next_task["id"],
                    owner_id="speaker-api-worker-2",
                    segments=[
                        {
                            "id": "c" * 32,
                            "start_seconds": 1,
                            "end_seconds": 2,
                            "text": "New generation.",
                            "original_speaker_id": "SPEAKER_99",
                            "speaker_id": "SPEAKER_99",
                        }
                    ],
                    speakers=[
                        {
                            "speaker_id": "SPEAKER_99",
                            "display_name": "Speaker 1",
                            "first_seen_seconds": 1,
                            "last_seen_seconds": 2,
                        }
                    ],
                    language_probability=0.9,
                    elapsed_seconds=1,
                )
                assert next_generation is not None
                historical_speaker_write = await client.patch(
                    f"{speaker_base}/SPEAKER_01",
                    headers=headers,
                    json={"expected_source_version": 11, "display_name": "Historical edit"},
                )
                assert historical_speaker_write.status_code == 409
                assert historical_speaker_write.json()["detail"]["code"] == (
                    "current_generation_changed"
                )
            finally:
                os.close(lock_fd)


async def test_task_api_rejects_missing_worker_model_and_invalid_request(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "run_in_threadpool", _run_inline)
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=tmp_path / "missing-model",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            headers = await _authorize(client, settings)
            meeting_id = seed_meeting(settings.data_dir, "No model")
            unavailable_worker = await client.post(
                "/api/tasks",
                headers=headers,
                json={"meeting_id": meeting_id, "request_id": "missing-worker"},
            )
            assert unavailable_worker.status_code == 503
            assert unavailable_worker.json()["detail"]["code"] == "worker_unavailable"

            lock_fd = acquire_worker_lock(settings.data_dir)
            assert lock_fd is not None
            write_runtime_profile(
                settings.data_dir,
                RuntimeProfile(
                    device="cpu", compute_type="int8", model_path=settings.asr_model_path
                ),
            )
            write_heartbeat(settings.data_dir, "idle")
            try:
                unavailable_model = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={"meeting_id": meeting_id, "request_id": "missing-model"},
                )
                invalid_request = await client.post(
                    "/api/tasks",
                    headers=headers,
                    json={"meeting_id": meeting_id, "request_id": "bad"},
                )
                assert unavailable_model.status_code == 503
                assert unavailable_model.json()["detail"]["code"] == "model_unavailable"
                assert invalid_request.status_code == 400
                assert invalid_request.json()["detail"]["code"] == "invalid_request_id"
            finally:
                os.close(lock_fd)
