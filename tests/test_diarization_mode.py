from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from seeding import prepare_diarization_model, seed_meeting

from asr_studio import routes
from asr_studio.config import Settings
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    TaskError,
    accept_transcription_task,
    claim_next_task,
    get_task,
    publish_complete_generation,
    transcription_state,
    update_task_progress,
)


def _prepared_asr_model(path: Path) -> Path:
    path.mkdir()
    (path / "model.bin").write_bytes(b"fixture-model")
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


def _segment(segment_id: str, **overrides: object) -> dict[str, object]:
    return {
        "id": segment_id,
        "start_seconds": 0.5,
        "end_seconds": 1.5,
        "text": "Fixture transcript.",
        "avg_logprob": -0.2,
        "no_speech_prob": 0.05,
        "compression_ratio": 1.0,
        **overrides,
    }


def _accept(
    data_dir: Path,
    model_dir: Path,
    meeting_id: str,
    request_id: str,
    *,
    diarization_model_path: Path,
    worker_ready: bool = True,
    **options: object,
) -> dict[str, object]:
    return accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id=request_id,
        model_path=model_dir,
        diarization_model_path=diarization_model_path,
        device="cpu",
        compute_type="int8",
        worker_ready=worker_ready,
        **options,
    )


def _publish(
    data_dir: Path,
    task: dict[str, object],
    *,
    owner_id: str,
    segments: list[dict[str, object]],
    speakers: list[dict[str, object]] | None = None,
    alignment_policy: str | None = "community-1-exclusive-max-overlap-v1",
    diarization_model_name: str | None = "Community-1",
) -> dict[str, object]:
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed and claimed["id"] == task["id"]
    assert update_task_progress(
        data_dir,
        str(task["id"]),
        owner_id=owner_id,
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=1.5,
        segment_count=len(segments),
    )
    generation = publish_complete_generation(
        data_dir,
        str(task["id"]),
        owner_id=owner_id,
        segments=segments,
        speakers=speakers or [],
        alignment_policy=alignment_policy,
        diarization_model_name=diarization_model_name,
        language_probability=0.9,
        elapsed_seconds=1.0,
    )
    assert generation is not None
    return generation


def test_schema_21_additive_reads_keep_legacy_provenance_nullable(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    meeting_id = seed_meeting(data_dir, "Schema fixture")
    model_dir = _prepared_asr_model(tmp_path / "asr-model")
    diarization_dir = prepare_diarization_model(tmp_path / "diarization")
    task = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "schema-legacy-001",
        diarization_model_path=diarization_dir,
    )

    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (21,)
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        generation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transcript_generations)")
        }
        assert "diarization_mode" in task_columns
        assert "diarization_mode" in generation_columns
        assert connection.execute(
            "SELECT diarization_mode FROM tasks WHERE id = ?", (task["id"],)
        ).fetchone() == ("community-1",)

    generation = _publish(
        data_dir,
        task,
        owner_id="schema-owner",
        segments=[_segment("a" * 32)],
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_generations SET diarization_mode = NULL WHERE id = ?",
            (generation["id"],),
        )
    assert transcription_state(data_dir, meeting_id)["generation"]["diarization_mode"] is None


