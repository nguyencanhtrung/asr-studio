"""External inference fixtures for the native extension integration test.

The app scheduler, task workers, source/recipe checks and publication stay real.
Only model output and the external notes provider response are synthetic.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace

from seeding import prepare_diarization_model
from test_chatgpt_provider import connect

from asr_studio import worker
from asr_studio.chatgpt import ChatGPTProvider
from asr_studio.runtime import RuntimeProfile, write_runtime_profile


def processing_settings(settings, directory, adapter):
    model = directory / "whisper-small"
    model.mkdir()
    (model / "model.bin").write_bytes(b"synthetic-model")
    for name, value in {
        "config.json": {"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []},
        "tokenizer.json": {"model": {"type": "BPE"}},
        "preprocessor_config.json": {
            "feature_size": 128,
            "sampling_rate": 16000,
            "hop_length": 160,
            "n_fft": 400,
            "chunk_length": 30,
        },
    }.items():
        (model / name).write_text(json.dumps(value))
    settings = replace(
        settings,
        asr_model_path=model,
        asr_cpu_model_path=model,
        diarization_model_path=prepare_diarization_model(directory / "diarization"),
    )
    provider = ChatGPTProvider(
        settings.provider_dir,
        callback_url="http://127.0.0.1:8765/auth/chatgpt/callback",
        adapter=adapter,
    )
    provider.ensure()
    connect(provider)
    return settings


def _events_command(events):
    return [
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1], flush=True)",
        "\n".join(json.dumps(event) for event in events),
    ]


@contextmanager
def synthetic_processing_worker(settings, directory, monkeypatch):
    """Run real worker admission/publication with declared external boundaries."""
    monkeypatch.setenv("ASR_PROVIDER_DIR", str(settings.provider_dir))
    monkeypatch.setattr(
        worker,
        "_runner_command",
        lambda *_: _events_command(
            [
                {
                    "type": "segment",
                    "id": "a" * 32,
                    "start_seconds": 0.1,
                    "end_seconds": 0.5,
                    "text": "Synthetic meeting transcript.",
                },
                {"type": "complete", "segment_count": 1, "language_probability": 0.99},
            ]
        ),
    )
    monkeypatch.setattr(
        worker,
        "_diarization_command",
        lambda *_: _events_command(
            [
                {"type": "complete", "regular_turn_count": 0, "exclusive_turn_count": 0},
            ]
        ),
    )
    notes_child = directory / "synthetic_notes_provider.py"
    notes_child.write_text("""import sys
from pathlib import Path
from asr_studio import notes_runner
from asr_studio.chatgpt import ChatGPTProvider
from asr_studio.notes_document import SECTIONS

class SyntheticProvider(ChatGPTProvider):
    def generate_notes_document(self, *, snapshot, connection_version, model,
                                reasoning_effort, output_language, on_coverage, on_progress):
        pin = self.notes_task_pin()
        assert pin['connection_version'] == connection_version
        assert pin['model'] == model and pin['reasoning_effort'] == reasoning_effort
        segments = snapshot['segments']
        assert len(segments) == 1 and segments[0]['text'] == 'Synthetic meeting transcript.'
        on_coverage({'version': 1, 'segment_count': 1, 'chunks': [
            {'index': 1, 'owned': [0, 1], 'overlap': None, 'parts': [], 'provider_call_index': 1}]})
        return {'schema_version': 1, **{key: [] for key, _ in SECTIONS},
                'overview': [{'text': 'Synthetic meeting notes.',
                              'source_ids': [segments[0]['id']]}]}

notes_runner.ChatGPTProvider = SyntheticProvider
raise SystemExit(notes_runner.run_notes_task(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
""")
    monkeypatch.setattr(
        worker,
        "_notes_command",
        lambda task, owner, data: [sys.executable, str(notes_child), str(data), task["id"], owner],
    )
    lock = worker.acquire_worker_lock(settings.data_dir)
    assert lock is not None
    write_runtime_profile(settings.data_dir, RuntimeProfile("cpu", "int8", settings.asr_model_path))
    worker.write_heartbeat(settings.data_dir, "idle")
    stop = threading.Event()
    thread = threading.Thread(target=worker._worker_loop, args=(settings, lock, stop), daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)
        os.close(lock)
        assert not thread.is_alive(), "synthetic worker did not stop"
