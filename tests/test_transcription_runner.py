from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from asr_studio import runner
from asr_studio.runner import (
    ModelConfigurationError,
    prepared_engine_directory,
    prepared_model_directory,
    prepared_qwen_directory,
    transcribe_file,
)


def _prepared_model(path: Path) -> Path:
    path.mkdir()
    (path / "model.bin").write_bytes(b"local model")
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


def test_runner_requires_a_prepared_local_model_directory(tmp_path):
    with pytest.raises(ModelConfigurationError):
        prepared_model_directory(tmp_path / "hub-model-name")

    model = _prepared_model(tmp_path / "prepared")
    assert prepared_model_directory(model) == model.resolve()


def test_model_validation_rejects_vocabulary_fallback_and_bad_preprocessor(tmp_path):
    legacy_model = _prepared_model(tmp_path / "legacy")
    (legacy_model / "tokenizer.json").unlink()
    (legacy_model / "vocabulary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ModelConfigurationError):
        prepared_model_directory(legacy_model)

    malformed_model = _prepared_model(tmp_path / "malformed")
    (malformed_model / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ModelConfigurationError):
        prepared_model_directory(malformed_model)


def test_runner_reports_fixed_error_to_stderr_without_exposing_exception(
    tmp_path, monkeypatch, capsys
):
    model_dir = _prepared_model(tmp_path / "model")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")

    def fail_transcription(**_kwargs):
        raise RuntimeError("diagnostic traceback sentinel token=secret /private/model")

    monkeypatch.setattr(runner, "transcribe_file", fail_transcription)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "asr-studio-runner",
            "--model",
            str(model_dir),
            "--audio",
            str(audio),
            "--device",
            "cpu",
            "--compute-type",
            "int8",
        ],
    )

    assert runner.main() == 1
    captured = capsys.readouterr()
    assert captured.err == (
        '{"event":"runner_error","stage":"unknown","code":"transcription_failed"}\n'
    )
    assert "Traceback" not in captured.err
    assert "diagnostic traceback sentinel" not in captured.err
    assert "token=secret" not in captured.err
    assert "/private/model" not in captured.err
    assert '"code":"transcription_failed"' in captured.out
    assert "diagnostic traceback sentinel" not in captured.out


def test_runner_loads_local_only_and_emits_timestamps_and_uncertainty_measures(
    tmp_path, monkeypatch
):
    model_dir = _prepared_model(tmp_path / "model")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    observed: dict[str, object] = {}

    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    class FakeWhisperModel:
        def __init__(self, model_path, **kwargs):
            assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
            observed["model_path"] = model_path
            observed["kwargs"] = kwargs

        def transcribe(self, audio_path, **kwargs):
            observed["audio_path"] = audio_path
            observed["transcribe_kwargs"] = kwargs
            return iter(
                [
                    SimpleNamespace(
                        start=1.125,
                        end=2.75,
                        text=" Xin chào.",
                        avg_logprob=-0.45,
                        no_speech_prob=0.08,
                        compression_ratio=1.2,
                    )
                ]
            ), SimpleNamespace(language_probability=0.97)

    fake_package = ModuleType("faster_whisper")
    fake_package.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_package)
    events = []

    transcribe_file(
        model_path=model_dir,
        audio_path=audio,
        device="cpu",
        compute_type="int8",
        emit=events.append,
    )

    assert observed["model_path"] == str(model_dir.resolve())
    assert observed["kwargs"] == {
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
    }
    assert observed["audio_path"] == str(audio.resolve())
    assert observed["transcribe_kwargs"] == {
        "language": None,
        "task": "transcribe",
        "multilingual": True,
        "beam_size": 5,
        "vad_filter": False,
        "condition_on_previous_text": False,
    }
    assert events[0] == {"type": "stage", "stage": "loading_model"}
    assert events[1] == {"type": "stage", "stage": "transcribing"}
    segment = events[2]
    assert segment["type"] == "segment"
    assert len(segment["id"]) == 32
    assert segment["start_seconds"] == 1.125
    assert segment["end_seconds"] == 2.75
    assert segment["avg_logprob"] == -0.45
    assert segment["no_speech_prob"] == 0.08
    assert segment["compression_ratio"] == 1.2
    assert events[3] == {
        "type": "complete",
        "segment_count": 1,
        "language_probability": 0.97,
    }
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def _prepared_qwen(path: Path) -> Path:
    path.mkdir()
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


