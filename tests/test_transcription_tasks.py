from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from seeding import prepare_diarization_model, seed_meeting

from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    TaskError,
    accept_transcription_task,
    add_current_generation_speaker,
    cancel_task,
    claim_next_task,
    fail_task,
    finish_canceled_task,
    finish_interrupted_task,
    publish_complete_generation,
    reassign_current_generation_speaker,
    recover_abandoned_tasks,
    transcription_state,
    update_current_generation_segment_text,
    update_current_generation_speaker_name,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock


def _task_store(tmp_path: Path, *, meeting_count: int = 1):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text(
        json.dumps({"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []}),
        encoding="utf-8",
    )
    (model_dir / "tokenizer.json").write_text(
        json.dumps({"model": {"type": "BPE"}}), encoding="utf-8"
    )
    (model_dir / "preprocessor_config.json").write_text(
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
    prepare_diarization_model(tmp_path / "diarization-model")
    meetings = [seed_meeting(data_dir, f"Meeting {index}") for index in range(meeting_count)]
    return data_dir, model_dir, meetings


def _accept(
    data_dir: Path,
    model_dir: Path,
    meeting_id: str,
    request_id: str,
    *,
    language_hint: str | None = None,
):
    options = {}
    if language_hint is not None:
        options["language_hint"] = language_hint
    return accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id=request_id,
        model_path=model_dir,
        **options,
        diarization_model_path=data_dir.parent / "diarization-model",
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )


def _segment(segment_id: str, **overrides):
    return {
        "id": segment_id,
        "start_seconds": 1.25,
        "end_seconds": 2.5,
        "text": "A saved utterance.",
        "avg_logprob": -0.2,
        "no_speech_prob": 0.05,
        "compression_ratio": 1.0,
        **overrides,
    }


def _publish_generation(
    data_dir: Path,
    model_dir: Path,
    meeting_id: str,
    request_id: str,
    owner_id: str,
    segments: list[dict],
    speakers: list[dict],
):
    task = _accept(data_dir, model_dir, meeting_id, request_id)
    claim_next_task(data_dir, owner_id=owner_id)
    assert update_task_progress(
        data_dir,
        task["id"],
        owner_id=owner_id,
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=max(segment["end_seconds"] for segment in segments),
        segment_count=len(segments),
    )
    return publish_complete_generation(
        data_dir,
        task["id"],
        owner_id=owner_id,
        segments=segments,
        speakers=speakers,
        language_probability=0.9,
        elapsed_seconds=1.0,
    )


def test_request_id_returns_original_task_and_rejects_payload_reuse(tmp_path):
    data_dir, model_dir, (first_meeting, second_meeting) = _task_store(tmp_path, meeting_count=2)
    accepted = _accept(data_dir, model_dir, first_meeting, "request-0001")

    model_loop = tmp_path / "changed-server-model"
    diarizer_loop = tmp_path / "changed-server-diarizer"
    model_loop.symlink_to(model_loop, target_is_directory=True)
    diarizer_loop.symlink_to(diarizer_loop, target_is_directory=True)
    retry = accept_transcription_task(
        data_dir,
        meeting_id=first_meeting,
        request_id="request-0001",
        model_path=model_loop,
        diarization_model_path=diarizer_loop,
        device="cuda",
        compute_type="float16",
        worker_ready=False,
    )
    assert retry["id"] == accepted["id"]
    assert retry["status"] == "accepted"
    assert retry["request_id"] == "request-0001"

    with pytest.raises(TaskError) as error:
        _accept(data_dir, model_dir, second_meeting, "request-0001")
    assert error.value.code == "idempotency_conflict"


def test_rerun_language_is_pinned_per_task_and_generation(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    first = _accept(data_dir, model_dir, meeting_id, "language-run-001")
    assert first["language_hint"] == "en"
    assert first["model_name"] == "model"
    assert first["device"] == "cpu"
    assert first["compute_type"] == "int8"

    same_default = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "language-run-001",
        language_hint="en",
    )
    assert same_default["id"] == first["id"]
    with pytest.raises(TaskError) as conflict:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "language-run-001",
            language_hint="vi",
        )
    assert conflict.value.code == "idempotency_conflict"

    claim_next_task(data_dir, owner_id="language-owner-1")
    assert update_task_progress(
        data_dir,
        first["id"],
        owner_id="language-owner-1",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=2.5,
        segment_count=1,
    )
    first_generation = publish_complete_generation(
        data_dir,
        first["id"],
        owner_id="language-owner-1",
        segments=[
            _segment(
                "1" * 32,
                original_speaker_id="SPEAKER_00",
                speaker_id="SPEAKER_00",
                text="English generation.",
            )
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Alice",
                "first_seen_seconds": 1.0,
                "last_seen_seconds": 2.5,
            }
        ],
        language_probability=0.9,
        elapsed_seconds=1.0,
    )
    assert first_generation is not None

    second = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "language-run-002",
        language_hint="vi",
    )
    assert second["language_hint"] == "vi"
    assert second["model_name"] == "model"
    assert second["device"] == "cpu"
    assert second["compute_type"] == "int8"
    claim_next_task(data_dir, owner_id="language-owner-2")
    assert update_task_progress(
        data_dir,
        second["id"],
        owner_id="language-owner-2",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=2.5,
        segment_count=1,
    )
    second_generation = publish_complete_generation(
        data_dir,
        second["id"],
        owner_id="language-owner-2",
        segments=[
            _segment(
                "2" * 32,
                original_speaker_id="SPEAKER_99",
                speaker_id="SPEAKER_99",
                text="Vietnamese generation.",
            )
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_99",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1.0,
                "last_seen_seconds": 2.5,
            }
        ],
        language_probability=0.8,
        elapsed_seconds=1.0,
    )
    assert second_generation is not None

    state = transcription_state(data_dir, meeting_id)
    assert state["generation"]["id"] == second_generation["id"]
    assert state["generation"]["language_hint"] == "vi"
    assert state["generation"]["model_name"] == "model"
    assert state["generation"]["segments"][0]["id"] == "2" * 32
    assert state["generation"]["speakers"][0]["speaker_id"] == "SPEAKER_99"
    assert state["generation"]["speakers"][0]["display_name"] == "Speaker 1"
    assert state["generation_history"][0]["id"] == first_generation["id"]
    assert state["generation_history"][0]["language_hint"] == "en"
    assert state["generation_history"][0]["segments"][0]["id"] == "1" * 32
    assert state["generation_history"][0]["speakers"][0]["display_name"] == "Alice"
    with sqlite3.connect(database_path(data_dir)) as connection:
        persisted = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT request_id, language_hint, model_name, device, compute_type "
                "FROM tasks WHERE request_id IN (?, ?)",
                ("language-run-001", "language-run-002"),
            )
        }
    assert persisted == {
        "language-run-001": ("en", "model", "cpu", "int8"),
        "language-run-002": ("vi", "model", "cpu", "int8"),
    }

    failed = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "language-run-003",
        language_hint="en",
    )
    claim_next_task(data_dir, owner_id="language-owner-3")
    assert fail_task(
        data_dir,
        failed["id"],
        owner_id="language-owner-3",
        error_code="transcription_failed",
        message="Runner failed before publication.",
        elapsed_seconds=1.0,
    )
    assert transcription_state(data_dir, meeting_id)["generation"]["id"] == second_generation["id"]

    with pytest.raises(TaskError) as invalid:
        _accept(
            data_dir,
            model_dir,
            meeting_id,
            "language-run-invalid",
            language_hint="fr",
        )
    assert invalid.value.code == "invalid_language_hint"