def test_explicit_off_is_manual_rerun_only_and_publishes_truthful_no_speaker_generation(tmp_path):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    meeting_id = seed_meeting(data_dir, "Off fixture")
    model_dir = _prepared_asr_model(tmp_path / "asr-model")
    diarization_dir = prepare_diarization_model(tmp_path / "diarization")
    with pytest.raises(TaskError) as fresh_error:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "off-fresh-001",
            diarization_model_path=tmp_path / "missing-diarization",
            diarization_mode="off",
        )
    assert fresh_error.value.code == "diarization_mode_requires_rerun"
    with pytest.raises(TaskError) as invalid_error:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "off-invalid-001",
            diarization_model_path=diarization_dir,
            diarization_mode="invalid",
        )
    assert invalid_error.value.code == "invalid_diarization_mode"

    first_task = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "off-community-001",
        diarization_model_path=diarization_dir,
    )
    first_generation = _publish(
        data_dir,
        first_task,
        owner_id="community-owner",
        segments=[_segment("1" * 32, text="Edited source text.")],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Alice",
                "first_seen_seconds": 0.5,
                "last_seen_seconds": 1.5,
            }
        ],
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, snapshot_json) VALUES (?, ?, ?, 1, 1, ?)",
            (
                "notes-source-fixture",
                meeting_id,
                first_generation["id"],
                json.dumps({"text": "Edited source text."}),
            ),
        )

    off_task = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "off-rerun-001",
        diarization_model_path=tmp_path / "missing-diarization",
        diarization_mode="off",
        expected_generation_id=first_generation["id"],
    )
    assert off_task["diarization_mode"] == "off"
    retry = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "off-rerun-001",
        diarization_model_path=tmp_path / "missing-diarization",
        diarization_mode="off",
        expected_generation_id=first_generation["id"],
    )
    assert retry["id"] == off_task["id"]
    with pytest.raises(TaskError) as generation_conflict:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "off-rerun-001",
            diarization_model_path=tmp_path / "missing-diarization",
            diarization_mode="off",
            expected_generation_id="3" * 32,
        )
    assert generation_conflict.value.code == "idempotency_conflict"
    with pytest.raises(TaskError) as mode_conflict:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "off-rerun-001",
            diarization_model_path=diarization_dir,
            diarization_mode="community-1",
            expected_generation_id=first_generation["id"],
        )
    assert mode_conflict.value.code == "idempotency_conflict"

    off_generation = _publish(
        data_dir,
        off_task,
        owner_id="off-owner",
        segments=[_segment("2" * 32, no_speech_prob=0.8)],
        alignment_policy=None,
        diarization_model_name=None,
    )
    completed_retry = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "off-rerun-001",
        diarization_model_path=tmp_path / "missing-diarization",
        diarization_mode="off",
        expected_generation_id=first_generation["id"],
        worker_ready=False,
    )
    assert completed_retry["id"] == off_task["id"]
    assert completed_retry["status"] == "succeeded"
    state = transcription_state(data_dir, meeting_id)
    assert state["generation"]["id"] == off_generation["id"]
    assert state["generation"]["diarization_mode"] == "off"
    assert state["generation"]["diarization_model_name"] is None
    assert state["generation"]["speakers"] == []
    segment = state["generation"]["segments"][0]
    assert segment["speaker_id"] is None
    assert segment["original_speaker_id"] is None
    assert segment["review_reasons"] == ["asr_uncertain"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT text FROM transcript_segments WHERE generation_id = ?",
            (first_generation["id"],),
        ).fetchone() == ("Edited source text.",)
        assert connection.execute(
            "SELECT snapshot_json FROM notes_sources WHERE id = 'notes-source-fixture'"
        ).fetchone() == ((json.dumps({"text": "Edited source text."})),)


def test_rollback_fence_rejects_new_off_and_does_not_claim_pending_off(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    meeting_id = seed_meeting(data_dir, "Rollback fence fixture")
    model_dir = _prepared_asr_model(tmp_path / "asr-model")
    diarization_dir = prepare_diarization_model(tmp_path / "diarization")

    community_task = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "rollback-community-001",
        diarization_model_path=diarization_dir,
    )
    community_generation = _publish(
        data_dir,
        community_task,
        owner_id="rollback-community-owner",
        segments=[_segment("3" * 32)],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Fixture speaker",
                "first_seen_seconds": 0.5,
                "last_seen_seconds": 1.5,
            }
        ],
    )

    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "1")
    with pytest.raises(TaskError) as admission_error:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "rollback-off-new-001",
            diarization_model_path=tmp_path / "missing-diarization",
            diarization_mode="off",
            expected_generation_id=community_generation["id"],
        )
    assert admission_error.value.code == "diarization_rollback_fence"

    monkeypatch.delenv("ASR_DIARIZATION_ROLLBACK_FENCE")
    pending_off = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "rollback-off-pending-001",
        diarization_model_path=tmp_path / "missing-diarization",
        diarization_mode="off",
        expected_generation_id=community_generation["id"],
    )
    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "true")
    assert claim_next_task(data_dir, owner_id="rollback-old-runtime") is None
    assert get_task(data_dir, str(pending_off["id"]))["status"] == "accepted"

    monkeypatch.delenv("ASR_DIARIZATION_ROLLBACK_FENCE")
    claimed = claim_next_task(data_dir, owner_id="rollback-compatible-runtime")
    assert claimed is not None
    assert claimed["id"] == pending_off["id"]


def test_diarization_capability_reports_real_file_and_worker_readiness(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        diarization_model_path=tmp_path / "community-1",
    )
    monkeypatch.setattr(routes, "worker_is_healthy", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "prepared_diarization_directory",
        lambda path: path,
    )
    ready = routes.diarization_capability(settings)
    assert ready["contract_version"] == 1
    assert ready["community_1"]["ready"] is True
    assert ready["off"]["ready"] is True

    monkeypatch.setattr(
        routes,
        "prepared_diarization_directory",
        lambda _path: (_ for _ in ()).throw(routes.DiarizationConfigurationError()),
    )
    unavailable = routes.diarization_capability(settings)
    assert unavailable["community_1"] == {
        "id": "community-1",
        "ready": False,
        "state": "unavailable",
        "reason": "model_unavailable",
        "operator_prepared": False,
    }

    monkeypatch.setattr(routes, "worker_is_healthy", lambda *_args, **_kwargs: False)
    unavailable = routes.diarization_capability(settings)
    assert unavailable["off"]["ready"] is False
    assert unavailable["off"]["reason"] == "worker_unavailable"

    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "1")
    fenced = routes.diarization_capability(settings)
    assert fenced["off"] == {
        "id": "off",
        "ready": False,
        "state": "unavailable",
        "reason": "rollback_fence",
        "allowed_on": "manual_transcript_rerun",
    }