def _fake_audio_modules(monkeypatch, chunks, observed=None):
    """16 kHz samples are indices, so each decode can tell which chunk it received."""
    audio_module = ModuleType("faster_whisper.audio")
    audio_module.decode_audio = lambda path, sampling_rate: list(range(sampling_rate * 100))
    vad_module = ModuleType("faster_whisper.vad")

    class VadOptions:
        def __init__(self, **kwargs):
            if observed is not None:
                observed["vad"] = kwargs

    vad_module.VadOptions = VadOptions
    vad_module.get_speech_timestamps = lambda audio, options: [
        {"start": int(start * 16000), "end": int(end * 16000)} for start, end in chunks
    ]
    package = ModuleType("faster_whisper")
    package.audio = audio_module
    package.vad = vad_module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_module)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad_module)
    return package


@pytest.mark.parametrize(
    "missing",
    [
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "generation_config.json",
        "processor_config.json",
        "chat_template.jinja",
    ],
)
def test_qwen_needs_every_file_the_processor_and_model_load(tmp_path, missing):
    model = _prepared_qwen(tmp_path / "qwen")
    (model / missing).unlink()
    with pytest.raises(ModelConfigurationError):
        prepared_qwen_directory(model)


def test_an_unreadable_model_directory_is_not_prepared_rather_than_a_crash(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    model = _prepared_qwen(tmp_path / "qwen")
    model.chmod(0)
    try:
        for engine in ("qwen", "whisper", "whisper_vi"):
            with pytest.raises(ModelConfigurationError):
                prepared_engine_directory(engine, model)
    finally:
        model.chmod(0o700)


def test_qwen_validation_rejects_a_whisper_directory_and_whisper_rejects_qwen(tmp_path):
    whisper = _prepared_model(tmp_path / "whisper")
    qwen = _prepared_qwen(tmp_path / "qwen")
    assert prepared_qwen_directory(qwen) == qwen.resolve()
    with pytest.raises(ModelConfigurationError):
        prepared_qwen_directory(whisper)
    with pytest.raises(ModelConfigurationError):
        prepared_model_directory(qwen)


def test_qwen_runs_local_only_keeps_time_order_and_retries_implausible_languages(
    tmp_path, monkeypatch
):
    model_dir = _prepared_qwen(tmp_path / "qwen")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    from transformers.models.qwen3_asr.processing_qwen3_asr import Qwen3ASRProcessor

    chunks = [
        (0.0, 4.0),
        (5.0, 6.0),
        (7.0, 10.0),
        (11.0, 12.0),
        (13.0, 14.0),
        (15.0, 16.0),
        (17.0, 18.0),
    ]
    _fake_audio_modules(monkeypatch, chunks)
    detected = {0: "Vietnamese", 5: "Chinese", 7: "English", 11: "Thai", 13: None, 15: "English"}
    spoken = {15: "", 17: "cảm ơn " * 50}
    detected[17] = "Vietnamese"
    observed: dict[str, object] = {"load": [], "forced": []}

    class Output:
        def __getitem__(self, _key):
            return self

    class Inputs(dict):
        def to(self, *_args):
            return self

    class Processor:
        tokenizer = SimpleNamespace(pad_token_id=0)

        def apply_transcription_request(self, pieces, language=None):
            self.batch = [piece[0] // 16000 for piece in pieces]
            self.language = language
            if language is not None:
                observed["forced"].append((self.batch, language[0]))
            return Inputs(input_ids=SimpleNamespace(shape=(len(pieces), 3)))

        def decode(self, _generated, skip_special_tokens):
            assert skip_special_tokens
            if self.language is not None:
                # With the language forced, the model emits the text alone.
                return [f"{second}:{self.language[0]}" for second in self.batch]
            return [
                ""
                if second == 13
                else f"language {detected[second]}<asr_text>{spoken.get(second, f'{second}:auto')}"
                for second in self.batch
            ]

        def parse_output(self, raw):
            # The real parser: it collapses repeated phrases, which is why the runner must not
            # measure repetition on its result.
            return Qwen3ASRProcessor.parse_output(None, raw)

    processor = Processor()

    class Model:
        device = "cuda"
        dtype = "bfloat16"

        def to(self, device):
            observed["device"] = device
            return self

        def generate(self, **kwargs):
            return Output()

    def loader(name, result):
        def from_pretrained(path, **kwargs):
            observed["load"].append((name, path, kwargs))
            return result

        return SimpleNamespace(from_pretrained=from_pretrained)

    transformers = ModuleType("transformers")
    transformers.AutoProcessor = loader("processor", processor)
    transformers.AutoModelForMultimodalLM = loader("model", Model())
    torch = ModuleType("torch")
    torch.bfloat16 = "bfloat16"
    torch.inference_mode = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    events = []

    transcribe_file(
        model_path=model_dir,
        audio_path=audio,
        device="cuda",
        compute_type="bfloat16",
        engine="qwen",
        emit=events.append,
    )

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert observed["device"] == "cuda"
    assert [(name, kwargs.get("local_files_only")) for name, _path, kwargs in observed["load"]] == [
        ("processor", True),
        ("model", True),
    ]
    assert not any("trust_remote_code" in kwargs for _name, _path, kwargs in observed["load"])
    segments = [event for event in events if event["type"] == "segment"]
    # The chunk nearest to 5 s (tie: the earlier) is Vietnamese; those nearest to 11 and 13 s are
    # English. The chunk at 15 s is believable but silent: it is dropped, not stored blank.
    assert [(segment["start_seconds"], segment["text"]) for segment in segments] == [
        (0.0, "0:auto"),
        (5.0, "5:vi"),
        (7.0, "7:auto"),
        (11.0, "11:en"),
        (13.0, "13:en"),
        (17.0, segments[-1]["text"]),
    ]
    assert segments[1]["avg_logprob"] is None
    assert segments[1]["no_speech_prob"] is None
    assert segments[1]["compression_ratio"] < 2.4
    # The parser hid the loop from the text; the measure still sees what the decoder produced.
    assert len(segments[-1]["text"]) < 40
    assert segments[-1]["compression_ratio"] > 2.4
    assert events[-1] == {"type": "complete", "segment_count": 6, "language_probability": None}


def test_phowhisper_decodes_each_vad_chunk_in_vietnamese_with_real_timestamps(
    tmp_path, monkeypatch
):
    model_dir = _prepared_model(tmp_path / "model")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    observed: dict[str, object] = {"calls": []}
    package = _fake_audio_modules(monkeypatch, [(1.0, 3.0), (4.0, 6.0)], observed)

    class FakeWhisperModel:
        def __init__(self, _model_path, **kwargs):
            observed["kwargs"] = kwargs

        def transcribe(self, piece, **kwargs):
            observed["calls"].append(kwargs)
            part = SimpleNamespace(
                text=" Xin chào " if piece[0] == 16000 else "  ",
                avg_logprob=-0.2,
                no_speech_prob=0.1,
                compression_ratio=1.1,
            )
            return iter([part]), SimpleNamespace(language_probability=1.0)

    package.WhisperModel = FakeWhisperModel
    events = []

    transcribe_file(
        model_path=model_dir,
        audio_path=audio,
        device="cuda",
        compute_type="float16",
        engine="whisper_vi",
        emit=events.append,
    )

    assert observed["vad"]["min_silence_duration_ms"] == 1500
    assert observed["vad"]["max_speech_duration_s"] == 28
    assert observed["calls"][0] == {
        "language": "vi",
        "task": "transcribe",
        "multilingual": False,
        "beam_size": 5,
        "vad_filter": False,
        "without_timestamps": True,
        "condition_on_previous_text": False,
    }
    segments = [event for event in events if event["type"] == "segment"]
    assert [(s["start_seconds"], s["end_seconds"], s["text"]) for s in segments] == [
        (1.0, 3.0, "Xin chào")
    ]
    assert segments[0]["avg_logprob"] == -0.2
    assert events[-1] == {"type": "complete", "segment_count": 1, "language_probability": None}