def test_canceled_rerun_preserves_edited_current_generation(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    first = _publish_generation(
        data_dir,
        model_dir,
        meeting_id,
        "cancel-rerun-001",
        "cancel-rerun-owner-1",
        [
            _segment(
                "a" * 32,
                text="Original transcript.",
                original_speaker_id="SPEAKER_00",
                speaker_id="SPEAKER_00",
            )
        ],
        [
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1.25,
                "last_seen_seconds": 2.5,
            }
        ],
    )
    assert first is not None
    update_current_generation_segment_text(
        data_dir,
        meeting_id,
        "a" * 32,
        expected_generation_id=first["id"],
        expected_source_version=1,
        text="Edited original transcript.",
    )
    update_current_generation_speaker_name(
        data_dir,
        meeting_id,
        "SPEAKER_00",
        expected_generation_id=first["id"],
        expected_source_version=2,
        display_name="Alice",
    )

    rerun = _accept(
        data_dir,
        model_dir,
        meeting_id,
        "cancel-rerun-002",
        language_hint="vi",
    )
    claim_next_task(data_dir, owner_id="cancel-rerun-owner-2")
    assert cancel_task(data_dir, rerun["id"])["status"] == "stopping"
    assert (
        publish_complete_generation(
            data_dir,
            rerun["id"],
            owner_id="cancel-rerun-owner-2",
            segments=[_segment("b" * 32, text="Late rerun output.")],
            speakers=[
                {
                    "speaker_id": "SPEAKER_99",
                    "display_name": "Speaker 1",
                    "first_seen_seconds": 1.25,
                    "last_seen_seconds": 2.5,
                }
            ],
            language_probability=0.8,
            elapsed_seconds=2.0,
        )
        is None
    )
    before_reap = transcription_state(data_dir, meeting_id)
    assert before_reap["generation"]["id"] == first["id"]
    assert before_reap["generation"]["segments"][0]["text"] == "Edited original transcript."
    assert before_reap["generation"]["speakers"][0]["display_name"] == "Alice"
    assert before_reap["generation_history"] == []

    assert finish_canceled_task(
        data_dir, rerun["id"], owner_id="cancel-rerun-owner-2", elapsed_seconds=3.0
    )
    after_reap = transcription_state(data_dir, meeting_id)
    assert after_reap["task"]["status"] == "canceled"
    assert after_reap["generation"]["id"] == first["id"]


