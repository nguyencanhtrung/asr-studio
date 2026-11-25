from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from notes_helpers import seed_cited_notes, seed_completed_transcript
from seeding import prepare_diarization_model, prepare_recording, seed_meeting

from asr_studio import routes
from asr_studio.app import create_app
from asr_studio.asr_models import write_default_model
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.runtime import RuntimeProfile, runtime_profile_path, write_runtime_profile
from asr_studio.storage import database_path
from asr_studio.tasks import (
    accept_transcription_task,
    claim_next_task,
    publish_complete_generation,
    transcription_state,
    update_task_progress,
)
from asr_studio.worker import (
    acquire_transcription_lock,
    acquire_worker_lock,
    write_heartbeat,
    write_transcription_heartbeat,
)

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
                assert running.status_code == 200
                assert running.json()["kind"] == "transcription"
                assert running.json()["diarization_mode"] == "community-1"
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


async def test_asr_models_route_requires_auth_and_matches_meeting_choices(tmp_path, monkeypatch):
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
            refused = await client.get("/api/asr-models")
            assert refused.status_code == 401
            headers = await _authorize(client, settings)
            meeting_id = seed_meeting(settings.data_dir, "Model catalog")

            catalog = await client.get("/api/asr-models", headers=headers)
            meeting = await client.get(f"/api/meetings/{meeting_id}", headers=headers)
            assert catalog.status_code == 200
            assert meeting.status_code == 200
            # The settings catalog adds install state and sizes to what a meeting offers.
            install_keys = {"install", "required_bytes", "free_bytes"}
            assert [
                {k: v for k, v in choice.items() if k not in install_keys}
                for choice in catalog.json()["asr_models"]
            ] == meeting.json()["transcription"]["asr_models"]


def _admission_state(data_dir):
    with closing(sqlite3.connect(database_path(data_dir))) as connection, connection:
        connection.execute("BEGIN")
        return (
            connection.execute("SELECT id, current_generation_id FROM meetings").fetchall(),
            connection.execute("SELECT * FROM transcript_generations ORDER BY id").fetchall(),
            connection.execute("SELECT * FROM transcript_segments ORDER BY id").fetchall(),
            connection.execute("SELECT * FROM tasks ORDER BY id").fetchall(),
            connection.execute("SELECT * FROM processing_slot").fetchall(),
            connection.execute("SELECT * FROM notes_sources ORDER BY id").fetchall(),
            connection.execute("SELECT * FROM notes_revisions ORDER BY id").fetchall(),
        )


async def test_diarization_capability_route_is_authoritative_and_authenticated(tmp_path):
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
            unauthorized = await client.get("/api/diarization-capability")
            assert unauthorized.status_code == 401
            headers = await _authorize(client, settings)
            meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
            prepare_recording(settings.data_dir, meeting_id)
            before = _admission_state(settings.data_dir)
            lock_fd = acquire_transcription_lock(settings.data_dir)
            assert lock_fd is not None
            try:
                write_transcription_heartbeat(settings.data_dir, "idle")
                response = await client.get("/api/diarization-capability", headers=headers)
                assert response.status_code == 200
                capability = response.json()
                assert capability["community_1"]["operator_prepared"] is True
                for key, mode in (("community_1", "community-1"), ("off", "off")):
                    refused = await client.post(
                        "/api/tasks",
                        headers=headers,
                        json={
                            "meeting_id": meeting_id,
                            "request_id": f"missing-profile-{mode}",
                            "expected_generation_id": generation_id,
                            "diarization_mode": mode,
                        },
                    )
                    assert refused.status_code == 503
                    assert refused.json()["detail"]["code"] == "worker_unavailable"
                    assert capability[key]["ready"] is False
                    assert capability[key]["reason"] == "worker_unavailable"
                assert _admission_state(settings.data_dir) == before

                write_runtime_profile(
                    settings.data_dir,
                    RuntimeProfile(
                        device="cpu", compute_type="int8", model_path=settings.asr_model_path
                    ),
                )
                response = await client.get("/api/diarization-capability", headers=headers)
                assert response.status_code == 200
                assert response.json() == {
                    "contract_version": 1,
                    "default_mode": "community-1",
                    "explicit_mode_scope": "manual_transcript_rerun",
                    "community_1": {
                        "id": "community-1",
                        "ready": True,
                        "state": "ready",
                        "reason": None,
                        "operator_prepared": True,
                    },
                    "off": {
                        "id": "off",
                        "ready": True,
                        "state": "ready",
                        "reason": None,
                        "allowed_on": "manual_transcript_rerun",
                    },
                }
            finally:
                os.close(lock_fd)

            response = await client.get("/api/diarization-capability", headers=headers)
            assert response.status_code == 200
            for mode in ("community_1", "off"):
                assert response.json()[mode]["ready"] is False
                assert response.json()[mode]["reason"] == "worker_unavailable"


