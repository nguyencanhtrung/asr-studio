"""The ASR models an operator can choose for one processing run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from asr_studio.config import Settings
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory
from asr_studio.runtime import RuntimeProfile

AUTO = "auto"
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


def engine_of(asr_model: str) -> str:
    return "whisper" if asr_model == AUTO else CATALOG[asr_model].engine


def model_path(settings: Settings, asr_model: str) -> Path:
    return getattr(settings, CATALOG[asr_model].path_setting)


def resolve_run(
    settings: Settings, profile: RuntimeProfile | None, asr_model: str
) -> dict[str, object]:
    """The model files and runtime one run uses; auto stays the worker's hardware default."""
    if profile is None:
        path = settings.asr_gpu_model_path if asr_model == AUTO else model_path(settings, asr_model)
        return {
            "asr_model": asr_model,
            "model_path": path,
            "device": "auto",
            "compute_type": "default",
        }
    return {
        "asr_model": asr_model,
        "model_path": profile.model_path if asr_model == AUTO else model_path(settings, asr_model),
        "device": profile.device,
        "compute_type": (
            _QWEN_COMPUTE_TYPE if engine_of(asr_model) == "qwen" else profile.compute_type
        ),
    }


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
    for asr_model in selectable_models():
        path = Path(resolve_run(settings, profile, asr_model)["model_path"])
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
                "id": asr_model,
                # Without a worker the hardware default is unknown: do not guess a GPU model.
                "model_name": path.name if profile is not None else None,
                "ready": reason is None,
                "reason": reason,
                "prepare_command": (
                    prepare_command(asr_model, profile.device) if reason == "not_prepared" else None
                ),
            }
        )
    return choices