def test_server_computes_stale_worker_state_for_active_task(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "heartbeat-0001")
    assert task["worker_state"] == "checking-worker"
    claimed = claim_next_task(data_dir, owner_id="worker-owner")
    assert claimed and claimed["id"] == task["id"]
    assert transcription_state(data_dir, meeting_id)["task"]["worker_state"] == "healthy"

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET heartbeat_at = '2000-01-01 00:00:00' WHERE id = ?",
            (task["id"],),
        )
    stale = transcription_state(data_dir, meeting_id)["task"]
    assert stale["worker_state"] == "checking-worker"


def test_task_summary_exposes_bounded_pipeline_progress_without_audio_ratio(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "progress-summary-0001")
    claimed = claim_next_task(data_dir, owner_id="progress-owner")
    assert claimed and claimed["id"] == task["id"]
    assert update_task_progress(
        data_dir,
        task["id"],
        owner_id="progress-owner",
        stage="chunking",
        elapsed_seconds=4.0,
        transcript_end_seconds=10.0,
        segment_count=2,
        progress_current=2,
        progress_total=5,
        progress_unit="chunks",
    )
    summary = transcription_state(data_dir, meeting_id)["task"]
    assert summary["progress"] == {"current": 2, "total": 5, "unit": "chunks"}


