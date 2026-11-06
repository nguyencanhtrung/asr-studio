#!/usr/bin/env python3
"""Profile local Community-1 memory by phase using a repeated, authorized PCM WAV."""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.metadata
import json
import os
import resource
import threading
import time
import wave
from pathlib import Path
from typing import Any

SAMPLE_INTERVAL_SECONDS = 0.02
MAX_DURATION_SECONDS = 14400


def _hwm_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MemoryProbe:
    """Combine sampled RSS with kernel high-water readings around native calls."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stack: list[str] = []
        self.peaks: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self.stop = threading.Event()
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def sample(self) -> int:
        current = int(Path("/proc/self/statm").read_text().split()[1]) * self.page_size
        with self.lock:
            for name in ["whole_process", *self.stack]:
                self.peaks[name] = max(self.peaks.get(name, 0), current)
        return current

    def _watch(self) -> None:
        while not self.stop.wait(SAMPLE_INTERVAL_SECONDS):
            self.sample()

    def wrap(self, name: str, original):
        @functools.wraps(original, updated=())
        def measured(*args, **kwargs):
            before = self.sample()
            hwm_before = _hwm_kib()
            started = time.monotonic()
            shapes = [list(value.shape) for value in args if hasattr(value, "shape")][:4]
            with self.lock:
                self.stack.append(name)
            try:
                return original(*args, **kwargs)
            finally:
                after = self.sample()
                with self.lock:
                    self.stack.pop()
                    self.events.append(
                        {
                            "phase": name,
                            "start_seconds": started - self.started,
                            "elapsed_seconds": time.monotonic() - started,
                            "rss_before_bytes": before,
                            "rss_after_bytes": after,
                            "hwm_before_kib": hwm_before,
                            "hwm_after_kib": _hwm_kib(),
                            "input_shapes": shapes,
                        }
                    )

        return measured

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=24)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    try:
        with wave.open(str(args.audio), "rb") as recording:
            duration = recording.getnframes() / recording.getframerate()
    except (OSError, wave.Error, ZeroDivisionError) as error:
        parser.error(f"Cannot read the authorized PCM WAV: {error}")
    if duration <= 0 or duration * args.repeat > MAX_DURATION_SECONDS:
        parser.error("Repeated waveform must be positive and no longer than 14400 seconds")

    from asr_studio.diarization_runner import (
        COMMUNITY_1_REQUIRED_FILES,
        _decode_waveform,
        _load_community_pipeline,
        _set_offline_environment,
        load_audio_decoder,
    )

    _set_offline_environment()
    import pyannote.audio.pipelines.clustering as clustering
    import torch
    from scipy.cluster import hierarchy
    from scipy.spatial import distance

    probe = MemoryProbe()
    try:
        pipeline, _ = probe.wrap("load_model", _load_community_pipeline)(args.model, args.device)
        short = probe.wrap("decode_source", _decode_waveform)(args.audio, load_audio_decoder())
        waveform = probe.wrap("repeat_waveform", lambda value: value.repeat(1, args.repeat))(short)
        del short
        for attribute in ("get_segmentations", "get_embeddings", "reconstruct"):
            setattr(pipeline, attribute, probe.wrap(attribute, getattr(pipeline, attribute)))
        clustering_type = type(pipeline.clustering)
        clustering_type.__call__ = probe.wrap("clustering", clustering_type.__call__)
        clustering_type.constrained_argmax = probe.wrap(
            "constrained_assignment", clustering_type.constrained_argmax
        )
        for attribute in ("linkage", "fcluster", "cluster_vbx", "cdist"):
            setattr(clustering, attribute, probe.wrap(attribute, getattr(clustering, attribute)))
        distance.pdist = probe.wrap("pdist", distance.pdist)
        hierarchy._hierarchy.fast_linkage = probe.wrap(
            "fast_linkage", hierarchy._hierarchy.fast_linkage
        )
        plda_type = type(pipeline.clustering.plda)
        plda_type.__call__ = probe.wrap("plda", plda_type.__call__)
        output = probe.wrap("pipeline", pipeline)({"waveform": waveform, "sample_rate": 16000})
        probe.sample()
        result = {
            "kind": "Community-1 on a public repeated synthetic waveform; no ASR or notes",
            "audio_seconds": waveform.shape[1] / 16000,
            "sample_rate": 16000,
            "waveform_bytes": waveform.numel() * waveform.element_size(),
            "source_wav_sha256": _sha256(args.audio),
            "repeat_count": args.repeat,
            "source_wav_seconds": duration,
            "model_sha256": {str(p): _sha256(args.model / p) for p in COMMUNITY_1_REQUIRED_FILES},
            "measurement": (
                "Sampled RSS can miss native peaks while the GIL is held; "
                "use kernel HWM before/after each call for peak attribution. "
                "Phase RSS includes nested calls."
            ),
            "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "gpu": torch.cuda.get_device_name(0) if args.device == "cuda" else None,
            "device": args.device,
            "torch_cpu_threads": torch.get_num_threads(),
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("pyannote-audio", "torch", "scipy", "numpy", "torchcodec")
            },
            "phase_sampled_peak_rss_bytes": probe.peaks,
            "events": probe.events,
            "hwm_kib": _hwm_kib(),
            "regular_turn_count": len(list(output.speaker_diarization.itertracks())),
            "exclusive_turn_count": len(list(output.exclusive_speaker_diarization.itertracks())),
            "elapsed_seconds": time.monotonic() - probe.started,
        }
        print(json.dumps(result, separators=(",", ":")), flush=True)
    finally:
        probe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
