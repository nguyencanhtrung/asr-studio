from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import torch
from seeding import prepare_diarization_model

from asr_studio import diarization_runner
from asr_studio.diarization_runner import (
    DIARIZATION_ERROR_MESSAGES,
    DiarizationConfigurationError,
    DiarizationPhaseError,
    diarize_file,
    main,
    preflight_diarization_model,
    prepared_diarization_directory,
    safe_diarization_error,
)


def _prepared_model(path: Path) -> Path:
    return prepare_diarization_model(path)


def _install_pipeline_modules(monkeypatch, pipeline, *, check_environment=False):
    pyannote_package = ModuleType("pyannote")
    pyannote_package.__path__ = []
    pyannote_audio = ModuleType("pyannote.audio")

    def from_pretrained(*_args, **_kwargs):
        if check_environment:
            assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
            assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
            assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"
            assert os.environ["DO_NOT_TRACK"] == "1"
        return pipeline

    pyannote_audio.Pipeline = SimpleNamespace(from_pretrained=from_pretrained)
    torch = ModuleType("torch")
    torch.device = lambda name: f"device:{name}"
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_package)
    monkeypatch.setitem(sys.modules, "pyannote.audio", pyannote_audio)
    monkeypatch.setitem(sys.modules, "torch", torch)


def _audio_samples(frames=16000, *, channels=1, sample_rate=16000):
    # torchcodec 0.11.1 AudioSamples (torchcodec/_frame.py): data is a float32 tensor shaped
    # (num_channels, num_samples) and sample_rate is an int. The tensor is real; only the runner's
    # own `torch` import is faked.
    return SimpleNamespace(data=torch.zeros(channels, frames), sample_rate=sample_rate)


def _decoder_returning(samples):
    # torchcodec 0.11.1 AudioDecoder(source, *, stream_index, sample_rate, num_channels) and
    # get_all_samples() -> AudioSamples (torchcodec/decoders/_audio_decoder.py).
    class FakeAudioDecoder:
        calls = []

        def __init__(self, source, **kwargs):
            self.calls.append(("init", source, kwargs))

        def get_all_samples(self):
            self.calls.append(("get_all_samples",))
            return samples

    return FakeAudioDecoder


class _OpenFails:
    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("decoder internals with /private/path")


class _DecodeFails:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_all_samples(self):
        raise RuntimeError("decoder internals with /private/path")


def _install_audio_decoder(monkeypatch, decoder_type):
    torchcodec_package = ModuleType("torchcodec")
    torchcodec_package.__path__ = []
    torchcodec_decoders = ModuleType("torchcodec.decoders")
    torchcodec_decoders.AudioDecoder = decoder_type
    monkeypatch.setitem(sys.modules, "torchcodec", torchcodec_package)
    monkeypatch.setitem(sys.modules, "torchcodec.decoders", torchcodec_decoders)


class _NoTurns:
    def itertracks(self, *, yield_label):
        return iter(())


class _SpyPipeline:
    """Records what it is called with and returns an empty diarization."""

    def __init__(self):
        self.inputs = []

    def to(self, _device):
        return self

    def __call__(self, source):
        self.inputs.append(source)
        return SimpleNamespace(
            speaker_diarization=_NoTurns(),
            exclusive_speaker_diarization=_NoTurns(),
        )


def _diarize_with_fakes(tmp_path, monkeypatch, *, decoder_type, pipeline, events):
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"local audio")
    _install_pipeline_modules(monkeypatch, pipeline)
    _install_audio_decoder(monkeypatch, decoder_type)
    diarize_file(
        model_path=_prepared_model(tmp_path / "model"),
        audio_path=audio_path,
        device="cpu",
        emit=events.append,
    )


def test_runner_requires_a_prepared_local_community_directory(tmp_path):
    with pytest.raises(DiarizationConfigurationError):
        prepared_diarization_directory(tmp_path / "community-1")

    model = _prepared_model(tmp_path / "prepared")
    assert prepared_diarization_directory(model) == model.resolve()

    (model / "embedding/pytorch_model.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n", encoding="ascii"
    )
    with pytest.raises(DiarizationConfigurationError, match="Git LFS pointer"):
        prepared_diarization_directory(model)


