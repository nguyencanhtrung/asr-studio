"""Local-only ASR child process and its newline-delimited JSON protocol."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
import zlib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from asr_studio.safe_logging import emit_safe_log

_REQUIRED_MODEL_FILES = (
    "model.bin",
    "config.json",
    "tokenizer.json",
    "preprocessor_config.json",
)


# How a model turns audio into segments:
#   whisper     the decoder's own segments (Whisper large-v3, Whisper small)
#   whisper_vi  one Vietnamese-only decode per VAD chunk (PhoWhisper emits ~30 s segments otherwise)
#   qwen        batched decodes of VAD chunks (its forced aligner has no Vietnamese)
ENGINES = ("whisper", "whisper_vi", "qwen")
_QWEN_REQUIRED_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "processor_config.json",
    "chat_template.jinja",
)
_SAMPLE_RATE = 16000
# Short chunks padded to 30 s make PhoWhisper invent sentences: 1.5 s of silence merges most of
# them (measured: 38 -> 9 implausibly dense chunks on two 70-minute meetings).
_WHISPER_VI_MIN_SILENCE_MS = 1500
_QWEN_MIN_SILENCE_MS = 500
_QWEN_BATCH_SIZE = 4
_QWEN_WINDOW_CHUNKS = 32
_QWEN_LANGUAGES = {"Vietnamese": "vi", "English": "en"}


class ModelConfigurationError(RuntimeError):
    """The configured local model path is missing or is not a prepared ASR model."""


def prepared_model_directory(path: Path) -> Path:
    model_path = Path(path).expanduser()
    try:
        resolved = model_path.resolve(strict=True)
    except OSError as error:
        raise ModelConfigurationError("The local model directory is unavailable.") from error
    if not resolved.is_dir():
        raise ModelConfigurationError("The local model path must be a directory.")
    if any(not (resolved / filename).is_file() for filename in _REQUIRED_MODEL_FILES):
        raise ModelConfigurationError("The local model directory is incomplete.")
    try:
        tokenizer_config = json.loads((resolved / "tokenizer.json").read_text(encoding="utf-8"))
        model_config = json.loads((resolved / "config.json").read_text(encoding="utf-8"))
        preprocessor_config = json.loads(
            (resolved / "preprocessor_config.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelConfigurationError("The local model configuration is invalid.") from error
    if not isinstance(tokenizer_config, dict) or not isinstance(
        tokenizer_config.get("model"), dict
    ):
        raise ModelConfigurationError("The local model tokenizer is invalid.")
    if not isinstance(model_config, dict) or any(
        key not in model_config for key in ("lang_ids", "suppress_ids", "suppress_ids_begin")
    ):
        raise ModelConfigurationError("The local model configuration is incomplete.")
    if not isinstance(preprocessor_config, dict):
        raise ModelConfigurationError("The local model preprocessor configuration is invalid.")
    for key in ("feature_size", "sampling_rate", "hop_length", "n_fft", "chunk_length"):
        value = preprocessor_config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelConfigurationError(
                "The local model preprocessor configuration is incomplete."
            )
    if preprocessor_config["sampling_rate"] != 16000:
        raise ModelConfigurationError("The local model must use a 16 kHz preprocessor.")
    return resolved


def prepared_qwen_directory(path: Path) -> Path:
    model_path = Path(path).expanduser()
    try:
        resolved = model_path.resolve(strict=True)
    except OSError as error:
        raise ModelConfigurationError("The local model directory is unavailable.") from error
    if any(not (resolved / filename).is_file() for filename in _QWEN_REQUIRED_FILES):
        raise ModelConfigurationError("The local model directory is incomplete.")
    try:
        config = json.loads((resolved / "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelConfigurationError("The local model configuration is invalid.") from error
    if not isinstance(config, dict) or config.get("model_type") != "qwen3_asr":
        raise ModelConfigurationError("The local model is not a Qwen3-ASR model.")
    return resolved


def prepared_engine_directory(engine: str, path: Path) -> Path:
    try:
        return prepared_qwen_directory(path) if engine == "qwen" else prepared_model_directory(path)
    except OSError as error:
        # Path.is_file() raises on a directory this user may not read.
        raise ModelConfigurationError("The local model directory is unreadable.") from error


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _finite_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _segment_event(
    start: object,
    end: object,
    text: str,
    *,
    avg_logprob: object = None,
    no_speech_prob: object = None,
    compression_ratio: object = None,
) -> dict[str, Any]:
    return {
        "type": "segment",
        "id": uuid.uuid4().hex,
        "start_seconds": _finite_or_none(start),
        "end_seconds": _finite_or_none(end),
        "text": text,
        "avg_logprob": _finite_or_none(avg_logprob),
        "no_speech_prob": _finite_or_none(no_speech_prob),
        "compression_ratio": _finite_or_none(compression_ratio),
    }


def _speech_chunks(audio: Any, min_silence_ms: int) -> list[tuple[int, int]]:
    """Silero VAD (bundled with faster-whisper) as sample ranges short enough for one decode."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        min_silence_duration_ms=min_silence_ms, speech_pad_ms=200, max_speech_duration_s=28
    )
    return [(chunk["start"], chunk["end"]) for chunk in get_speech_timestamps(audio, options)]


