"""Which ASR models a run can use, and how each choice reaches a task."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from seeding import prepare_diarization_model, seed_meeting

from asr_studio.asr_models import CATALOG, model_choices, prepare_command, resolve_run
from asr_studio.config import Settings
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory
from asr_studio.runtime import MODEL_REPOSITORIES, RuntimeProfile
from asr_studio.storage import initialize
from asr_studio.tasks import (
    TaskError,
    accept_transcription_task,
    claim_next_task,
    publish_complete_generation,
    transcription_state,
    update_task_progress,
)


def _whisper_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(b"model")
    (path / "config.json").write_text(
        json.dumps({"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []}), encoding="utf-8"
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


def _qwen_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type": "qwen3_asr"}', encoding="utf-8")
    for name in (
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "generation_config.json",
        "processor_config.json",
    ):
        (path / name).write_text("{}", encoding="utf-8")
    (path / "chat_template.jinja").write_text("", encoding="utf-8")
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        asr_gpu_model_path=_whisper_dir(tmp_path / "whisper-large-v3"),
        asr_cpu_model_path=tmp_path / "whisper-small",
        asr_phowhisper_model_path=tmp_path / "pho-whisper-large",
        asr_qwen_model_path=tmp_path / "qwen3-asr-1.7b",
        diarization_model_path=prepare_diarization_model(tmp_path / "diarization"),
    )


def _gpu(settings: Settings) -> RuntimeProfile:
    return RuntimeProfile("cuda", "float16", settings.asr_gpu_model_path)


def _by_id(settings: Settings, profile: RuntimeProfile | None) -> dict[str, dict]:
    return {choice["id"]: choice for choice in model_choices(settings, profile)}


def test_readiness_is_per_model_and_only_unprepared_models_get_a_command(tmp_path):
    settings = _settings(tmp_path)
    choices = _by_id(settings, _gpu(settings))
    assert list(choices) == [
        "auto",
        "whisper-large-v3",
        "whisper-small",
        "pho-whisper-large",
        "qwen3-asr-1.7b",
    ]
    assert choices["auto"]["ready"] is True
    assert choices["auto"]["model_name"] == "whisper-large-v3"
    assert choices["whisper-large-v3"]["prepare_command"] is None
    pho = choices["pho-whisper-large"]
    assert (pho["ready"], pho["reason"]) == (False, "not_prepared")
    assert pho["prepare_command"].endswith("model prepare --asr-model pho-whisper-large")
    assert "HF_HUB_OFFLINE=0" in pho["prepare_command"]

    _whisper_dir(settings.asr_cpu_model_path)
    _whisper_dir(settings.asr_phowhisper_model_path)
    _qwen_dir(settings.asr_qwen_model_path)
    assert all(choice["ready"] for choice in model_choices(settings, _gpu(settings)))


def test_a_gpu_only_model_is_not_ready_on_a_cpu_worker_even_when_prepared(tmp_path):
    settings = _settings(tmp_path)
    _qwen_dir(settings.asr_qwen_model_path)
    profile = RuntimeProfile("cpu", "int8", settings.asr_gpu_model_path)
    qwen = _by_id(settings, profile)["qwen3-asr-1.7b"]
    assert (qwen["ready"], qwen["reason"], qwen["prepare_command"]) == (
        False,
        "gpu_required",
        None,
    )


def test_the_prepare_command_for_the_default_keeps_the_gpu_visible(tmp_path):
    settings = _settings(tmp_path)
    # Without the GPU file the container would select and download the CPU model instead.
    assert "compose.gpu.yaml" in prepare_command("auto", "cuda")
    assert "compose.gpu.yaml" not in prepare_command("auto", "cpu")
    assert "compose.gpu.yaml" not in prepare_command("pho-whisper-large", "cuda")
    missing = RuntimeProfile("cuda", "float16", tmp_path / "gone")
    assert "compose.gpu.yaml" in _by_id(settings, missing)["auto"]["prepare_command"]


def test_an_unreadable_optional_model_does_not_break_reading_a_meeting(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    settings = _settings(tmp_path)
    _qwen_dir(settings.asr_qwen_model_path)
    settings.asr_qwen_model_path.chmod(0)
    try:
        qwen = _by_id(settings, _gpu(settings))["qwen3-asr-1.7b"]
    finally:
        settings.asr_qwen_model_path.chmod(0o700)
    assert (qwen["ready"], qwen["reason"]) == (False, "not_prepared")


def test_no_worker_means_no_choice_is_ready(tmp_path):
    settings = _settings(tmp_path)
    choices = model_choices(settings, None)
    assert {choice["reason"] for choice in choices} == {"worker_unavailable"}
    # A CPU machine must not read as if it had the GPU default while its worker is down.
    assert {choice["model_name"] for choice in choices} == {None}


def test_auto_stays_the_hardware_default_and_a_choice_changes_only_its_own_run(tmp_path):
    settings = _settings(tmp_path)
    profile = RuntimeProfile("cuda", "float16", tmp_path / "configured-default")
    assert resolve_run(settings, profile, "auto") == {
        "asr_model": "auto",
        "model_path": tmp_path / "configured-default",
        "device": "cuda",
        "compute_type": "float16",
    }
    assert resolve_run(settings, profile, "pho-whisper-large") == {
        "asr_model": "pho-whisper-large",
        "model_path": settings.asr_phowhisper_model_path,
        "device": "cuda",
        "compute_type": "float16",
    }
    # Transformers runs Qwen in its own dtype, and the transcript must say so.
    assert resolve_run(settings, profile, "qwen3-asr-1.7b")["compute_type"] == "bfloat16"
    assert profile.model_path == tmp_path / "configured-default"


def _accept(settings: Settings, meeting_id: str, request_id: str, asr_model: str, *, device="cuda"):
    profile = RuntimeProfile(device, "float16", settings.asr_gpu_model_path)
    return accept_transcription_task(
        settings.data_dir,
        meeting_id=meeting_id,
        request_id=request_id,
        diarization_model_path=settings.diarization_model_path,
        worker_ready=True,
        **resolve_run(settings, profile, asr_model),
    )


def test_an_unprepared_model_cannot_create_a_task(tmp_path):
    settings = _settings(tmp_path)
    initialize(settings.data_dir)
    meeting_id = seed_meeting(settings.data_dir, "Unprepared")
    with pytest.raises(TaskError) as refused:
        _accept(settings, meeting_id, "request-unprepared-1", "pho-whisper-large")
    assert (refused.value.status_code, refused.value.code) == (503, "model_unavailable")


def test_a_gpu_only_model_cannot_create_a_task_on_cpu(tmp_path):
    settings = _settings(tmp_path)
    initialize(settings.data_dir)
    _qwen_dir(settings.asr_qwen_model_path)
    meeting_id = seed_meeting(settings.data_dir, "CPU")
    with pytest.raises(TaskError) as refused:
        _accept(settings, meeting_id, "request-cpu-only-1", "qwen3-asr-1.7b", device="cpu")
    assert (refused.value.status_code, refused.value.code) == (503, "model_requires_gpu")


def test_the_task_records_the_choice_and_a_retry_cannot_change_it(tmp_path):
    settings = _settings(tmp_path)
    initialize(settings.data_dir)
    _qwen_dir(settings.asr_qwen_model_path)
    meeting_id = seed_meeting(settings.data_dir, "Choice")
    task = _accept(settings, meeting_id, "request-choice-01", "qwen3-asr-1.7b")
    assert task["asr_model"] == "qwen3-asr-1.7b"
    assert task["model_name"] == "qwen3-asr-1.7b"
    assert task["compute_type"] == "bfloat16"
    assert _accept(settings, meeting_id, "request-choice-01", "qwen3-asr-1.7b")["id"] == task["id"]
    with pytest.raises(TaskError) as conflict:
        _accept(settings, meeting_id, "request-choice-01", "auto")
    assert conflict.value.code == "idempotency_conflict"


def test_the_generation_records_the_runtime_of_the_run_that_made_it(tmp_path):
    settings = _settings(tmp_path)
    initialize(settings.data_dir)
    _qwen_dir(settings.asr_qwen_model_path)
    meeting_id = seed_meeting(settings.data_dir, "Runtime")
    task = _accept(settings, meeting_id, "request-runtime-01", "qwen3-asr-1.7b")
    assert claim_next_task(settings.data_dir, owner_id="worker")["id"] == task["id"]
    assert update_task_progress(
        settings.data_dir,
        task["id"],
        owner_id="worker",
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=2.0,
        segment_count=1,
    )
    assert publish_complete_generation(
        settings.data_dir,
        task["id"],
        owner_id="worker",
        segments=[
            {"id": "a" * 32, "start_seconds": 1.0, "end_seconds": 2.0, "text": "Xin chào."},
        ],
        language_probability=None,
        elapsed_seconds=1.0,
    )
    generation = transcription_state(settings.data_dir, meeting_id)["generation"]
    assert generation["model_name"] == "qwen3-asr-1.7b"
    assert (generation["device"], generation["compute_type"]) == ("cuda", "bfloat16")


def test_the_cpu_default_is_a_catalog_model_that_automatic_points_at(tmp_path):
    settings = _settings(tmp_path)
    profile = RuntimeProfile("cpu", "int8", settings.asr_cpu_model_path)

    choices = {item["id"]: item for item in model_choices(settings, profile)}

    assert choices["auto"]["model_name"] == choices["whisper-small"]["model_name"]
    assert choices["whisper-small"]["prepare_command"].endswith("--asr-model whisper-small")
    assert resolve_run(settings, profile, "whisper-small")["compute_type"] == "int8"
    assert CATALOG["whisper-small"].repository == MODEL_REPOSITORIES["cpu"]


def test_a_whisper_model_without_a_preprocessor_file_is_prepared_as_whisper_small_ships(tmp_path):
    model = _whisper_dir(tmp_path / "whisper-small")
    preprocessor = model / "preprocessor_config.json"
    preprocessor.unlink()
    assert prepared_engine_directory("whisper", model) == model.resolve()

    preprocessor.write_text("{}", encoding="utf-8")
    with pytest.raises(ModelConfigurationError):  # a file that is there must still be right
        prepared_engine_directory("whisper", model)