def test_runner_loads_local_pipeline_with_telemetry_and_network_disabled(tmp_path, monkeypatch):
    model_dir = _prepared_model(tmp_path / "model")
    audio_path = tmp_path / "recording.m4a"
    audio_path.write_bytes(b"local audio")
    samples = _audio_samples()
    decoder_type = _decoder_returning(samples)
    observed = {}

    class Annotation:
        def itertracks(self, *, yield_label):
            assert yield_label is True
            return iter([(SimpleNamespace(start=1.25, end=2.75), "track", "SPEAKER_00")])

    class FakePipeline:
        def to(self, device):
            observed["device"] = device
            return self

        def __call__(self, source):
            observed["pipeline_input"] = source
            return SimpleNamespace(
                speaker_diarization=Annotation(),
                exclusive_speaker_diarization=Annotation(),
            )

    class FakePipelineType:
        @staticmethod
        def from_pretrained(source, *, token):
            assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
            assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
            assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"
            assert os.environ["DO_NOT_TRACK"] == "1"
            observed["model_path"] = source
            observed["token"] = token
            return FakePipeline()

    pyannote_package = ModuleType("pyannote")
    pyannote_package.__path__ = []
    pyannote_audio = ModuleType("pyannote.audio")
    pyannote_audio.Pipeline = FakePipelineType
    torch = ModuleType("torch")
    torch.device = lambda name: f"device:{name}"
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_package)
    monkeypatch.setitem(sys.modules, "pyannote.audio", pyannote_audio)
    monkeypatch.setitem(sys.modules, "torch", torch)
    _install_audio_decoder(monkeypatch, decoder_type)
    monkeypatch.setenv("HF_TOKEN", "setup-only-token")
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    events = []

    diarize_file(
        model_path=model_dir,
        audio_path=audio_path,
        device="cuda",
        emit=events.append,
    )

    pipeline_input = observed.pop("pipeline_input")
    assert isinstance(pipeline_input, dict)
    assert pipeline_input.keys() == {"waveform", "sample_rate"}
    assert pipeline_input["waveform"] is samples.data
    assert pipeline_input["sample_rate"] == 16000
    assert decoder_type.calls == [
        ("init", str(audio_path.resolve()), {"sample_rate": 16000, "num_channels": 1}),
        ("get_all_samples",),
    ]
    assert observed == {
        "model_path": str(model_dir.resolve()),
        "token": False,
        "device": "device:cuda",
    }
    assert events[0] == {"type": "stage", "stage": "loading_diarization_model"}
    assert events[1] == {"type": "stage", "stage": "decoding_input"}
    assert events[2] == {"type": "stage", "stage": "diarizing"}
    assert events[3] == {
        "type": "turn",
        "kind": "regular",
        "start_seconds": 1.25,
        "end_seconds": 2.75,
        "speaker_id": "SPEAKER_00",
    }
    assert events[4]["kind"] == "exclusive"
    assert events[5] == {
        "type": "complete",
        "regular_turn_count": 1,
        "exclusive_turn_count": 1,
    }
    assert "HF_TOKEN" not in os.environ
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_runner_maps_audio_decoder_import_failure_to_a_stable_safe_error(monkeypatch):
    original_import = builtins.__import__

    def no_torchcodec(name, *args, **kwargs):
        if name == "torchcodec.decoders":
            raise OSError("native library path must stay in logs")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_torchcodec)

    with pytest.raises(DiarizationPhaseError) as raised:
        from asr_studio.diarization_runner import load_audio_decoder

        load_audio_decoder()

    assert raised.value.code == "diarization_decoder_unavailable"
    assert str(raised.value) == DIARIZATION_ERROR_MESSAGES[raised.value.code]
    assert "native library path" in str(raised.value.__cause__)