def test_concurrent_acceptance_claims_only_one_global_slot(tmp_path):
    data_dir, model_dir, meetings = _task_store(tmp_path, meeting_count=2)
    barrier = threading.Barrier(2)

    def attempt(index: int):
        barrier.wait(timeout=5)
        try:
            return _accept(data_dir, model_dir, meetings[index], f"request-{index:04d}")
        except TaskError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(attempt, range(2)))

    assert sum(isinstance(item, dict) for item in accepted) == 1
    assert sum(item == "system_busy" for item in accepted) == 1

    barrier = threading.Barrier(2)

    def claim(index: int):
        barrier.wait(timeout=5)
        return claim_next_task(data_dir, owner_id=f"worker-{index}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, range(2)))
    assert sum(item is not None for item in claimed) == 1


def test_progress_requires_current_owner_and_reports_measured_values(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "progress-0001")
    claimed = claim_next_task(data_dir, owner_id="worker-owner")
    assert claimed and claimed["id"] == task["id"]

    early_publish = publish_complete_generation(
        data_dir,
        task["id"],
        owner_id="worker-owner",
        segments=[_segment("a" * 32)],
        language_probability=0.9,
        elapsed_seconds=1.0,
    )
    assert early_publish is None
    assert transcription_state(data_dir, meeting_id)["generation"] is None

    assert (
        publish_complete_generation(
            data_dir,
            task["id"],
            owner_id="stale-owner",
            segments=[_segment("a" * 32)],
            language_probability=0.9,
            elapsed_seconds=10.0,
        )
        is None
    )
    assert not update_task_progress(
        data_dir,
        task["id"],
        owner_id="stale-owner",
        stage="transcribing",
        elapsed_seconds=10.2,
        transcript_end_seconds=14.5,
        segment_count=3,
    )
    assert update_task_progress(
        data_dir,
        task["id"],
        owner_id="worker-owner",
        stage="transcribing",
        elapsed_seconds=10.2,
        transcript_end_seconds=14.5,
        segment_count=3,
    )
    current = transcription_state(data_dir, meeting_id)["task"]
    assert current["stage"] == "transcribing"
    assert current["elapsed_seconds"] == 10.2
    assert current["transcript_end_seconds"] == 14.5
    assert current["segment_count"] == 3
    assert "model_path" not in current and "owner_id" not in current


def test_cancel_holds_slot_and_rejects_late_generation_until_runner_reaped(tmp_path):
    data_dir, model_dir, meetings = _task_store(tmp_path, meeting_count=2)
    task = _accept(data_dir, model_dir, meetings[0], "cancel-0001")
    claim_next_task(data_dir, owner_id="worker-owner")

    stopping = cancel_task(data_dir, task["id"])
    assert stopping["status"] == "stopping"
    with pytest.raises(TaskError) as error:
        _accept(data_dir, model_dir, meetings[1], "cancel-0002")
    assert error.value.code == "system_busy"

    late_result = publish_complete_generation(
        data_dir,
        task["id"],
        owner_id="worker-owner",
        segments=[_segment("a" * 32)],
        language_probability=0.9,
        elapsed_seconds=3.0,
    )
    assert late_result is None
    assert transcription_state(data_dir, meetings[0])["generation"] is None
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (task["id"],)

    assert finish_canceled_task(data_dir, task["id"], owner_id="worker-owner", elapsed_seconds=4.0)
    assert cancel_task(data_dir, task["id"])["status"] == "canceled"
    accepted_after_reap = _accept(data_dir, model_dir, meetings[1], "cancel-0002")
    assert accepted_after_reap["status"] == "accepted"


def test_error_finalization_honors_cancel_and_releases_reaped_slot(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "cancel-error-0001")
    claim_next_task(data_dir, owner_id="worker-owner")
    assert cancel_task(data_dir, task["id"])["status"] == "stopping"

    assert fail_task(
        data_dir,
        task["id"],
        owner_id="worker-owner",
        error_code="transcription_failed",
        message="Runner failed as the user canceled.",
        elapsed_seconds=2.0,
    )
    assert cancel_task(data_dir, task["id"])["status"] == "canceled"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)


def test_worker_shutdown_finalization_honors_cancel_after_reap(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "cancel-shutdown-0001")
    claim_next_task(data_dir, owner_id="worker-owner")
    assert cancel_task(data_dir, task["id"])["status"] == "stopping"

    assert finish_interrupted_task(
        data_dir, task["id"], owner_id="worker-owner", elapsed_seconds=2.0
    )
    assert cancel_task(data_dir, task["id"])["status"] == "canceled"


