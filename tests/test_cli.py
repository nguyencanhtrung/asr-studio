from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import pytest

from asr_studio import __version__, cli, model_installer
from asr_studio.asr_models import CATALOG
from asr_studio.cli import main
from asr_studio.diarization_runner import (
    DIARIZATION_ERROR_MESSAGES,
    DiarizationConfigurationError,
)
from asr_studio.runtime import RuntimeProfile
from asr_studio.storage import SCHEMA_VERSION
from asr_studio.tasks import TASK_ERROR_MESSAGES


def test_version_json_reports_application_version_and_supported_schema(capsys):
    assert main(["version", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "version": __version__,
        "supported_schema_version": SCHEMA_VERSION,
    }
    assert captured.err == ""


def test_version_json_checks_expected_version_and_schema(capsys):
    assert (
        main(
            [
                "version",
                "--json",
                "--expect-version",
                __version__,
                "--expect-schema",
                str(SCHEMA_VERSION),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["version"] == __version__

    assert (
        main(
            [
                "version",
                "--json",
                "--expect-version",
                __version__,
                "--expect-schema",
                str(SCHEMA_VERSION + 1),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Version/schema mismatch" in captured.err


def test_legacy_version_flag_keeps_its_output(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert f"asr-studio {__version__}" in capsys.readouterr().out


def _prepared_model(path):
    path.mkdir()
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


def test_installation_check_requires_worker_before_printing_access_link(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "worker_is_healthy", lambda _directory: False)

    assert main(["installation", "check"]) == 1
    captured = capsys.readouterr()
    assert "worker is not ready" in captured.err
    assert captured.out == ""


def test_installation_check_requires_models_visible_from_app(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "worker_is_healthy", lambda _directory: True)
    model = _prepared_model(tmp_path / "model")
    monkeypatch.setattr(cli, "read_runtime_profile", lambda _directory: None)
    assert main(["installation", "check"]) == 1
    assert "cannot read the worker model" in capsys.readouterr().err

    monkeypatch.setattr(
        cli,
        "read_runtime_profile",
        lambda _directory: RuntimeProfile("cpu", "int8", model),
    )
    monkeypatch.setattr(cli, "prepared_diarization_directory", lambda _path: None)

    assert main(["installation", "check"]) == 0
    assert "worker and local models" in capsys.readouterr().out

    unmounted_model = tmp_path / "unmounted-model"
    model.rename(unmounted_model)
    assert main(["installation", "check"]) == 1
    captured = capsys.readouterr()
    assert "cannot read the mounted models" in captured.err
    assert captured.out == ""

    unmounted_model.rename(model)

    def missing_diarization(_path):
        raise DiarizationConfigurationError("not mounted")

    monkeypatch.setattr(cli, "prepared_diarization_directory", missing_diarization)
    assert main(["installation", "check"]) == 1
    assert "cannot read the mounted models" in capsys.readouterr().err


def test_model_check_fails_cleanly_when_local_model_is_missing(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing-model"
    monkeypatch.setenv("ASR_MODEL_PATH", str(missing))
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "int8")

    assert main(["model", "check"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "runtime_unavailable: selecting_runtime\n"
    assert str(missing) not in captured.err
    assert "Traceback" not in captured.err


def test_model_check_accepts_the_same_prepared_model_as_runtime(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "prepared-model")
    monkeypatch.setenv("ASR_MODEL_PATH", str(model))
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "int8")

    assert main(["model", "check"]) == 0

    captured = capsys.readouterr()
    assert str(model) not in captured.out
    assert "ready" in captured.out
    assert captured.err == ""


def test_runtime_check_rejects_cuda_when_no_gpu_is_visible(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "prepared-model")
    monkeypatch.setenv("ASR_MODEL_PATH", str(model))
    monkeypatch.setenv("ASR_DEVICE", "cuda")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "float16")
    monkeypatch.setattr(cli.ctypes, "CDLL", lambda _name: object())
    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 0)

    assert main(["runtime", "check"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "runtime_unavailable: selecting_runtime\n"


def test_runtime_check_rejects_missing_cuda_libraries(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "prepared-model")
    monkeypatch.setenv("ASR_MODEL_PATH", str(model))
    monkeypatch.setenv("ASR_DEVICE", "cuda")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "float16")

    def missing_library(name):
        raise OSError(f"missing {name} token=secret /private/cuda")

    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(cli.ctypes, "CDLL", missing_library)

    assert main(["runtime", "check"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "runtime_unavailable: selecting_runtime\n"
    assert "token=secret" not in captured.err
    assert "/private/cuda" not in captured.err


def test_runtime_check_reports_sanitized_diarization_preflight_failure(
    tmp_path, monkeypatch, capsys
):
    model = _prepared_model(tmp_path / "prepared-model")
    monkeypatch.setenv("ASR_MODEL_PATH", str(model))
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "int8")
    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(
        cli.ctranslate2,
        "get_supported_compute_types",
        lambda _device: {"int8", "float32"},
    )

    monkeypatch.setattr(
        cli,
        "_run_diarization_preflight",
        lambda **_kwargs: (
            "diarization_decoder_unavailable",
            DIARIZATION_ERROR_MESSAGES["diarization_decoder_unavailable"],
        ),
    )

    assert main(["runtime", "check"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "diarization_decoder_unavailable" in captured.err
    assert DIARIZATION_ERROR_MESSAGES["diarization_decoder_unavailable"] in captured.err
    assert "runtime check with --verbose" in captured.err
    assert "libpython" not in captured.err
    assert "Traceback" not in captured.err


def test_runtime_check_accepts_cuda_model_device_and_compute_type(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "prepared-model")
    monkeypatch.setenv("ASR_MODEL_PATH", str(model))
    monkeypatch.setenv("ASR_DEVICE", "cuda")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "float16")
    monkeypatch.setattr(cli.ctypes, "CDLL", lambda _name: object())
    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        cli.ctranslate2,
        "get_supported_compute_types",
        lambda _device: {"float16", "float32", "int8_float16"},
    )
    preflight_calls = []

    def preflight(*, model_path, device, verbose):
        preflight_calls.append((model_path, device, verbose))
        return None

    monkeypatch.setattr(cli, "_run_diarization_preflight", preflight)

    assert main(["runtime", "check"]) == 0

    captured = capsys.readouterr()
    assert str(model) not in captured.out
    assert "NVIDIA GPU is ready" in captured.out
    assert "Community-1 diarization model is ready on cuda" in captured.out
    assert "float16" not in captured.out
    assert captured.err == ""
    assert preflight_calls == [(cli.Settings.from_env().diarization_model_path, "cuda", False)]


def test_diarization_preflight_isolated_process_accepts_only_safe_child_errors(
    tmp_path, monkeypatch
):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        event = {
            "type": "error",
            "code": "diarization_cuda_failed",
            "message": "vendor detail /private/path token=secret",
        }
        return subprocess.CompletedProcess(command, 1, stdout=json.dumps(event), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    result = cli._run_diarization_preflight(model_path=tmp_path / "model", device="cuda")

    assert result == (
        "diarization_failed",
        DIARIZATION_ERROR_MESSAGES["diarization_failed"],
    )
    assert observed["command"][:3] == [sys.executable, "-m", "asr_studio.diarization_runner"]
    assert "--check-runtime" in observed["command"]
    assert "--verbose" not in observed["command"]
    assert observed["kwargs"]["stderr"] is subprocess.DEVNULL


def test_diarization_preflight_verbose_mode_inherits_stderr(tmp_path, monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr=None)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert (
        cli._run_diarization_preflight(model_path=tmp_path / "model", device="cuda", verbose=True)
        is None
    )
    assert observed["stderr"] is None
    assert "--verbose" in observed["command"]


def test_task_diagnose_prints_only_allowlisted_diarization_details(tmp_path, monkeypatch, capsys):
    task_id = "a" * 32
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASR_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(
        cli,
        "get_task",
        lambda _data_dir, _task_id: {
            "id": task_id,
            "meeting_id": "b" * 32,
            "status": "failed",
            "stage": "failed",
            "error_code": "diarization_input_decode_failed",
            "error_message": "vendor path /private/model/token=secret",
        },
    )

    assert main(["task", "diagnose", task_id]) == 0

    captured = capsys.readouterr()
    assert f"Diagnostic ID: {task_id}" in captured.out
    assert f"Meeting ID: {'b' * 32}" in captured.out
    assert "Failure phase: See worker logs for this Diagnostic ID." in captured.out
    assert "Code: diarization_input_decode_failed" in captured.out
    assert DIARIZATION_ERROR_MESSAGES["diarization_input_decode_failed"] in captured.out
    assert "vendor path" not in captured.out
    assert "secret" not in captured.out
    assert captured.err == ""


def test_task_diagnose_allowlists_publication_failure(tmp_path, monkeypatch, capsys):
    task_id = "e" * 32
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASR_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(
        cli,
        "get_task",
        lambda _data_dir, _task_id: {
            "meeting_id": "f" * 32,
            "status": "failed",
            "stage": "failed",
            "error_code": "publication_failed",
            "error_message": "database is locked at /private/path",
        },
    )

    assert main(["task", "diagnose", task_id]) == 0

    captured = capsys.readouterr()
    assert "Code: publication_failed" in captured.out
    assert TASK_ERROR_MESSAGES["publication_failed"] in captured.out
    assert "database is locked" not in captured.out
    assert "/private/path" not in captured.out
    assert captured.err == ""


def test_task_diagnose_sanitizes_unknown_codes_and_rejects_malformed_ids(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASR_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(
        cli,
        "get_task",
        lambda _data_dir, _task_id: {
            "meeting_id": "b" * 32,
            "status": "failed",
            "stage": "failed",
            "error_code": "vendor_exception\u001b[31m",
            "error_message": "token=secret",
        },
    )

    assert main(["task", "diagnose", "\u001b[31munknown"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "32 lowercase hexadecimal" in captured.err

    assert main(["task", "diagnose", "c" * 32]) == 0
    captured = capsys.readouterr()
    assert "Code: other" in captured.out
    assert "another local processing phase" in captured.out
    assert "vendor_exception" not in captured.out
    assert "secret" not in captured.out


def test_task_diagnose_missing_database_is_read_only_and_fails_without_traceback(
    tmp_path, monkeypatch, capsys
):
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("ASR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ASR_AUTH_DIR", str(tmp_path / "auth"))

    assert main(["task", "diagnose", "d" * 32]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Task diagnostics are unavailable.\n"
    assert "Traceback" not in captured.err
    assert not (data_dir / "app.sqlite3").exists()


def test_model_prepare_downloads_the_automatically_selected_cpu_model(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "whisper-small"
    monkeypatch.setenv("ASR_DEVICE", "auto")
    monkeypatch.setenv("ASR_CPU_MODEL_PATH", str(target))
    monkeypatch.delenv("ASR_MODEL_PATH", raising=False)
    monkeypatch.delenv("ASR_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(
        cli.ctranslate2, "get_supported_compute_types", lambda _device: {"int8", "float32"}
    )
    downloaded = []

    def download(*, repo_id, revision, local_dir):
        downloaded.append((repo_id, revision, local_dir))
        _prepared_model(local_dir)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)

    assert main(["model", "prepare"]) == 0
    assert downloaded == [("Systran/faster-whisper-small", None, target.resolve())]
    assert "ready" in capsys.readouterr().out


def test_model_prepare_keeps_the_gpu_default_on_its_pinned_revision(tmp_path, monkeypatch):
    target = tmp_path / "whisper-large-v3"
    monkeypatch.setenv("ASR_DEVICE", "auto")
    monkeypatch.setenv("ASR_GPU_MODEL_PATH", str(target))
    monkeypatch.delenv("ASR_MODEL_PATH", raising=False)
    monkeypatch.delenv("ASR_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(cli.ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        cli.ctranslate2, "get_supported_compute_types", lambda _device: {"float16", "int8"}
    )
    monkeypatch.setattr(cli.ctypes, "CDLL", lambda _name: object(), raising=False)
    downloaded = []

    def download(*, repo_id, revision, local_dir):
        downloaded.append((repo_id, revision))
        _prepared_model(local_dir)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)

    assert main(["model", "prepare"]) == 0
    assert downloaded == [("Systran/faster-whisper-large-v3", CATALOG["whisper-large-v3"].revision)]


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """The three catalog models need about 20 GB; these tests download nothing."""
    monkeypatch.setattr(
        model_installer.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10**12)
    )


def test_prepare_converts_phowhisper_and_publishes_only_a_complete_model(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "pho-whisper-large"
    asr_model = "pho-whisper-large"
    monkeypatch.setenv("ASR_PHOWHISPER_MODEL_PATH", str(target))
    downloaded = []

    def download(*, repo_id, revision, local_dir):
        assert revision == CATALOG[asr_model].revision
        downloaded.append(repo_id)
        Path(local_dir).mkdir()

    class Converter:
        def __init__(self, source, **kwargs):
            self.kwargs = kwargs

        def convert(self, output, **kwargs):
            assert not target.exists()
            assert self.kwargs["copy_files"] == ["tokenizer.json", "preprocessor_config.json"]
            assert kwargs == {"quantization": "float16"}
            _prepared_model(Path(output))

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr("ctranslate2.converters.TransformersConverter", Converter)

    assert main(["model", "prepare", "--asr-model", "pho-whisper-large"]) == 0

    assert downloaded == ["vinai/PhoWhisper-large"]
    assert (target / "model.bin").is_file()
    assert [path.name for path in tmp_path.iterdir()] == ["pho-whisper-large"]
    assert "ready" in capsys.readouterr().out


def test_prepare_of_an_optional_model_never_replaces_existing_files(tmp_path, monkeypatch, capsys):
    target = tmp_path / "pho-whisper-large"
    target.mkdir()
    (target / "operator-file").write_text("kept", encoding="utf-8")
    monkeypatch.setenv("ASR_PHOWHISPER_MODEL_PATH", str(target))
    downloaded = []
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        lambda *, repo_id, revision, local_dir: downloaded.append(repo_id),
    )

    assert main(["model", "prepare", "--asr-model", "pho-whisper-large"]) == 1

    # Refused before spending 3 GB of download on a conversion it could not publish.
    assert downloaded == []
    assert (target / "operator-file").read_text(encoding="utf-8") == "kept"
    error = capsys.readouterr().err
    assert "model_preparation_failed" in error
    assert "not empty" in error


def test_prepare_downloads_qwen_without_touching_the_hardware_default(tmp_path, monkeypatch):
    target = tmp_path / "qwen3-asr-1.7b"
    asr_model = "qwen3-asr-1.7b"
    monkeypatch.setenv("ASR_QWEN_MODEL_PATH", str(target))
    downloaded = []

    def download(*, repo_id, revision, local_dir):
        assert revision == CATALOG[asr_model].revision
        downloaded.append(repo_id)
        Path(local_dir).mkdir()
        (Path(local_dir) / "config.json").write_text(
            '{"model_type": "qwen3_asr"}', encoding="utf-8"
        )
        for name in (
            "model.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "generation_config.json",
            "processor_config.json",
        ):
            (Path(local_dir) / name).write_text("{}", encoding="utf-8")
        (Path(local_dir) / "chat_template.jinja").write_text("", encoding="utf-8")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)

    assert main(["model", "prepare", "--asr-model", "qwen3-asr-1.7b"]) == 0
    assert downloaded == ["Qwen/Qwen3-ASR-1.7B-hf"]