def _transcribe_whisper_vi(
    model: Any, audio_path: Path, emit: Callable[[dict[str, Any]], None]
) -> int:
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(audio_path), sampling_rate=_SAMPLE_RATE)
    segment_count = 0
    for start, end in _speech_chunks(audio, _WHISPER_VI_MIN_SILENCE_MS):
        parts, _info = model.transcribe(
            audio[start:end],
            language="vi",
            task="transcribe",
            multilingual=False,
            beam_size=5,
            vad_filter=False,
            without_timestamps=True,
            condition_on_previous_text=False,
        )
        parts = list(parts)
        text = " ".join(part.text.strip() for part in parts).strip()
        if not text:
            continue
        segment_count += 1
        emit(
            _segment_event(
                start / _SAMPLE_RATE,
                end / _SAMPLE_RATE,
                text,
                avg_logprob=sum(part.avg_logprob for part in parts) / len(parts),
                no_speech_prob=parts[0].no_speech_prob,
                compression_ratio=max(part.compression_ratio for part in parts),
            )
        )
    return segment_count


def _compression_ratio(text: str) -> float:
    encoded = text.encode("utf-8")
    return len(encoded) / len(zlib.compress(encoded))


def _transcribe_qwen(
    model_path: Path, audio_path: Path, emit: Callable[[dict[str, Any]], None]
) -> int:
    import torch
    from faster_whisper.audio import decode_audio
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    emit({"type": "stage", "stage": "loading_model"})
    processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(
        str(model_path), dtype=torch.bfloat16, local_files_only=True
    ).to("cuda")
    emit({"type": "stage", "stage": "transcribing"})
    audio = decode_audio(str(audio_path), sampling_rate=_SAMPLE_RATE)
    chunks = _speech_chunks(audio, _QWEN_MIN_SILENCE_MS)

    def decode(indices: list[int], language: str | None = None) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        # Shortest first keeps padding, and so VRAM, close to what each batch needs.
        ordered = sorted(indices, key=lambda index: chunks[index][1] - chunks[index][0])
        for offset in range(0, len(ordered), _QWEN_BATCH_SIZE):
            batch = ordered[offset : offset + _QWEN_BATCH_SIZE]
            longest = max(chunks[index][1] - chunks[index][0] for index in batch) / _SAMPLE_RATE
            options = {} if language is None else {"language": [language] * len(batch)}
            inputs = processor.apply_transcription_request(
                [audio[chunks[index][0] : chunks[index][1]] for index in batch], **options
            ).to(model.device, model.dtype)
            with torch.inference_mode():
                # 20 tokens per second bounds a decoder that has started to loop.
                output = model.generate(**inputs, max_new_tokens=max(64, int(20 * longest)))
            generated = output[:, inputs["input_ids"].shape[1] :]
            # parse_output collapses repeated phrases, which would hide a looping decoder from
            # the compression ratio: measure what the decoder produced, show the parsed text.
            raw = processor.decode(generated, skip_special_tokens=True)
            for index, text, parsed in zip(batch, raw, processor.parse_output(raw), strict=True):
                results[index] = {**parsed, "raw": text}
        return results

    segment_count = 0
    # ponytail: fixed window keeps progress streaming in time order; shortest-first across the
    # whole meeting would batch tighter but publish nothing until the end.
    for first in range(0, len(chunks), _QWEN_WINDOW_CHUNKS):
        indices = list(range(first, min(first + _QWEN_WINDOW_CHUNKS, len(chunks))))
        results = decode(indices)
        # Per-chunk detection names Chinese, Malay or Thai on 5-25% of short Vietnamese chunks.
        # Decode those again in the language of the nearest chunk it detected believably.
        believable = [i for i in indices if results[i].get("language") in _QWEN_LANGUAGES]
        retries: dict[str, list[int]] = defaultdict(list)
        for index in indices:
            if index not in believable:
                nearest = min(believable, key=lambda other: abs(other - index), default=None)
                language = results[nearest]["language"] if nearest is not None else "Vietnamese"
                retries[_QWEN_LANGUAGES[language]].append(index)
        for language, group in retries.items():
            results.update(decode(group, language))
        for index in indices:
            text = str(results[index].get("transcription") or "").strip()
            if not text:
                continue
            segment_count += 1
            # ponytail: no avg_logprob or no_speech_prob. Its logprobs are not comparable with
            # Whisper's (median -4.4 on clear speech) and would flag 84% of segments uncertain;
            # only loops are detected until a calibrated measure exists.
            emit(
                _segment_event(
                    chunks[index][0] / _SAMPLE_RATE,
                    chunks[index][1] / _SAMPLE_RATE,
                    text,
                    compression_ratio=_compression_ratio(results[index]["raw"]),
                )
            )
    return segment_count


