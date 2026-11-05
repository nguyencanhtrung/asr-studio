"""Select and publish the local ASR runtime actually used by the worker."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import ctranslate2

from asr_studio.config import Settings
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory

CUDA_LIBRARIES = ("libcublas.so.12", "libcudnn.so.9")
RUNTIME_PROFILE_NAME = "worker.runtime.json"
MODEL_REPOSITORIES = {
    "cuda": "Qwen/Qwen3-ASR-1.7B-hf",
    "cpu": "Systran/faster-whisper-small",
}


class RuntimeConfigurationError(RuntimeError):
    """The selected local compute profile cannot run safely."""


def engine_for_path(settings: Settings, model_path: Path) -> str:
    """Use Qwen only for the configured Qwen catalog path; custom paths stay Whisper."""
    try:
        configured = Path(settings.asr_qwen_model_path).expanduser().resolve(strict=False)
        selected = Path(model_path).expanduser().resolve(strict=False)
    except OSError:
        return "whisper"
    return "qwen" if selected == configured or selected.name == "qwen3-asr-1.7b" else "whisper"


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    device: str
    compute_type: str
    model_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_path", Path(self.model_path))
        if self.device not in {"cpu", "cuda"}:
            raise ValueError("Runtime profile device must be cpu or cuda")
        if not self.compute_type:
            raise ValueError("Runtime profile compute type is required")

    @property
    def model_name(self) -> str:
        return self.model_path.name

    def public_summary(self) -> dict[str, str]:
        return {
            "device": self.device,
            "compute_type": self.compute_type,
            "model_name": self.model_name,
        }


def select_runtime(
    settings: Settings,
    *,
    get_cuda_device_count: Callable[[], int] = ctranslate2.get_cuda_device_count,
    load_library: Callable[[str], object] = ctypes.CDLL,
    get_supported_compute_types: Callable[
        [str], set[str]
    ] = ctranslate2.get_supported_compute_types,
    require_model: bool = True,
) -> RuntimeProfile:
    """Resolve auto to one concrete device/model profile and validate its runtime."""
    try:
        cuda_device_count = get_cuda_device_count()
    except RuntimeError as error:
        raise RuntimeConfigurationError(f"Cannot inspect CUDA devices: {error}") from error

    device = settings.asr_device
    if device == "auto":
        device = "cuda" if cuda_device_count > 0 else "cpu"

    if device == "cuda":
        if cuda_device_count < 1:
            raise RuntimeConfigurationError("No NVIDIA GPU is visible to the transcription worker.")
        for library in CUDA_LIBRARIES:
            try:
                load_library(library)
            except OSError as error:
                raise RuntimeConfigurationError(
                    f"CUDA runtime library is unavailable: {library}"
                ) from error

    model_path = settings.asr_model_path or (
        settings.asr_gpu_model_path if device == "cuda" else settings.asr_cpu_model_path
    )
    compute_type = settings.asr_compute_type or (
        settings.asr_gpu_compute_type if device == "cuda" else settings.asr_cpu_compute_type
    )
    try:
        supported_types = get_supported_compute_types(device)
    except RuntimeError as error:
        raise RuntimeConfigurationError(
            f"The {device} ASR runtime is unavailable: {error}"
        ) from error
    if compute_type != "default" and compute_type not in supported_types:
        supported = ", ".join(sorted(supported_types))
        raise RuntimeConfigurationError(
            f"ASR compute type {compute_type} is not supported on {device}; "
            f"supported values: {supported}"
        )

    if require_model:
        try:
            model_path = prepared_engine_directory(
                engine_for_path(settings, model_path), model_path
            )
        except ModelConfigurationError as error:
            raise RuntimeConfigurationError(
                f"Local ASR model is not prepared at {model_path}: {error}"
            ) from error
    else:
        model_path = Path(model_path).expanduser().resolve()
    return RuntimeProfile(device=device, compute_type=compute_type, model_path=model_path)


def runtime_profile_path(data_dir: Path) -> Path:
    return Path(data_dir) / RUNTIME_PROFILE_NAME


def write_runtime_profile(data_dir: Path, profile: RuntimeProfile) -> None:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = runtime_profile_path(directory)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".runtime-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        document = {
            "version": 1,
            "device": profile.device,
            "compute_type": profile.compute_type,
            "model_path": str(profile.model_path),
        }
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(document, temporary_file, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except OSError:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def read_runtime_profile(data_dir: Path) -> RuntimeProfile | None:
    path = runtime_profile_path(data_dir)
    if path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1:
            return None
        profile = RuntimeProfile(
            device=document["device"],
            compute_type=document["compute_type"],
            model_path=Path(document["model_path"]),
        )
        if not profile.model_path.is_absolute():
            return None
        os.chmod(path, 0o600)
        return profile
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