@pytest.mark.parametrize(
    "file_change", ["missing", "lfs_pointer", "empty", "directory_loop", "asset_loop"]
)
async def test_submit_refuses_stale_community_readiness_without_creating_a_task(
    tmp_path, file_change
):
    snapshot = prepare_diarization_model(tmp_path / "community-snapshot")
    community = tmp_path / "diarization"
    community.symlink_to(snapshot, target_is_directory=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=_prepared_model(tmp_path / "asr"),
        asr_qwen_model_path=tmp_path / "qwen",
        diarization_model_path=community,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            headers = await _authorize(client, settings)
            meeting_id, generation_id, segment_ids = seed_completed_transcript(
                settings.data_dir, title="Stale Community-1 readiness fixture"
            )
            _, revision, _, _ = seed_cited_notes(
                settings.data_dir, existing=(meeting_id, generation_id, segment_ids)
            )
            prepare_recording(settings.data_dir, meeting_id)
            before = _admission_state(settings.data_dir)
            lock_fd = acquire_transcription_lock(settings.data_dir)
            assert lock_fd is not None
            try:
                write_runtime_profile(
                    settings.data_dir,
                    RuntimeProfile(
                        device="cpu", compute_type="int8", model_path=settings.asr_model_path
                    ),
                )
                write_transcription_heartbeat(settings.data_dir, "idle")
                capability = await client.get("/api/diarization-capability", headers=headers)
                assert capability.status_code == 200
                assert capability.json()["community_1"]["ready"] is True
                assert capability.json()["off"]["ready"] is True
                model = community / "embedding" / "pytorch_model.bin"
                if file_change == "missing":
                    model.unlink()
                elif file_change == "lfs_pointer":
                    model.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid fixture\n")
                elif file_change == "directory_loop":
                    community.unlink()
                    community.symlink_to(community, target_is_directory=True)
                elif file_change == "asset_loop":
                    model.unlink()
                    model.symlink_to(model)
                else:
                    model.write_bytes(b"")
                payload = {
                    "meeting_id": meeting_id,
                    "request_id": f"catalog-stale-community-{file_change}",
                    "expected_generation_id": generation_id,
                    "diarization_mode": "community-1",
                }
                capability = await client.get("/api/diarization-capability", headers=headers)
                assert capability.status_code == 200
                refused = await client.post("/api/tasks", headers=headers, json=payload)
                community_1 = capability.json()["community_1"]
                assert (community_1["ready"], refused.status_code) == (False, 503)
                assert community_1 == {
                    "id": "community-1",
                    "ready": False,
                    "state": "unavailable",
                    "reason": "model_unavailable",
                    "operator_prepared": False,
                }
                assert refused.json() == {
                    "detail": {
                        "code": "diarization_model_unavailable",
                        "message": "The configured local Community-1 model is not prepared.",
                    }
                }
                assert _admission_state(settings.data_dir) == before

                meeting = await client.get(f"/api/meetings/{meeting_id}", headers=headers)
                assert meeting.status_code == 200
                generation = meeting.json()["transcription"]["generation"]
                assert generation["id"] == generation_id
                assert generation["segments"][0]["text"] == "We need a launch plan."
                notes = await client.get(f"/api/notes/{revision['id']}/export", headers=headers)
                assert notes.status_code == 200
                assert notes.text == revision["markdown"]

                assert capability.json()["off"]["ready"] is True
                off_payload = {
                    **payload,
                    "request_id": f"off-{file_change}",
                    "diarization_mode": "off",
                }
                asr_weights = settings.asr_model_path / "model.bin"
                saved_weights = asr_weights.rename(tmp_path / "saved-asr.bin")
                unavailable = await client.post("/api/tasks", headers=headers, json=off_payload)
                assert unavailable.status_code == 503
                assert unavailable.json()["detail"]["code"] == "model_unavailable"
                assert _admission_state(settings.data_dir) == before
                saved_weights.rename(asr_weights)

                accepted = await client.post("/api/tasks", headers=headers, json=off_payload)
                assert accepted.status_code == 202
                task = await client.get(f"/api/tasks/{accepted.json()['id']}", headers=headers)
                assert task.status_code == 200
                assert task.json()["kind"] == "transcription"
                assert task.json()["diarization_mode"] == "off"

                runtime_profile_path(settings.data_dir).unlink()
                after_accept = _admission_state(settings.data_dir)
                replay = await client.post("/api/tasks", headers=headers, json=off_payload)
                assert replay.status_code == 202
                assert replay.json() == accepted.json()
                assert _admission_state(settings.data_dir) == after_accept
            finally:
                os.close(lock_fd)


@pytest.mark.parametrize("mode", ["community-1", "off"])
async def test_explicit_rerun_rejects_weights_lost_after_readiness(tmp_path, mode):
    selected = _prepared_model(tmp_path / "small")
    default = _prepared_model(tmp_path / "default")
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_cpu_model_path=selected,
        asr_model_path=default,
        asr_gpu_model_path=default,
        diarization_model_path=prepare_diarization_model(tmp_path / "community"),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        headers = await _authorize(client, settings)
        meeting, generation, segments = seed_completed_transcript(settings.data_dir)
        _, revision, _, _ = seed_cited_notes(
            settings.data_dir, existing=(meeting, generation, segments)
        )
        prepare_recording(settings.data_dir, meeting)
        before = _admission_state(settings.data_dir)
        lock_fd = acquire_transcription_lock(settings.data_dir)
        assert lock_fd is not None
        try:
            write_runtime_profile(settings.data_dir, RuntimeProfile("cpu", "int8", default))
            write_transcription_heartbeat(settings.data_dir, "idle")
            meeting_url = f"/api/meetings/{meeting}"
            response = await client.get(meeting_url, headers=headers)
            assert response.status_code == 200
            initial = response.json()["transcription"]
            choices = {choice["id"]: choice for choice in initial["asr_models"]}
            assert choices["whisper-small"]["ready"] is True
            assert choices["auto"]["ready"] is True

            (selected / "model.bin").write_bytes(b"")
            refreshed = await client.get(meeting_url, headers=headers)
            assert refreshed.status_code == 200
            transcription = refreshed.json()["transcription"]
            choices = {choice["id"]: choice for choice in transcription["asr_models"]}
            refused = await client.post(
                "/api/tasks",
                headers=headers,
                json={
                    "meeting_id": meeting,
                    "expected_generation_id": generation,
                    "request_id": "unusable-asr-model",
                    "asr_model": "whisper-small",
                    "diarization_mode": mode,
                },
            )

            assert (choices["whisper-small"]["ready"], refused.status_code) == (False, 503)
            assert choices["whisper-small"]["reason"] == "not_prepared"
            assert choices["auto"]["ready"] is True
            assert refused.json()["detail"]["code"] == "model_unavailable"
            assert _admission_state(settings.data_dir) == before
            assert transcription["generation"] == initial["generation"]
            notes = await client.get(f"/api/notes/{revision['id']}/export", headers=headers)
            assert notes.status_code == 200
            assert notes.text == revision["markdown"]
        finally:
            os.close(lock_fd)


@pytest.mark.parametrize(
    ("mode", "choice"),
    [("community-1", "whisper-small"), ("off", "whisper-small"), (None, "auto")],
)
async def test_model_directory_loop_preserves_exact_post_retries(tmp_path, mode, choice):
    selected = tmp_path / "selected"
    snapshot = _prepared_model(tmp_path / "snapshot")
    selected.symlink_to(snapshot, target_is_directory=True)
    alternate = _prepared_model(tmp_path / "alternate")
    community = prepare_diarization_model(tmp_path / "community")
    diarizer = tmp_path / "diarization"
    diarizer.symlink_to(community, target_is_directory=True)
    uses_small = choice == "whisper-small"
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_cpu_model_path=selected if uses_small else alternate,
        asr_gpu_model_path=alternate if uses_small else selected,
        asr_phowhisper_model_path=tmp_path / "pho",
        asr_qwen_model_path=tmp_path / "qwen",
        diarization_model_path=diarizer,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        headers = await _authorize(client, settings)
        meeting, source, segments = seed_completed_transcript(settings.data_dir)
        seed_cited_notes(settings.data_dir, existing=(meeting, source, segments))
        prepare_recording(settings.data_dir, meeting)
        payload = {"meeting_id": meeting, "request_id": "original-recipe"}
        if mode is not None:
            payload.update(asr_model=choice, expected_generation_id=source, diarization_mode=mode)
        lock_fd = acquire_transcription_lock(settings.data_dir)
        assert lock_fd is not None
        try:
            write_runtime_profile(settings.data_dir, RuntimeProfile("cpu", "int8", alternate))
            write_transcription_heartbeat(settings.data_dir, "idle")
            if choice == "auto":
                write_default_model(settings, "whisper-large-v3")
            accepted = await client.post("/api/tasks", headers=headers, json=payload)
            assert accepted.status_code == 202
            receipt = accepted.json()
            assert receipt["status"] == "accepted"
            assert receipt["model_name"] == "snapshot"
            assert receipt["asr_model"] == (choice if uses_small else "whisper-large-v3")
            assert receipt["diarization_mode"] == (mode or "community-1")

            selected.unlink()
            selected.symlink_to(selected, target_is_directory=True)
            profile_path = runtime_profile_path(settings.data_dir)
            profile_path.chmod(0o640)  # Reading the current profile would chmod it back to 0600.
            before_retry = _admission_state(settings.data_dir)
            replay = await client.post("/api/tasks", headers=headers, json=payload)
            assert (replay.status_code, replay.json()) == (202, receipt)
            assert profile_path.stat().st_mode & 0o777 == 0o640
            assert _admission_state(settings.data_dir) == before_retry

            owner = "model-loop-fixture"
            task_id = receipt["id"]
            claimed = claim_next_task(settings.data_dir, owner_id=owner)
            assert claimed and claimed["id"] == task_id
            assert claimed["model_path"] == str(snapshot)
            assert claimed["diarization_model_path"] == ("" if mode == "off" else str(community))
            assert update_task_progress(
                settings.data_dir,
                task_id,
                owner_id=owner,
                stage="publishing",
                elapsed_seconds=1.0,
                transcript_end_seconds=0.2,
                segment_count=1,
            )
            generation = publish_complete_generation(
                settings.data_dir,
                task_id,
                owner_id=owner,
                segments=[
                    {"id": "d" * 32, "start_seconds": 0.0, "end_seconds": 0.2, "text": "New."}
                ],
                alignment_policy=None if mode == "off" else "community-1-exclusive-max-overlap-v1",
                diarization_model_name=None if mode == "off" else "Community-1",
                language_probability=None,
                elapsed_seconds=1.0,
            )
            assert generation and generation["id"] != source

            # Even the completed replay cannot consult the installed diarizer or new runtime.
            diarizer.unlink()
            diarizer.symlink_to(diarizer, target_is_directory=True)
            before_rejection = _admission_state(settings.data_dir)
            completed = await client.post("/api/tasks", headers=headers, json=payload)
            task = await client.get(f"/api/tasks/{task_id}")
            assert (completed.status_code, completed.json()) == (202, task.json())
            assert completed.json()["status"] == "succeeded"
            assert completed.json()["model_name"] == receipt["model_name"]
            conflict = await client.post(
                "/api/tasks", headers=headers, json={**payload, "language_hint": "vi"}
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "idempotency_conflict"
            assert profile_path.stat().st_mode & 0o777 == 0o640

            fresh = {**payload, "request_id": "fresh-recipe"}
            if mode is not None:
                fresh["expected_generation_id"] = generation["id"]
            refused = await client.post("/api/tasks", headers=headers, json=fresh)
            assert (refused.status_code, refused.json()) == (
                503,
                {
                    "detail": {
                        "code": "model_unavailable",
                        "message": "The selected local ASR model is not prepared.",
                    }
                },
            )
            response = await client.get("/api/asr-models")
            assert response.status_code == 200
            choices = {row["id"]: row for row in response.json()["asr_models"]}
            broken_choices = ["whisper-small"] if uses_small else ["auto", "whisper-large-v3"]
            for broken in broken_choices:
                assert (choices[broken]["ready"], choices[broken]["reason"]) == (
                    False,
                    "not_prepared",
                )
            assert choices["whisper-large-v3" if uses_small else "whisper-small"]["ready"] is True
            assert _admission_state(settings.data_dir) == before_rejection
        finally:
            os.close(lock_fd)


@pytest.mark.parametrize("mode", ["community-1", "off"])
@pytest.mark.parametrize("loss", ["missing", "unreadable", "fifo", "empty"])
async def test_new_explicit_rerun_requires_original_recording(tmp_path, monkeypatch, mode, loss):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        asr_model_path=_prepared_model(tmp_path / "asr"),
        diarization_model_path=prepare_diarization_model(tmp_path / "community"),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        headers = await _authorize(client, settings)
        meeting, generation, segments = seed_completed_transcript(settings.data_dir)
        edit = await client.patch(
            f"/api/meetings/{meeting}/transcript-segments/{segments[0]}/text",
            headers=headers,
            json={
                "expected_generation_id": generation,
                "expected_source_version": 1,
                "text": "A saved correction.",
            },
        )
        assert edit.status_code == 200
        seed_cited_notes(
            settings.data_dir, existing=(meeting, generation, segments), source_version=2
        )
        audio = prepare_recording(settings.data_dir, meeting)
        lock_fd = acquire_transcription_lock(settings.data_dir)
        assert lock_fd is not None
        try:
            write_runtime_profile(
                settings.data_dir, RuntimeProfile("cpu", "int8", settings.asr_model_path)
            )
            write_transcription_heartbeat(settings.data_dir, "idle")
            capability = await client.get("/api/diarization-capability", headers=headers)
            assert capability.status_code == 200
            assert capability.json()["community_1"]["ready"] and capability.json()["off"]["ready"]
            request = {
                "meeting_id": meeting,
                "expected_generation_id": generation,
                "diarization_mode": mode,
                "request_id": "recording-refused",
            }
            original_open = os.open

            def deny_recording(path, flags, *args, **kwargs):
                if path == audio:
                    raise PermissionError("fixture audio cannot be opened")
                return original_open(path, flags, *args, **kwargs)

            with closing(sqlite3.connect(database_path(settings.data_dir))) as connection:
                assert connection.execute(
                    "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
                ).fetchone() == (None,)
            before = _admission_state(settings.data_dir)
            with monkeypatch.context() as inaccessible:
                if loss == "unreadable":
                    inaccessible.setattr(os, "open", deny_recording)
                else:
                    backup = tmp_path / "saved.m4a"
                    if loss == "empty":
                        backup.write_bytes(audio.read_bytes())
                        audio.write_bytes(b"")
                    else:
                        audio.rename(backup)
                        if loss == "fifo":
                            os.mkfifo(audio)
                refused = await client.post("/api/tasks", headers=headers, json=request)
                after = _admission_state(settings.data_dir)
                assert (refused.status_code, after == before) == (409, True)
                assert refused.json() == {
                    "detail": {
                        "code": "recording_unavailable",
                        "message": (
                            "The original recording is unavailable. Restore it before rerunning."
                        ),
                    }
                }

            if loss != "unreadable":
                backup.replace(audio)
            accepted_request = {**request, "request_id": "recording-accepted"}
            accepted = await client.post("/api/tasks", headers=headers, json=accepted_request)
            assert accepted.status_code == 202
            assert accepted.json()["diarization_mode"] == mode
            after_accept = _admission_state(settings.data_dir)
            if mode == "community-1":
                (settings.diarization_model_path / "embedding/pytorch_model.bin").write_bytes(b"")
                replay = await client.post("/api/tasks", headers=headers, json=accepted_request)
                assert replay.status_code == 202
                assert replay.json() == accepted.json()
                assert _admission_state(settings.data_dir) == after_accept
            if loss == "empty":
                audio.write_bytes(b"")
            else:
                audio.unlink()
            replay = await client.post("/api/tasks", headers=headers, json=accepted_request)
            assert replay.status_code == 202
            assert replay.json() == accepted.json()
            assert _admission_state(settings.data_dir) == after_accept
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