def test_runtime_preflight_loads_offline_pipeline_and_moves_to_selected_device(
    tmp_path, monkeypatch
):
    model = _prepared_model(tmp_path / "model")
    observed = {}

    class FakePipeline:
        def to(self, device):
            observed["device"] = device

    pipeline = FakePipeline()
    _install_pipeline_modules(monkeypatch, pipeline, check_environment=True)
    monkeypatch.setattr(diarization_runner, "load_audio_decoder", lambda: object())
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    preflight_diarization_model(model_path=model, device="cuda")

    assert observed == {"device": "device:cuda"}
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_runtime_preflight_cli_does_not_require_audio(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "model")
    calls = []
    monkeypatch.setattr(
        diarization_runner,
        "preflight_diarization_model",
        lambda **kwargs: calls.append(kwargs),
    )

    assert main(["--check-runtime", "--model", str(model), "--device", "cuda"]) == 0

    assert calls == [{"model_path": model, "device": "cuda"}]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_model_load_failure_uses_its_phase_code_and_hides_vendor_text(tmp_path, monkeypatch):
    model = _prepared_model(tmp_path / "model")

    class BrokenPipelineType:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            raise RuntimeError("token=secret at /private/model/cache")

    pyannote_package = ModuleType("pyannote")
    pyannote_package.__path__ = []
    pyannote_audio = ModuleType("pyannote.audio")
    pyannote_audio.Pipeline = BrokenPipelineType
    torch = ModuleType("torch")
    torch.device = lambda name: name
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_package)
    monkeypatch.setitem(sys.modules, "pyannote.audio", pyannote_audio)
    monkeypatch.setitem(sys.modules, "torch", torch)

    with pytest.raises(DiarizationPhaseError) as raised:
        diarization_runner._load_community_pipeline(model, "cpu")

    assert raised.value.code == "diarization_model_load_failed"
    assert str(raised.value) == DIARIZATION_ERROR_MESSAGES[raised.value.code]
    assert "token=secret" in str(raised.value.__cause__)


def test_cuda_move_failure_uses_cuda_phase_code(tmp_path, monkeypatch):
    model = _prepared_model(tmp_path / "model")

    class BrokenPipeline:
        def to(self, _device):
            raise RuntimeError("CUDA driver detail")

    _install_pipeline_modules(monkeypatch, BrokenPipeline())

    with pytest.raises(DiarizationPhaseError) as raised:
        diarization_runner._load_community_pipeline(model, "cuda")

    assert raised.value.code == "diarization_cuda_failed"
    assert str(raised.value) == DIARIZATION_ERROR_MESSAGES[raised.value.code]


def test_decoder_resamples_and_downmixes_the_whole_recording_in_one_pass(tmp_path):
    samples = _audio_samples()
    decoder_type = _decoder_returning(samples)
    audio_path = tmp_path / "recording.m4a"

    waveform = diarization_runner._decode_waveform(audio_path, decoder_type)

    assert waveform is samples.data
    assert decoder_type.calls == [
        ("init", str(audio_path), {"sample_rate": 16000, "num_channels": 1}),
        ("get_all_samples",),
    ]


@pytest.mark.parametrize(
    "decoder_type",
    [
        pytest.param(_OpenFails, id="open-fails"),
        pytest.param(_DecodeFails, id="decode-fails"),
        pytest.param(_decoder_returning(_audio_samples(frames=0)), id="no-samples"),
        pytest.param(_decoder_returning(_audio_samples(channels=2)), id="not-mono"),
        pytest.param(_decoder_returning(_audio_samples(sample_rate=48000)), id="wrong-rate"),
        pytest.param(
            _decoder_returning(SimpleNamespace(data=torch.zeros(1, 16000, 1), sample_rate=16000)),
            id="not-2d",
        ),
        pytest.param(
            _decoder_returning(SimpleNamespace(data=None, sample_rate=16000)), id="no-data"
        ),
    ],
)
def test_input_decode_failure_uses_its_phase_code(tmp_path, decoder_type):
    with pytest.raises(DiarizationPhaseError) as raised:
        diarization_runner._decode_waveform(tmp_path / "recording.wav", decoder_type)

    assert raised.value.code == "diarization_input_decode_failed"
    assert raised.value.stage == "decoding_input"
    assert str(raised.value) == DIARIZATION_ERROR_MESSAGES[raised.value.code]


def test_input_decode_failure_stops_before_the_diarizing_stage(tmp_path, monkeypatch):
    pipeline = _SpyPipeline()
    events = []

    with pytest.raises(DiarizationPhaseError) as raised:
        _diarize_with_fakes(
            tmp_path, monkeypatch, decoder_type=_DecodeFails, pipeline=pipeline, events=events
        )

    assert raised.value.code == "diarization_input_decode_failed"
    assert raised.value.stage == "decoding_input"
    assert events == [
        {"type": "stage", "stage": "loading_diarization_model"},
        {"type": "stage", "stage": "decoding_input"},
    ]
    assert pipeline.inputs == []


