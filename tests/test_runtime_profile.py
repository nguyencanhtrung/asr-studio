from __future__ import annotations

import json

import pytest

from asr_studio.config import Settings
from asr_studio.runtime import (
    RuntimeConfigurationError,
    RuntimeProfile,
    read_runtime_profile,
    select_runtime,
    write_runtime_profile,
)


def _prepared_model(path):
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


def _prepared_qwen(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type": "qwen3_asr"}', encoding="utf-8")
    for filename in (
        "generation_config.json",
        "model.safetensors",
        "processor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ):
        (path / filename).write_text("{}", encoding="utf-8")
    (path / "chat_template.jinja").write_text("", encoding="utf-8")
    return path


def _settings(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path / "data",
        "auth_dir": tmp_path / "auth",
        "asr_device": "auto",
        "asr_gpu_model_path": _prepared_model(tmp_path / "whisper-large-v3"),
        "asr_cpu_model_path": _prepared_model(tmp_path / "whisper-small"),
    }
    values.update(overrides)
    return Settings(**values)


def test_auto_runtime_accepts_qwen_hardware_default_layout(tmp_path):
    qwen = _prepared_qwen(tmp_path / "qwen3-asr-1.7b")
    settings = _settings(
        tmp_path,
        asr_gpu_model_path=qwen,
        asr_qwen_model_path=qwen,
    )

    profile = select_runtime(
        settings,
        get_cuda_device_count=lambda: 1,
        load_library=lambda _name: object(),
        get_supported_compute_types=lambda _device: {"float16", "float32"},
    )

    assert profile.model_name == "qwen3-asr-1.7b"


def test_auto_runtime_prefers_gpu_large_model_and_float16(tmp_path):
    settings = _settings(tmp_path)
    loaded = []

    profile = select_runtime(
        settings,
        get_cuda_device_count=lambda: 1,
        load_library=lambda name: loaded.append(name),
        get_supported_compute_types=lambda device: {"float16", "float32"},
    )

    assert profile.device == "cuda"
    assert profile.compute_type == "float16"
    assert profile.model_name == "whisper-large-v3"
    assert loaded == ["libcublas.so.12", "libcudnn.so.9"]


def test_auto_runtime_falls_back_to_cpu_small_model_and_int8(tmp_path):
    settings = _settings(tmp_path)

    profile = select_runtime(
        settings,
        get_cuda_device_count=lambda: 0,
        load_library=lambda _name: pytest.fail("CPU selection must not load CUDA libraries"),
        get_supported_compute_types=lambda device: {"int8", "float32"},
    )

    assert profile.device == "cpu"
    assert profile.compute_type == "int8"
    assert profile.model_name == "whisper-small"


def test_visible_gpu_with_broken_cuda_never_silently_falls_back_to_cpu(tmp_path):
    settings = _settings(tmp_path)

    def missing_library(name):
        raise OSError(f"missing {name}")

    with pytest.raises(RuntimeConfigurationError, match="libcublas"):
        select_runtime(
            settings,
            get_cuda_device_count=lambda: 1,
            load_library=missing_library,
            get_supported_compute_types=lambda device: {"float16"},
        )


def test_runtime_profile_round_trips_without_exposing_an_unvalidated_document(tmp_path):
    data_dir = tmp_path / "data"
    profile = RuntimeProfile(
        device="cuda",
        compute_type="float16",
        model_path=tmp_path / "whisper-large-v3",
    )

    write_runtime_profile(data_dir, profile)

    assert read_runtime_profile(data_dir) == profile
    assert (data_dir / "worker.runtime.json").stat().st_mode & 0o777 == 0o600
