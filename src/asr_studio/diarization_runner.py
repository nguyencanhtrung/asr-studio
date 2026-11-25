"""Local-only Community-1 child process and its newline-delimited JSON protocol."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from asr_studio.safe_logging import emit_safe_log

COMMUNITY_1_REQUIRED_FILES = (
    Path("config.yaml"),
    Path("embedding/pytorch_model.bin"),
    Path("plda/plda.npz"),
    Path("plda/xvec_transform.npz"),
    Path("segmentation/pytorch_model.bin"),
)
_GIT_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_SAMPLE_RATE = 16000
DIARIZATION_ERROR_MESSAGES = {
    "diarization_decoder_unavailable": (
        "The local audio decoder is unavailable. "
        "Check the application's audio runtime dependencies."
    ),
    "diarization_model_load_failed": (
        "The local Community-1 model could not be loaded. "
        "Check that the gated model assets are complete and readable."
    ),
    "diarization_cuda_failed": (
        "Community-1 could not run on the selected CUDA device. "
        "Check the CUDA runtime or select CPU."
    ),
    "diarization_input_decode_failed": (
        "The meeting recording could not be decoded. Re-upload a valid audio file and retry."
    ),
    "diarization_failed": "Local diarization failed. Check the worker logs and try again.",
}


class DiarizationConfigurationError(RuntimeError):
    """The configured local Community-1 directory is missing or incomplete."""


class DiarizationPhaseError(RuntimeError):
    """A failure at a known diarization phase with a stable, safe code."""

    def __init__(self, code: str, stage: str) -> None:
        if code not in DIARIZATION_ERROR_MESSAGES:
            code = "diarization_failed"
        self.code = code
        self.stage = stage
        super().__init__(DIARIZATION_ERROR_MESSAGES[code])


def safe_diarization_error(code: object, message: object) -> tuple[str, str]:
    """Return a child error only when its code and exact message are allowlisted."""
    if isinstance(code, str) and isinstance(message, str):
        expected_message = DIARIZATION_ERROR_MESSAGES.get(code)
        if expected_message is not None and message == expected_message:
            return code, expected_message
    fallback_code = "diarization_failed"
    return fallback_code, DIARIZATION_ERROR_MESSAGES[fallback_code]


def prepared_diarization_directory(path: Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        is_directory = resolved.is_dir()
    except (OSError, RuntimeError) as error:
        raise DiarizationConfigurationError(
            "The local Community-1 model directory is unavailable."
        ) from error
    if not is_directory:
        raise DiarizationConfigurationError("The local Community-1 model directory is incomplete.")
    for relative_path in COMMUNITY_1_REQUIRED_FILES:
        try:
            candidate = (resolved / relative_path).resolve(strict=True)
            candidate.relative_to(resolved)
            if not candidate.is_file():
                raise OSError
            with candidate.open("rb") as model_file:
                prefix = model_file.read(len(_GIT_LFS_POINTER_PREFIX))
        except (OSError, RuntimeError, ValueError):
            raise DiarizationConfigurationError(
                f"The local Community-1 model is missing {relative_path.as_posix()}."
            ) from None
        if not prefix:
            raise DiarizationConfigurationError(
                f"The local Community-1 file {relative_path.as_posix()} is empty."
            )
        if prefix.startswith(_GIT_LFS_POINTER_PREFIX):
            raise DiarizationConfigurationError(
                f"The local Community-1 file {relative_path.as_posix()} is still a Git LFS pointer."
            )
    return resolved


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _set_offline_environment() -> None:
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYANNOTE_METRICS_ENABLED": "0",
            "DO_NOT_TRACK": "1",
            "ORT_DISABLE_TELEMETRY": "1",
        }
    )
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "HF_API_TOKEN"):
        os.environ.pop(name, None)


def load_audio_decoder():
    """Load torchcodec's decoder without exposing native loader errors to users."""
    try:
        from torchcodec.decoders import AudioDecoder
    except (ImportError, OSError, RuntimeError) as error:
        raise DiarizationPhaseError(
            "diarization_decoder_unavailable", "loading_audio_decoder"
        ) from error
    return AudioDecoder


def _load_community_pipeline(model_path: Path, device: str):
    try:
        resolved_model = prepared_diarization_directory(model_path)
        import torch
        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained(str(resolved_model), token=False)
        if pipeline is None:
            raise RuntimeError("Community-1 did not return a pipeline")
    except Exception as error:
        raise DiarizationPhaseError(
            "diarization_model_load_failed", "loading_diarization_model"
        ) from error

    selected_device = "cuda" if device == "cuda" else "cpu"
    stage = (
        "moving_diarization_model_to_cuda"
        if selected_device == "cuda"
        else "moving_diarization_model_to_cpu"
    )
    try:
        pipeline.to(torch.device(selected_device))
    except Exception as error:
        code = (
            "diarization_cuda_failed"
            if selected_device == "cuda"
            else "diarization_model_load_failed"
        )
        raise DiarizationPhaseError(code, stage) from error
    return pipeline, selected_device