def test_complete_generations_are_atomic_stable_and_preserve_previous_current(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    first = _accept(data_dir, model_dir, meeting_id, "generation-001")
    claim_next_task(data_dir, owner_id="owner-1")
    assert update_task_progress(
        data_dir,
        first["id"],
        owner_id="owner-1",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=0,
        segment_count=0,
    )
    result = publish_complete_generation(
        data_dir,
        first["id"],
        owner_id="owner-1",
        segments=[
            _segment("1" * 32),
            _segment(
                "2" * 32,
                start_seconds=2.5,
                end_seconds=3.75,
                text="Review this line.",
                avg_logprob=-1.2,
                no_speech_prob=0.7,
                compression_ratio=2.5,
            ),
        ],
        language_probability=0.91,
        elapsed_seconds=12.5,
    )
    assert result and result["status"] == "complete"
    assert result["segment_count"] == 2

    state = transcription_state(data_dir, meeting_id)
    assert state["task"]["status"] == "succeeded"
    assert state["task"]["result_scope"] == "transcript_only"
    assert state["generation"]["generation_number"] == 1
    assert state["generation"]["language_probability"] == 0.91
    assert [segment["id"] for segment in state["generation"]["segments"]] == [
        "1" * 32,
        "2" * 32,
    ]
    assert state["generation"]["segments"][1]["uncertain"] is True
    assert state["generation"]["segments"][1]["avg_logprob"] == -1.2

    failed = _accept(data_dir, model_dir, meeting_id, "generation-002")
    claim_next_task(data_dir, owner_id="owner-2")
    fail_task(
        data_dir,
        failed["id"],
        owner_id="owner-2",
        error_code="transcription_failed",
        message="Failed before a complete transcript was returned.",
        elapsed_seconds=2.0,
    )
    assert transcription_state(data_dir, meeting_id)["generation"]["id"] == result["id"]

    rerun = _accept(data_dir, model_dir, meeting_id, "generation-003")
    claim_next_task(data_dir, owner_id="owner-3")
    assert update_task_progress(
        data_dir,
        rerun["id"],
        owner_id="owner-3",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=0,
        segment_count=0,
    )
    second = publish_complete_generation(
        data_dir,
        rerun["id"],
        owner_id="owner-3",
        segments=[_segment("3" * 32, text="A later generation.")],
        language_probability=0.88,
        elapsed_seconds=8.0,
    )
    current = transcription_state(data_dir, meeting_id)
    assert second and second["generation_number"] == 2
    assert current["generation"]["id"] == second["id"]
    assert current["generation"]["segments"][0]["id"] == "3" * 32
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM transcript_generations WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone() == (2,)


def test_generation_speaker_catalog_and_versioned_reassignment_keep_original_assignment(
    tmp_path,
):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "speaker-generation-001")
    claim_next_task(data_dir, owner_id="speaker-owner")
    assert update_task_progress(
        data_dir,
        task["id"],
        owner_id="speaker-owner",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=2.5,
        segment_count=1,
    )
    generation = publish_complete_generation(
        data_dir,
        task["id"],
        owner_id="speaker-owner",
        segments=[
            _segment(
                "a" * 32,
                original_speaker_id="SPEAKER_00",
                speaker_id="SPEAKER_00",
                review_reasons=["overlap"],
                overlap=True,
                speaker_dominance=0.52,
                uncertain=True,
            )
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 0.2,
                "last_seen_seconds": 2.5,
            },
            {
                "speaker_id": "SPEAKER_01",
                "display_name": "Speaker 2",
                "first_seen_seconds": 2.0,
                "last_seen_seconds": 3.0,
            },
        ],
        regular_turn_count=2,
        exclusive_turn_count=2,
        language_probability=0.9,
        elapsed_seconds=2.0,
    )
    assert generation is not None
    current = transcription_state(data_dir, meeting_id)["generation"]
    assert current["segments"][0]["review_reasons"] == ["overlap"]
    assert current["segments"][0]["overlap"] is True
    assert current["speakers"][1]["display_name"] == "Speaker 2"

    changed = reassign_current_generation_speaker(
        data_dir,
        meeting_id,
        "a" * 32,
        expected_generation_id=generation["id"],
        expected_source_version=1,
        expected_assignment_version=1,
        speaker_id="SPEAKER_01",
    )
    assert changed == {
        "id": "a" * 32,
        "generation_id": generation["id"],
        "original_speaker_id": "SPEAKER_00",
        "speaker_id": "SPEAKER_01",
        "assignment_version": 2,
        "source_version": 2,
    }
    state = transcription_state(data_dir, meeting_id)["generation"]["segments"][0]
    assert state["original_speaker_id"] == "SPEAKER_00"
    assert state["speaker_id"] == "SPEAKER_01"
    assert state["assignment_version"] == 2
    assert transcription_state(data_dir, meeting_id)["generation"]["source_version"] == 2

    with pytest.raises(TaskError) as error:
        reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            "a" * 32,
            expected_generation_id=generation["id"],
            expected_source_version=1,
            expected_assignment_version=1,
            speaker_id="SPEAKER_00",
        )
    assert error.value.code == "source_version_changed"

    with pytest.raises(TaskError) as error:
        reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            "a" * 32,
            expected_generation_id=generation["id"],
            expected_source_version=2,
            expected_assignment_version=1,
            speaker_id="SPEAKER_00",
        )
    assert error.value.code == "assignment_version_changed"

    next_task = _accept(data_dir, model_dir, meeting_id, "speaker-generation-002")
    claim_next_task(data_dir, owner_id="speaker-owner-2")
    assert update_task_progress(
        data_dir,
        next_task["id"],
        owner_id="speaker-owner-2",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=1.0,
        segment_count=1,
    )
    next_generation = publish_complete_generation(
        data_dir,
        next_task["id"],
        owner_id="speaker-owner-2",
        segments=[_segment("c" * 32, original_speaker_id="SPEAKER_99")],
        speakers=[
            {
                "speaker_id": "SPEAKER_99",
                "display_name": "Speaker 1",
                "first_seen_seconds": 0,
                "last_seen_seconds": 1,
            }
        ],
        language_probability=0.9,
        elapsed_seconds=2.0,
    )
    assert next_generation is not None
    with pytest.raises(TaskError) as stale_generation:
        reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            "a" * 32,
            expected_generation_id=generation["id"],
            expected_source_version=2,
            expected_assignment_version=2,
            speaker_id="SPEAKER_00",
        )
    assert stale_generation.value.code == "current_generation_changed"

    with (
        sqlite3.connect(database_path(data_dir)) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "UPDATE transcript_segments SET original_speaker_id = 'SPEAKER_01' WHERE id = ?",
            ("a" * 32,),
        )

    with (
        sqlite3.connect(database_path(data_dir)) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "UPDATE transcript_segments SET speaker_id = 'SPEAKER_99' WHERE id = ?",
            ("a" * 32,),
        )