def transcribe_file(
    *,
    model_path: Path,
    audio_path: Path,
    device: str,
    compute_type: str,
    engine: str = "whisper",
    emit: Callable[[dict[str, Any]], None] = _emit,
) -> None:
    """Load one prepared local model, transcribe one local file and emit measured results."""
    resolved_model = prepared_engine_directory(engine, model_path)
    resolved_audio = Path(audio_path).resolve(strict=True)
    if not resolved_audio.is_file():
        raise FileNotFoundError("The recording is unavailable.")

    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)

    if engine == "qwen":
        segment_count = _transcribe_qwen(resolved_model, resolved_audio, emit)
        emit({"type": "complete", "segment_count": segment_count, "language_probability": None})
        return

    from faster_whisper import WhisperModel

    _emit_with = emit
    _emit_with({"type": "stage", "stage": "loading_model"})
    model = WhisperModel(
        str(resolved_model),
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )
    _emit_with({"type": "stage", "stage": "transcribing"})
    if engine == "whisper_vi":
        segment_count = _transcribe_whisper_vi(model, resolved_audio, emit)
        emit({"type": "complete", "segment_count": segment_count, "language_probability": None})
        return
    segments, info = model.transcribe(
        str(resolved_audio),
        language=None,
        task="transcribe",
        multilingual=True,
        beam_size=5,
        vad_filter=False,
    )
    segment_count = 0
    for segment in segments:
        segment_count += 1
        _emit_with(
            _segment_event(
                segment.start,
                segment.end,
                str(segment.text),
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
                compression_ratio=segment.compression_ratio,
            )
        )
    _emit_with(
        {
            "type": "complete",
            "segment_count": segment_count,
            "language_probability": _finite_or_none(info.language_probability),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="asr-studio-runner")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--device", required=True, choices=("cpu", "cuda", "auto"))
    parser.add_argument("--compute-type", required=True)
    parser.add_argument("--engine", choices=ENGINES, default="whisper")
    args = parser.parse_args()
    try:
        transcribe_file(
            model_path=args.model,
            audio_path=args.audio,
            device=args.device,
            compute_type=args.compute_type,
            engine=args.engine,
        )
    except ModelConfigurationError:
        _emit(
            {
                "type": "error",
                "code": "model_unavailable",
                "message": "The local ASR model is unavailable.",
            }
        )
        return 2
    except Exception:
        emit_safe_log(event="runner_error", stage="unknown", code="transcription_failed")
        _emit(
            {
                "type": "error",
                "code": "transcription_failed",
                "message": "Local transcription failed. Check the worker logs and try again.",
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
