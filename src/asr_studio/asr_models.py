"""The ASR models an operator can choose for one processing run."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from asr_studio.config import Settings
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory
from asr_studio.runtime import RuntimeProfile, engine_for_path

AUTO = "auto"
DEFAULT_MODEL_FILE = "asr-default.json"  # a file, not a table: rolling back needs no migration
_QWEN_COMPUTE_TYPE = "bfloat16"  # Transformers, not CTranslate2: fixed by what the model ships


@dataclass(frozen=True, slots=True)
class AsrModel:
    id: str
    repository: str
    engine: str
    path_setting: str
    revision: str  # pinned Hugging Face commit: an install fetches exactly these files
    download_bytes: int
    extra_bytes: int = 0  # written beside the download before it is published (a conversion)
    gpu_only: bool = False

    @property
    def required_bytes(self) -> int:
        return self.download_bytes + self.extra_bytes


CATALOG = {
    model.id: model
    for model in (
        AsrModel(
            "whisper-large-v3",
            "Systran/faster-whisper-large-v3",
            "whisper",
            "asr_gpu_model_path",
            "edaa852ec7e145841d8ffdb056a99866b5f0a478",
            3_090_839_273,
        ),
        AsrModel(
            "whisper-small",
            "Systran/faster-whisper-small",
            "whisper",
            "asr_cpu_model_path",
            "536b0662742c02347bc0e980a01041f333bce120",
            486_215_847,
        ),
        AsrModel(
            "pho-whisper-large",
            "vinai/PhoWhisper-large",
            "whisper_vi",
            "asr_phowhisper_model_path",
            "b9136a44b5f2ca664bd0b8f74baecf1715f6eeeb",
            6_177_255_126,
            # The converted model measured 3,090,188,278 bytes, beside the download until done.
            extra_bytes=3_300_000_000,
        ),
        AsrModel(
            "qwen3-asr-1.7b",
            "Qwen/Qwen3-ASR-1.7B-hf",
            "qwen",
            "asr_qwen_model_path",
            "bcd2b5b7f32b480ab5790554cfa8347f246a14f3",
            4_087_646_324,
            gpu_only=True,
        ),
    )
}


def selectable_models() -> tuple[str, ...]:
    return (AUTO, *CATALOG)


def engine_of(asr_model: str, model_path: Path | None = None) -> str:
    if asr_model != AUTO:
        return CATALOG[asr_model].engine
    if model_path is not None:
        model_name = Path(model_path).expanduser().name
        for model in CATALOG.values():
            if model_name == model.id:
                return model.engine
    return "whisper"


def _stored_default(settings: Settings) -> tuple[str | None, bool]:
    """The operator's chosen default, and whether a setting is there that cannot be understood."""
    try:
        document = json.loads((settings.data_dir / DEFAULT_MODEL_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    chosen = document.get("asr_model", "") if isinstance(document, dict) else ""
    if chosen is None:
        return None, False
    return (chosen, False) if isinstance(chosen, str) and chosen in CATALOG else (None, True)


def read_default_model(settings: Settings) -> str | None:
    return _stored_default(settings)[0]


def default_model_damaged(settings: Settings) -> bool:
    """A setting that cannot be read counts as no choice, and Settings has to say so."""
    return _stored_default(settings)[1]


def write_default_model(settings: Settings, asr_model: str | None) -> None:
    descriptor, temp = tempfile.mkstemp(dir=settings.data_dir, prefix=f".{DEFAULT_MODEL_FILE}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"asr_model": asr_model}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, settings.data_dir / DEFAULT_MODEL_FILE)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def model_path(settings: Settings, asr_model: str) -> Path:
    if asr_model == "whisper-large-v3":
        gpu_path = Path(settings.asr_gpu_model_path).expanduser().resolve(strict=False)
        if engine_for_path(settings, gpu_path) == "qwen":
            return gpu_path.parent / asr_model
    return getattr(settings, CATALOG[asr_model].path_setting)


def resolve_run(
    settings: Settings, profile: RuntimeProfile | None, asr_model: str
) -> dict[str, object]:
    """The model files and runtime one run uses; auto is the chosen default, else the hardware's."""
    if asr_model == AUTO:
        asr_model = read_default_model(settings) or AUTO
    if profile is None:
        path = settings.asr_gpu_model_path if asr_model == AUTO else model_path(settings, asr_model)
        return {
            "asr_model": asr_model,
            "model_path": path,
            "device": "auto",
            "compute_type": "default",
        }
    path = profile.model_path if asr_model == AUTO else model_path(settings, asr_model)
    if asr_model == AUTO and engine_for_path(settings, path) == "qwen":
        asr_model = "qwen3-asr-1.7b"
    engine = engine_of(asr_model, path)
    return {
        "asr_model": asr_model,
        "model_path": path,
        "device": profile.device,
        "compute_type": _QWEN_COMPUTE_TYPE if engine == "qwen" else profile.compute_type,
    }


def runtime_summary(settings: Settings, profile: RuntimeProfile) -> dict[str, str]:
    """The hardware default as it runs: the profile's compute type is for Whisper models."""
    summary = profile.public_summary()
    if engine_for_path(settings, profile.model_path) == "qwen":
        summary["compute_type"] = _QWEN_COMPUTE_TYPE
    return summary


def prepare_command(asr_model: str, device: str) -> str:
    """The command that prepares one model; it downloads weights and reads no Recording."""
    target = "" if asr_model == AUTO else f" --asr-model {asr_model}"
    # The default depends on the hardware the container sees; a named model does not.
    gpu = " -f compose.gpu.yaml" if asr_model == AUTO and device == "cuda" else ""
    return (
        f"docker compose -f compose.yaml{gpu} -f compose.model-prepare.yaml run --pull never --rm "
        '--no-deps --user "$(id -u):$(id -g)" -e HF_HOME=/tmp/huggingface -e HF_HUB_OFFLINE=0 '
        f"-e TRANSFORMERS_OFFLINE=0 worker python -m asr_studio.cli model prepare{target}"
    )


def model_choices(settings: Settings, profile: RuntimeProfile | None) -> list[dict[str, object]]:
    """Readiness of every choice, as the app container sees the shared read-only /models mount."""
    choices: list[dict[str, object]] = []
    operator_default = read_default_model(settings)
    for choice in selectable_models():
        run = resolve_run(settings, profile, choice)
        path, asr_model = Path(run["model_path"]), str(run["asr_model"])  # auto may name a model
        # A named command installs at the catalog path: elsewhere, prepare the hardware default.
        named = asr_model != AUTO and Path(model_path(settings, asr_model)) == path
        reason: str | None = None
        if profile is None:
            reason = "worker_unavailable"
        elif asr_model != AUTO and CATALOG[asr_model].gpu_only and profile.device != "cuda":
            reason = "gpu_required"
        else:
            try:
                prepared_engine_directory(engine_of(asr_model), path)
            except ModelConfigurationError:
                reason = "not_prepared"
        choices.append(
            {
                "id": choice,
                "chosen": choice == AUTO and operator_default is not None,
                # Without a worker the hardware default is unknown: do not guess a GPU model.
                "model_name": path.name if profile is not None else None,
                "ready": reason is None,
                "reason": reason,
                "prepare_command": (
                    prepare_command(asr_model if named else AUTO, profile.device)
                    if reason == "not_prepared"
                    else None
                ),
            }
        )
    return choices