def preflight_diarization_model(*, model_path: Path, device: str) -> None:
    """Load local Community-1 offline and move it to the selected device."""
    _set_offline_environment()
    load_audio_decoder()
    _load_community_pipeline(model_path, device)


def _decode_waveform(audio_path: Path, decoder_type):
    """Decode the whole recording once into a 16 kHz mono float32 waveform held in memory.

    The decoder downmixes and resamples, so the full-rate samples never reach Python. The price is
    one in-memory copy, about 0.9 GiB at the default 4 h cap. Given a path instead, pyannote
    re-decodes the file from the start for every chunk it crops, and diarization time grows with
    the square of the duration.
    """
    try:
        samples = decoder_type(
            str(audio_path), sample_rate=_SAMPLE_RATE, num_channels=1
        ).get_all_samples()
        waveform = samples.data
        if (
            samples.sample_rate != _SAMPLE_RATE
            or waveform.ndim != 2
            or waveform.shape[0] != 1
            or waveform.shape[1] < 1
        ):
            raise ValueError("Audio decoder returned invalid samples")
    except Exception as error:
        raise DiarizationPhaseError("diarization_input_decode_failed", "decoding_input") from error
    return waveform


def diarize_file(
    *,
    model_path: Path,
    audio_path: Path,
    device: str,
    emit: Callable[[dict[str, Any]], None] = _emit,
) -> None:
    """Load one prepared local model, emit its turns, and release it when this process exits."""
    stage = "loading_audio_decoder"
    try:
        _set_offline_environment()
        emit({"type": "stage", "stage": "loading_diarization_model"})
        decoder_type = load_audio_decoder()
        pipeline, _ = _load_community_pipeline(model_path, device)

        stage = "decoding_input"
        try:
            resolved_audio = Path(audio_path).resolve(strict=True)
            if not resolved_audio.is_file():
                raise FileNotFoundError("The recording is unavailable")
        except OSError as error:
            raise DiarizationPhaseError("diarization_input_decode_failed", stage) from error
        emit({"type": "stage", "stage": "decoding_input"})
        waveform = _decode_waveform(resolved_audio, decoder_type)

        emit({"type": "stage", "stage": "diarizing"})
        stage = "diarizing"
        try:
            output = pipeline({"waveform": waveform, "sample_rate": _SAMPLE_RATE})
        except Exception as error:
            raise DiarizationPhaseError("diarization_failed", stage) from error
    except DiarizationPhaseError:
        raise
    except Exception as error:
        raise DiarizationPhaseError("diarization_failed", stage) from error

    try:
        annotations = (
            ("regular", output.speaker_diarization),
            ("exclusive", output.exclusive_speaker_diarization),
        )
        counts: dict[str, int] = {}
        for kind, annotation in annotations:
            count = 0
            for turn, _, speaker_label in annotation.itertracks(yield_label=True):
                start_seconds = _finite(turn.start)
                end_seconds = _finite(turn.end)
                if start_seconds is None or end_seconds is None or end_seconds <= start_seconds:
                    raise RuntimeError("Community-1 returned an invalid speaker turn.")
                count += 1
                emit(
                    {
                        "type": "turn",
                        "kind": kind,
                        "start_seconds": start_seconds,
                        "end_seconds": end_seconds,
                        "speaker_id": str(speaker_label),
                    }
                )
            counts[kind] = count
        emit(
            {
                "type": "complete",
                "regular_turn_count": counts["regular"],
                "exclusive_turn_count": counts["exclusive"],
            }
        )
    except Exception as error:
        raise DiarizationPhaseError("diarization_failed", "collecting_result") from error


def _write_error_context(*, diagnostic_id: str, meeting_id: str, stage: str, code: str) -> None:
    del diagnostic_id, meeting_id
    emit_safe_log(event="diarization_error", stage=stage, code=code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asr-studio-diarization-runner")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--device", required=True, choices=("cpu", "cuda", "auto"))
    parser.add_argument("--check-runtime", action="store_true")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show local preflight and diarization traceback details",
    )
    parser.add_argument("--diagnostic-id", default="unknown")
    parser.add_argument("--meeting-id", default="unknown")
    args = parser.parse_args(argv)
    try:
        if args.check_runtime:
            preflight_diarization_model(model_path=args.model, device=args.device)
        else:
            if args.audio is None:
                parser.error("--audio is required unless --check-runtime is used")
            diarize_file(
                model_path=args.model,
                audio_path=args.audio,
                device=args.device,
            )
    except DiarizationPhaseError as error:
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        code, message = safe_diarization_error(error.code, error.args[0])
        _write_error_context(
            diagnostic_id=args.diagnostic_id,
            meeting_id=args.meeting_id,
            stage=error.stage,
            code=code,
        )
        _emit({"type": "error", "code": code, "message": message})
        return 1
    except Exception:
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        code, message = safe_diarization_error(None, None)
        _write_error_context(
            diagnostic_id=args.diagnostic_id,
            meeting_id=args.meeting_id,
            stage="unknown",
            code=code,
        )
        _emit({"type": "error", "code": code, "message": message})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