def test_current_generation_edits_are_versioned_local_and_preserve_boundaries(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    first = _publish_generation(
        data_dir,
        model_dir,
        meeting_id,
        "editable-generation-001",
        "editable-owner-1",
        [
            _segment(
                "d" * 32,
                start_seconds=1.25,
                end_seconds=2.5,
                original_speaker_id="SPEAKER_00",
                speaker_id="SPEAKER_00",
            ),
            _segment(
                "e" * 32,
                start_seconds=3.0,
                end_seconds=4.0,
                text="A separate utterance.",
                original_speaker_id="SPEAKER_01",
                speaker_id="SPEAKER_01",
            ),
        ],
        [
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1.25,
                "last_seen_seconds": 2.5,
            },
            {
                "speaker_id": "SPEAKER_01",
                "display_name": "Speaker 2",
                "first_seen_seconds": 3.0,
                "last_seen_seconds": 4.0,
            },
        ],
    )
    assert first is not None

    text_change = update_current_generation_segment_text(
        data_dir,
        meeting_id,
        "d" * 32,
        expected_generation_id=first["id"],
        expected_source_version=1,
        text="<b>Corrected words</b>",
    )
    assert text_change["source_version"] == 2

    renamed = update_current_generation_speaker_name(
        data_dir,
        meeting_id,
        "SPEAKER_00",
        expected_generation_id=first["id"],
        expected_source_version=2,
        display_name="  Alice  ",
    )
    assert renamed["display_name"] == "Alice"
    assert renamed["source_version"] == 3
    reset = update_current_generation_speaker_name(
        data_dir,
        meeting_id,
        "SPEAKER_00",
        expected_generation_id=first["id"],
        expected_source_version=3,
        display_name="   ",
    )
    assert reset["display_name"] == "Speaker 1"
    assert reset["source_version"] == 4

    added = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=first["id"],
        expected_source_version=4,
        display_name="Witness",
    )
    assert added["speaker_id"].startswith("MANUAL_")
    assert added["display_name"] == "Witness"
    assert added["source_version"] == 5
    reassigned = reassign_current_generation_speaker(
        data_dir,
        meeting_id,
        "d" * 32,
        expected_generation_id=first["id"],
        expected_source_version=5,
        expected_assignment_version=1,
        speaker_id=added["speaker_id"],
    )
    assert reassigned["source_version"] == 6
    assert reassigned["original_speaker_id"] == "SPEAKER_00"

    current = transcription_state(data_dir, meeting_id)["generation"]
    first_segment, other_segment = current["segments"]
    assert (first_segment["start_seconds"], first_segment["end_seconds"]) == (1.25, 2.5)
    assert first_segment["text"] == "<b>Corrected words</b>"
    assert first_segment["speaker_id"] == added["speaker_id"]
    assert first_segment["original_speaker_id"] == "SPEAKER_00"
    assert other_segment["text"] == "A separate utterance."
    assert other_segment["speaker_id"] == "SPEAKER_01"
    assert current["speakers"][0]["display_name"] == "Speaker 1"
    assert current["speakers"][2]["display_name"] == "Witness"

    with pytest.raises(TaskError) as stale_write:
        update_current_generation_segment_text(
            data_dir,
            meeting_id,
            "d" * 32,
            expected_generation_id=first["id"],
            expected_source_version=5,
            text="A stale draft.",
        )
    assert stale_write.value.code == "source_version_changed"

    second = _publish_generation(
        data_dir,
        model_dir,
        meeting_id,
        "editable-generation-002",
        "editable-owner-2",
        [
            _segment(
                "f" * 32,
                text="A fresh generation.",
                original_speaker_id="SPEAKER_00",
                speaker_id="SPEAKER_00",
            )
        ],
        [
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1.25,
                "last_seen_seconds": 2.5,
            }
        ],
    )
    assert second is not None
    state = transcription_state(data_dir, meeting_id)
    assert state["generation"]["id"] == second["id"]
    assert state["generation"]["speakers"][0]["display_name"] == "Speaker 1"
    assert state["generation_history"][0]["id"] == first["id"]
    assert state["generation_history"][0]["segments"][0]["text"] == "<b>Corrected words</b>"
    assert state["generation_history"][0]["speakers"][0]["display_name"] == "Speaker 1"

    with pytest.raises(TaskError) as historical_write:
        update_current_generation_segment_text(
            data_dir,
            meeting_id,
            "d" * 32,
            expected_generation_id=first["id"],
            expected_source_version=6,
            text="Cannot edit history.",
        )
    assert historical_write.value.code == "current_generation_changed"
    with pytest.raises(TaskError) as historical_speaker_write:
        update_current_generation_speaker_name(
            data_dir,
            meeting_id,
            "SPEAKER_00",
            expected_generation_id=first["id"],
            expected_source_version=6,
            display_name="Cannot edit history.",
        )
    assert historical_speaker_write.value.code == "current_generation_changed"
    with pytest.raises(TaskError) as cross_generation_speaker:
        reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            "f" * 32,
            expected_generation_id=second["id"],
            expected_source_version=1,
            expected_assignment_version=1,
            speaker_id=added["speaker_id"],
        )
    assert cross_generation_speaker.value.code == "invalid_speaker_id"