def test_pipeline_receives_the_decoded_waveform_and_never_a_file_path(tmp_path, monkeypatch):
    samples = _audio_samples()
    pipeline = _SpyPipeline()

    _diarize_with_fakes(
        tmp_path,
        monkeypatch,
        decoder_type=_decoder_returning(samples),
        pipeline=pipeline,
        events=[],
    )

    (pipeline_input,) = pipeline.inputs
    assert not isinstance(pipeline_input, str | os.PathLike)
    assert isinstance(pipeline_input, dict)
    assert pipeline_input.keys() == {"waveform", "sample_rate"}
    assert pipeline_input["waveform"] is samples.data
    assert pipeline_input["sample_rate"] == 16000


def test_pipeline_failure_uses_the_diarizing_stage_and_hides_vendor_text(tmp_path, monkeypatch):
    class BrokenPipeline(_SpyPipeline):
        def __call__(self, source):
            raise RuntimeError("vendor detail at /private/model/cache")

    events = []

    with pytest.raises(DiarizationPhaseError) as raised:
        _diarize_with_fakes(
            tmp_path,
            monkeypatch,
            decoder_type=_decoder_returning(_audio_samples()),
            pipeline=BrokenPipeline(),
            events=events,
        )

    assert raised.value.code == "diarization_failed"
    assert raised.value.stage == "diarizing"
    assert str(raised.value) == DIARIZATION_ERROR_MESSAGES[raised.value.code]
    assert events[-1] == {"type": "stage", "stage": "diarizing"}


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (
            "diarization_input_decode_failed",
            DIARIZATION_ERROR_MESSAGES["diarization_input_decode_failed"],
            "diarization_input_decode_failed",
        ),
        ("diarization_cuda_failed", "vendor detail with a local path", "diarization_failed"),
        ("unknown_vendor_code", "vendor detail", "diarization_failed"),
    ],
)
def test_child_error_allowlist_requires_known_code_and_exact_safe_message(code, message, expected):
    result_code, result_message = safe_diarization_error(code, message)

    assert result_code == expected
    assert result_message == DIARIZATION_ERROR_MESSAGES[expected]


def test_runner_default_safe_stderr_and_explicit_verbose_traceback(tmp_path, monkeypatch, capsys):
    model = _prepared_model(tmp_path / "model")
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"audio")

    def fail(**_kwargs):
        raise DiarizationPhaseError("diarization_input_decode_failed", "decoding_input")

    monkeypatch.setattr("asr_studio.diarization_runner.diarize_file", fail)
    diagnostic_id = "a" * 32
    meeting_id = "b" * 32
    assert (
        main(
            [
                "--model",
                str(model),
                "--audio",
                str(audio),
                "--device",
                "cpu",
                "--diagnostic-id",
                diagnostic_id,
                "--meeting-id",
                meeting_id,
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "type": "error",
        "code": "diarization_input_decode_failed",
        "message": DIARIZATION_ERROR_MESSAGES["diarization_input_decode_failed"],
    }
    context = json.loads(captured.err.splitlines()[-1])
    assert context == {
        "event": "diarization_error",
        "stage": "decoding_input",
        "code": "diarization_input_decode_failed",
    }
    assert diagnostic_id not in captured.err
    assert meeting_id not in captured.err
    assert "Traceback" not in captured.err

    def fail_with_vendor_detail(**_kwargs):
        raise RuntimeError("verbose diagnostic sentinel token=secret /private/model")

    monkeypatch.setattr("asr_studio.diarization_runner.diarize_file", fail_with_vendor_detail)
    assert (
        main(
            [
                "--verbose",
                "--model",
                str(model),
                "--audio",
                str(audio),
                "--device",
                "cpu",
            ]
        )
        == 1
    )
    verbose_captured = capsys.readouterr()
    assert "Traceback" in verbose_captured.err
    assert "verbose diagnostic sentinel" in verbose_captured.err
