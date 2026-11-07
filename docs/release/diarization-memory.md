# Diarization memory at the recording limit

Issue [#97](https://github.com/nguyencanhtrung/asr-studio/issues/97) permits documenting the RAM requirement instead of changing clustering. This change takes that path. It does not reduce memory use or change speaker assignment.

## Deployment budget

For recordings near the default four-hour limit, provision **at least 8 GiB of system RAM and leave at least 6 GiB available for the transcription worker**. These are provisional deployment budgets, not a measured minimum or a guarantee for every recording or ASR model. Reserve additional memory for the OS, the app, other workloads and any larger model footprint. Do not cap this worker at 4 GiB for four-hour recordings. Reducing the recording-duration limit is an alternative when that budget is unavailable.

The historical four-hour synthetic composite reached 4.93 GiB runner peak RSS, as recorded in [the archived performance report](archive/v0.1.0/performance-v0.1.0.md) and #97. The waveform fix already removed repeated decoding; it did not remove the clustering allocation. That recording was a composite. A current public synthetic diarization-only run is reported below; full ASR/notes acceptance remains separate.

## Measured cause

The installed pyannote.audio 4.0.7 `VBxClustering.__call__` normalizes the retained embeddings and calls SciPy centroid linkage with Euclidean distance. The installed SciPy implementation first forms a condensed float64 pair-distance vector (`pdist`), then allocates additional quadratic working memory in `fast_linkage`. The vector alone contains `n * (n - 1) / 2` entries. The allocation depends on retained embeddings, which vary with content and local speaker activity; audio duration alone is insufficient to guarantee an upper bound.

The following CPU-only synthetic measurements use deterministic normalized float32 embeddings. They do not include audio, model loading, waveform/segmentation buffers, full VBx refinement or reconstruction. Each phase and size runs in a fresh subprocess with one BLAS thread; RSS is the Linux process high-water mark. Raw values and versions are in [the measurement artifact](diarization-memory-profile-2026-11-06.json).

| Retained embeddings | Condensed vector (MiB) | `pdist` RSS increase (MiB) | Additional `fast_linkage` RSS increase (MiB) | Full centroid linkage phase RSS increase (MiB) |
| ---: | ---: | ---: | ---: | ---: |
| 2,000 | 15.3 | 20.9 | 11.8 | 33.5 |
| 4,000 | 61.0 | 71.2 | 53.4 | 133.8 |
| 8,000 | 244.1 | 267.4 | 228.7 | 495.7 |

The phase peaks are separate measurements and must not be summed as an end-to-end RSS result. RSS includes native allocations that `tracemalloc` does not fully report. The cut/unique controls in the artifact did not add a process high-water increase after linkage for these synthetic inputs. This establishes a reachable quadratic allocation mechanism; it does not attribute every byte of the historical meeting peak or establish speaker accuracy.

## Four-hour public synthetic GPU pipeline

A separate run used image `sha256:5bf256540b117017779d4f9715506ec67422ac3c6c0cb1e04c9c545d13c42870` built from application revision `a807dbc19f938243f5f7ebfc2a9124e1b9593814` and the model hashes in [the full-pipeline artifact](diarization-four-hour-profile-2026-11-06.json). The public input uses source rows 0 and 1 of `ademax/vivos-vie-speech2text@1c937fd`, assembled into a 600-second PCM WAV and repeated 24 times into a 4-hour waveform. Its float32 waveform occupies 0.858 GiB. It is a repeated reading sample, not a continuous meeting or speaker-accuracy gold set.

On `NVIDIA GeForce RTX 5080`, the actual Community-1 pipeline reached **4.561 GiB process peak RSS**, above the old 4 GiB target. There were 14,391 retained training embeddings and 103,543,245 condensed float64 pair distances (0.771 GiB). Kernel high-water readings locate the additional working peak inside `fast_linkage`:

| Phase | Process HWM before call (GiB) | Process HWM at return (GiB) |
| --- | ---: | ---: |
| `get_segmentations` | 1.942 | 2.617 |
| `get_embeddings` | 2.648 | 3.274 |
| `pdist` | 3.274 | 3.817 |
| `fast_linkage` | 3.886 | 4.561 |
| `fcluster` | 4.561 | 4.561 |
| `plda` | 4.561 | 4.561 |
| `cluster_vbx` | 4.561 | 4.561 |
| `cdist` | 4.561 | 4.561 |
| `constrained_assignment` | 4.561 | 4.561 |
| `reconstruct` | 4.561 | 4.561 |
| `reconstruct` | 4.561 | 4.561 |

HWM is cumulative for the process; the rows must not be summed or read as each phase's independent allocation. The RSS sampler can miss native C allocations while the GIL is held, so attribution uses kernel HWM before/after the wrapped native call. The artifact includes both sampled RSS and kernel HWM. PLDA, VBx, assignment and both reconstruction calls produced no higher process peak on this input. Other recordings can retain different numbers of embeddings or clusters.

This fixture differs from the historical composite. The new result supplies current phase attribution; it does not measure an optimization or establish a worst-case memory bound. The run used a container with no network and a fixed memory limit, recorded in the artifact. GPU timing is not used as a speed benchmark. No clustering result or algorithm was changed by the instrumentation.

## Reproduce and follow up

Run the checked-in profiler with the locked environment, for example:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  timeout --kill-after=5s 90s .venv/bin/python scripts/profile-diarization-memory.py \
  --phase linkage --n 8000 --dimension 256 --seed 97
```

Use `--phase normalize`, `pdist`, `fast_linkage` or `cut` to isolate the controls. The profiler is a Linux diagnostic and performs no model/provider request. The raw artifact records the exact script hash, package versions and source hashes; RSS can differ across allocators and machines.

Windowed clustering or embedding subsampling could bound this allocation, but either changes speaker clustering and needs an explicit identity/merge design plus real speaker-quality acceptance. No such algorithm change is included. A future reduction should measure the full pipeline at the configured duration cap and record retained-embedding counts as well as peak RSS.

To measure the full pipeline, supply a local 600-second PCM WAV as `PUBLIC_WAV`, the prepared model parent as `ASR_MODEL_HOST_PATH`, and a local matching image as `ASR_IMAGE`. The profiler checks the repeated duration before model loading. Run under a deadline and a container memory limit:

```sh
timeout --kill-after=10s 900s docker run --rm --pull=never --interactive \
  --network none --gpus all --cap-drop ALL --read-only \
  --memory 12g --memory-swap 12g --tmpfs /tmp:mode=1777,size=1g \
  --mount "type=bind,src=$ASR_MODEL_HOST_PATH/speaker-diarization-community-1,dst=/models/community,readonly" \
  --mount "type=bind,src=$PUBLIC_WAV,dst=/input.wav,readonly" \
  --entrypoint python "$ASR_IMAGE" - \
  --model /models/community --audio /input.wav --repeat 24 --device cuda \
  < scripts/profile-diarization-pipeline.py > /tmp/asr-diarization-memory.json
```

The output contains shapes, resource measurements and hashes, with no audio, embeddings or transcript. The raw artifact pins the exact profiler SHA. Model preparation remains a separate operator step.

To recreate the exact public WAV, download the pinned [VIVOS Parquet](https://huggingface.co/datasets/ademax/vivos-vie-speech2text/resolve/1c937fd/data/test-00000-of-00001-dc934696ee903158.parquet) as `vivos-test.parquet`. The optional preparation uses a temporary PyArrow environment; it does not add a production dependency:

```sh
uv run --no-project --with pyarrow==26.0.1 python - <<'PYWAV'
import hashlib, io, wave
from pathlib import Path
import pyarrow.parquet as pq
source = Path("vivos-test.parquet")
assert hashlib.sha256(source.read_bytes()).hexdigest() == "27d99b4b5f1f55dfcffa151abe58f5fa53bd2c572290524e0046cf9b23bd449a"
chunks = []
for row in pq.read_table(source).slice(0, 2).to_pylist():
    with wave.open(io.BytesIO(row["audio"]["bytes"])) as recording:
        assert (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) == (1, 2, 16000)
        chunks.append(recording.readframes(recording.getnframes()))
base = ((b"".join(chunks) + bytes(16000)) * 14)[:85 * 32000]
frames = (base * 8)[:600 * 32000]
with wave.open("public-600s.wav", "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(16000)
    output.writeframes(frames)
assert hashlib.sha256(Path("public-600s.wav").read_bytes()).hexdigest() == "f488d28ada19b19dbe34ff5ba67566f9f04b850471d36d13df635d3da51bfe4d"
PYWAV
```