def test_speakers_use_first_seen_then_manual_creation_order_for_legacy_catalogs(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    generation = _publish_generation(
        data_dir,
        model_dir,
        meeting_id,
        "speaker-order-generation-001",
        "speaker-order-owner",
        [
            _segment(
                "9" * 32,
                original_speaker_id="SPEAKER_Z",
                speaker_id="SPEAKER_Z",
            )
        ],
        [
            {
                "speaker_id": "SPEAKER_Z",
                "display_name": "Custom late speaker",
                "first_seen_seconds": 5.0,
                "last_seen_seconds": 6.0,
            },
            {
                "speaker_id": "SPEAKER_B",
                "display_name": "Custom early B",
                "first_seen_seconds": 1.0,
                "last_seen_seconds": 2.0,
            },
            {
                "speaker_id": "SPEAKER_A",
                "display_name": "Custom early A",
                "first_seen_seconds": 1.0,
                "last_seen_seconds": 3.0,
            },
        ],
    )
    assert generation is not None

    # This insertion order matches catalogs migrated from earlier versions, where row order
    # did not necessarily match the speakers' first appearance in the transcript.
    speakers = transcription_state(data_dir, meeting_id)["generation"]["speakers"]
    assert [speaker["speaker_id"] for speaker in speakers] == [
        "SPEAKER_A",
        "SPEAKER_B",
        "SPEAKER_Z",
    ]

    reset = update_current_generation_speaker_name(
        data_dir,
        meeting_id,
        "SPEAKER_Z",
        expected_generation_id=generation["id"],
        expected_source_version=1,
        display_name=" ",
    )
    assert reset["display_name"] == "Speaker 3"
    assert reset["source_version"] == 2

    first_manual = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=generation["id"],
        expected_source_version=2,
        display_name="Manual first",
    )
    second_manual = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=generation["id"],
        expected_source_version=3,
        display_name="Manual second",
    )
    ordered = transcription_state(data_dir, meeting_id)["generation"]["speakers"]
    assert [speaker["speaker_id"] for speaker in ordered] == [
        "SPEAKER_A",
        "SPEAKER_B",
        "SPEAKER_Z",
        first_manual["speaker_id"],
        second_manual["speaker_id"],
    ]


def test_new_exclusive_worker_recovery_cancels_stopping_and_interrupts_other_active_work(
    tmp_path,
):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "recovery-001")
    claim_next_task(data_dir, owner_id="old-worker")
    cancel_task(data_dir, task["id"])

    lock_fd = acquire_worker_lock(data_dir)
    assert lock_fd is not None
    try:
        assert recover_abandoned_tasks(data_dir) == [task["id"]]
    finally:
        import os

        os.close(lock_fd)

    recovered = transcription_state(data_dir, meeting_id)["task"]
    assert recovered["status"] == "canceled"
    assert recovered["stage"] == "canceled"
    assert recovered["error_code"] is None
    assert recovered["completed_at"]
    assert _active_slot(data_dir) is None
    assert transcription_state(data_dir, meeting_id)["generation"] is None
    assert claim_next_task(data_dir, owner_id="new-worker") is None


@pytest.mark.parametrize("claimed", [False, True])
def test_new_exclusive_worker_recovery_interrupts_accepted_and_running_tasks(tmp_path, claimed):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    task = _accept(data_dir, model_dir, meeting_id, "recovery-002")
    if claimed:
        claim_next_task(data_dir, owner_id="old-worker")

    assert recover_abandoned_tasks(data_dir) == [task["id"]]

    recovered = transcription_state(data_dir, meeting_id)["task"]
    assert recovered["status"] == "interrupted"
    assert recovered["error_code"] == "worker_restarted"
    assert _active_slot(data_dir) is None
    assert transcription_state(data_dir, meeting_id)["generation"] is None
    assert claim_next_task(data_dir, owner_id="new-worker") is None


def _active_slot(data_dir: Path):
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()[0]
