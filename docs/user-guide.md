# User guide

This self-hosted web app stores meeting recordings and makes transcripts on the local machine.
It uses faster-whisper for speech recognition and pyannote Community-1 for speaker diarization.
ChatGPT or Google Antigravity writes meeting notes in English or Vietnamese.

## Run locally

Requirements:

- Docker Engine and Docker Compose 2.30 or later.
- For a GPU: an NVIDIA driver and the NVIDIA Container Toolkit. Without a GPU, the app uses the CPU.

```sh
cp .env.example .env
./scripts/start.sh
```

The default app URL is `http://127.0.0.1:8765`. Compose publishes the port on loopback only, and
the app checks the exact configured Host and Origin. For a browser on another machine, keep the
Compose bind on loopback, terminate TLS at a reverse proxy, and set `ASR_PUBLIC_URL` to the exact
public HTTPS origin (for example, `https://asr.example.test`). Proxy that origin to
`127.0.0.1:8765` and preserve the public Host header; do not enable forwarded-header trust or expose
the Compose port directly.

### Start modes

| Mode                   | Behavior                                                                                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--source` (default) | Builds `ASR_IMAGE` from the checkout.                                                                                                                    |
| `--pull`             | Pulls `ASR_IMAGE`. The image must have a version tag (`:v0.1.3`) or a digest (`@sha256:...`). The script rejects `local`, `latest` and `edge`. |
| `--offline`          | Uses a local image and local models. It does not build, pull or download, it leaves the model installer stopped and it does not block the [network](#network-access), so **Settings → ASR Model** shows the prepare command.                                                                                  |

App, transcription worker, notes service and model installer always use the same `ASR_IMAGE`. Before it starts the services, the script:

1. Compares the app version and schema version in the image with the checkout.
2. Compares the image label `org.opencontainers.image.revision` with the checkout commit. A
   mismatch stops the script. An `unknown` revision gives only a warning. A checkout has a
   revision only when its directory is the root of a git repository.
3. Downloads a missing default ASR model (`--source` and `--pull` only). Only the `model prepare`
   step and the `model-installer` service download from the model hub.
4. Waits until the services are healthy.
5. Checks that the transcription worker runs, that the notes service is ready (or a legacy
   combined worker is serving notes), and that both local models are readable through the
   read-only mount.

After these checks pass, an interactive terminal shows the access link and a QR code. In a pipe,
CI or redirect, the script hides the token. To show it:

```sh
docker compose exec -T app python -m asr_studio.cli access show
```

To check a running installation:

```sh
docker compose exec -T app python -m asr_studio.cli installation check
docker compose exec -T app python -m asr_studio.cli version --json
```

Use `./scripts/start.sh` for a new installation. A plain `docker run` app container does not
start the worker or mount the models.

### Compute backend

The script detects the backend with the runtime image:

| Host                         | Device | Compute type | Model                              |
| ---------------------------- | ------ | ------------ | ---------------------------------- |
| NVIDIA GPU visible to Docker | CUDA   | `bfloat16` | `qwen3-asr-1.7b`                |
| No GPU                       | CPU    | `int8`     | `whisper-small` (lower accuracy) |

If the host has a GPU but Docker cannot access it, setup stops with an NVIDIA Container Toolkit
error. It does not fall back to the CPU.

### Install a release

The commands below install the ASR Studio release `v0.2.3`. For a real installation, use the tag,
commit or digest from the release notes. Do not overwrite old tags.

`v0.1.3` predates the rename: its Python module is `asr_en_vi`, and its old
`asr-en-vi` image is no longer published. To run it, check out that tag and build from
source with its Compose file and CLI instructions. The current checkout uses `asr_studio`
and must be rebuilt with `./scripts/start.sh --source` and `ASR_IMAGE=asr-studio:local`.

Keep `COMPOSE_PROJECT_NAME` and `ASR_MODEL_HOST_PATH` the same on each run, so that Compose
uses the same volumes and model directory. Prepare the ASR model while online. Download
Community-1 only after you accept its conditions (see
[Speaker diarization model](#speaker-diarization-model)).

Before upgrading an existing installation, finish or cancel pending Notes tasks and back up
the data volume. Role migration preserves task/source records, but a task accepted with an old
prompt can fail with `notes_contract_changed` after the prompt upgrade. Use **Create notes**
again from the retained transcript in the GUI. Saved transcripts and Notes revisions remain.
For schema rollback, restore the matching pre-upgrade database/data backup; see
[the Notes service ADR](adr/0013-notes-service-boundary.md).

Build from source:

```sh
git clone --branch v0.2.3 --depth 1 https://github.com/nguyencanhtrung/asr-studio.git
cd asr-studio
export COMPOSE_PROJECT_NAME=asr-source-v1
export ASR_MODEL_HOST_PATH="$PWD/models"
export ASR_IMAGE=asr-studio:v0.2.3
./scripts/start.sh --source
```

Pull the public image (same checkout):

```sh
export COMPOSE_PROJECT_NAME=asr-pull-v1
export ASR_PORT=8766
export ASR_MODEL_HOST_PATH="$PWD/models"
export ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.3
./scripts/start.sh --pull
```

A different project name gives separate data, token and provider volumes. Port 8766 prevents a
conflict with the source installation. The local tag `asr-studio:v0.2.3` shows that the image
comes from source, not from GHCR. To fix an exact artifact, set `ASR_IMAGE` to the published
digest (`ghcr.io/nguyencanhtrung/asr-studio@sha256:...`).

On a different machine, clone the same tag, install Docker and prepare Community-1. Do not copy
`.env` or the `auth` and `provider` volumes from an old installation.

After you prepare the models and disconnect the network, run in the same checkout:

```sh
./scripts/start.sh --offline
```

Open the access link of the new installation. Connect ChatGPT or Google with your own account.
Then use an authorized recording to test import, transcript, notes and export. A healthy status
does not prove that a provider works.

### Offline smoke test (optional)

For Linux with NVIDIA. It needs Docker, Chromium with Playwright, prepared models, and an
authorized WAV file with two speakers (2–90 seconds, 25 MiB maximum). It writes JSON, logs and a
report to the output directory.

```sh
./scripts/offline-smoke.py \
  --image "$ASR_IMAGE" \
  --models "$ASR_MODEL_HOST_PATH" \
  --audio ./authorized-two-voice.wav \
  --output /tmp/asr-offline-smoke-evidence \
  --mode offline
```

- `--mode pull` pulls with a new empty `DOCKER_CONFIG` and records `anonymous_pull`. A local
  registry login thus cannot hide a private image. When `DOCKER_HOST` is not set, the smoke uses
  the endpoint of the current Docker context for the pull only. A TLS context must set
  `DOCKER_HOST`, `DOCKER_CERT_PATH` and `DOCKER_TLS_VERIFY`.
- A failed pull does not always mean a private image. The registry or the Docker daemon can be
  unreachable. See the redacted stderr in `smoke.log`.
- The smoke records `image_revision_parity` when possible.
- In the offline phase, the smoke requests notes with a connected ChatGPT session. It expects
  status `failed` with code `provider_unavailable`, no change to saved notes, and no outbound
  calls.

#### Egress check

Use `--check-egress` for the first real transcription when you need a metadata-only check while
the app and worker have network access:

```sh
./scripts/offline-smoke.py \
  --image "$ASR_IMAGE" \
  --models "$ASR_MODEL_HOST_PATH" \
  --audio ./authorized-two-voice-long.wav \
  --output /tmp/asr-egress-smoke-evidence \
  --mode offline \
  --check-egress
```

The egress path accepts a WAV from 2 to 1800 seconds and keeps the existing 25 MiB byte limit. The
guard requires more than 8 seconds between the first and last actual `diarizing` API observations.
Late `complete` polls and cleanup time do not increase this lower-bound measurement. Use a longer
authorized recording when the observed interval is too short. These limits and the acceptance
requirements are tracked in [issue #92](https://github.com/nguyencanhtrung/asr-studio/issues/92).

The smoke starts one disposable observer for `app` and one for `worker`. Each observer shares only
the target container's network and PID namespaces. It uses a root UID with `NET_RAW`, `SYS_PTRACE`
and `DAC_READ_SEARCH` after dropping all other capabilities. It does not use privileged mode, host
PID, or host filesystem mounts. The observer reads the shared `/proc` namespace to map socket
inodes to process metadata. The approved proc access means this helper must not be described as
unable to read target files exposed through `/proc`.

The policy records IP, port, exact owned-stack or resolver name, SNI, direction and process
metadata. It never records packet payloads. The external allowlist is empty for this smoke because
the run makes no provider request. Loopback, the verified app/worker addresses, the exact resolver
addresses and exact `app`, `worker` and `localhost` names are internal. A Docker DNS query is
identified by the verified resolver IP even when its destination port was translated before
capture; the response from source port 53 is kept as inbound UDP without a guessed name. Incoming
client traffic and app responses on the app listener are not treated as initiated egress. Worker
incoming traffic still has to match an exact owned or resolver endpoint. Every other TCP or UDP
endpoint, including unowned private addresses and `mobile.events.data.microsoft.com`, fails the
check.

Before the task, each observer must see both sides of a known `app:8765` connection: a packet and
a socket record with a non-null process PID. The observer queries AF_PACKET drop statistics and
fails closed on a drop, malformed evidence or helper failure. TLS inspection is limited to a
ClientHello SNI when the handshake is visible. Encrypted payloads, later TLS names and certificate
content are not inspected.

At shutdown the helper drains queued packets and sends a final capture marker while it is still
running. The controller acknowledges that marker before the helper exits to confirm it consumed
all preceding metadata. Missing acknowledgement, an early stop or
missing final health evidence fails the smoke. Every signal or forced cleanup first verifies the
helper's project ownership labels.

Socket samples omit `TIME_WAIT` rows, which have no owning process; packet observation retains
their connection attempts. Reader-size or queue failures remain fatal and keep their specific
reason even if cleanup also fails.

### Troubleshooting

| Message            | Check                                                               |
| ------------------ | ------------------------------------------------------------------- |
| Worker not ready   | The `worker` service and the shared `data` volume.              |
| Notes not ready    | The `notes-service` health and the provider volume/session.      |
| Model not readable | `ASR_MODEL_HOST_PATH` and the `/models` mounts in app and worker. |

### Volumes and settings

| Volume               | Content                                                    | Mounted by  |
| -------------------- | ---------------------------------------------------------- | ----------- |
| `data`             | SQLite database, service liveness state, recordings and publications | app, worker, notes service |
| `auth`             | Access token and cookie signing key (restricted file mode) | app         |
| `provider`         | ChatGPT and OMP registration and credentials               | app, notes service |
| Host model directory | ASR and diarization models (read-only, not in the image)   | app, worker |

Restart and recreate keep the volumes. A new Compose project makes a new token, except the
one-time copy from the former `asr-en-vi` project, which keeps the token and provider sign-in.
After checking the new deployment, remove the old volumes with
`docker volume rm asr-en-vi_data asr-en-vi_auth asr-en-vi_provider asr-en-vi_model-install`.

| Setting                       | Purpose                                                                 |
| ----------------------------- | ----------------------------------------------------------------------- |
| `ASR_PORT`                  | Published port. The default link follows it.                            |
| `ASR_PUBLIC_URL`            | Exact loopback HTTP/HTTPS origin, or exact public HTTPS origin only; no credentials, path, query, fragment or wildcard. |
| `ASR_COOKIE_SECURE`         | Default `false` for HTTP. HTTPS `ASR_PUBLIC_URL` forces the access cookie to `Secure`. |
| `ASR_MAX_UPLOAD_BYTES`      | Upload limit. Default 4 GiB.                                            |
| `ASR_UPLOAD_CHUNK_BYTES`    | Maximum browser upload request size. Default 64 MiB.                   |
| `ASR_MAX_RECORDING_SECONDS` | Duration limit. Default 4 hours.                                        |

### Network access

The app, notes service and model installer use the default Docker network. The transcription
worker uses an internal-only network and has no provider mount. Nothing is published except the
app's port on the host's loopback address.

If Docker cannot allocate the worker network from its address pools, set
`ASR_WORKER_NETWORK_SUBNET` in `.env` to an unused CIDR after checking Docker networks and
host routes. An empty value keeps automatic allocation. The custom subnet keeps the
worker network internal.

| Service | Why it needs the network | Holds |
| --- | --- | --- |
| `app` | Sign-in and provider connection management. | Recordings, transcripts, sign-in data. |
| `worker` | ASR and diarization use local model paths; it has no provider egress. | Recordings, transcripts and task state. |
| `notes-service` | Calls the selected notes provider. | SQLite, recordings, transcripts, provider sign-in. |
| `model-installer` | Downloads catalog models at pinned revisions. | Only `/models` and its job files. |

`HF_HUB_OFFLINE=1` on `app`, `worker` and `notes-service` stops model-hub libraries. It does not
block the notes provider network, and neither does `--offline`, which only skips builds, pulls and
downloads and leaves the model installer stopped. To cut all egress, do it on the host, for example
with a firewall rule. Notes then fail with a clear provider error, and transcription keeps working.

The notes service shares the current `data` volume so it can read the SQLite/source/publication
layout and recording paths used by the existing application. This means the service boundary does
not prevent it from reading recordings at filesystem level; it is not an audio-isolation guarantee.
The notes adapter's provider request still uses transcript/source data rather than recording bytes.

### Access token

The link contains the token in the URL fragment (after `#`). The access page deletes the
fragment and sends the token in a JSON body to get a browser session.

- Show the current link and QR: `docker compose exec -T app python -m asr_studio.cli access show`
- Replace the token and revoke old links and sessions: `./scripts/rotate-token.sh`

Token rotation changes only the auth state. `docker compose down` keeps the volumes.
`docker compose down -v` deletes the data and auth volumes of the project.

## Transcription model

| Component   | Source                                                                                                   | Network or approval                                                                |
| ----------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Runtime     | Docker, `faster-whisper` 1.2.1, `pyannote.audio` 4.0.7, locked in `uv.lock`                        | The operator installs Docker. A GPU needs the NVIDIA driver and Container Toolkit. |
| ASR GPU     | [Qwen/Qwen3-ASR-1.7B-hf](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf), Apache-2.0, public, GPU only | `model prepare` downloads it as the GPU hardware default. No gated terms. |
| ASR CPU     | [Systran faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small), MIT, public          | `model prepare` can download it, on a GPU machine with `--asr-model whisper-small`. No gated terms. |
| ASR, optional | [Systran faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3), MIT, public | `model prepare --asr-model whisper-large-v3` downloads it. No gated terms. |
| ASR, optional | [vinai/PhoWhisper-large](https://huggingface.co/vinai/PhoWhisper-large), BSD-3-Clause, public | `model prepare --asr-model` downloads and converts it. No gated terms. |
| ASR, optional | [Qwen/Qwen3-ASR-1.7B-hf](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf), Apache-2.0, public, GPU only | `model prepare --asr-model` downloads it. No gated terms. |
| Diarization | [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1), CC-BY-4.0, gated | The operator accepts the conditions and downloads it. The app does not.            |
| UI          | HTML, CSS, JavaScript and fonts in the wheel                                                             | Served locally.                                                                    |

PyAV stays below version 19 for compatibility with faster-whisper 1.2.1. Install development
dependencies with `uv sync --all-groups --locked`. Install runtime only with
`uv sync --locked --no-dev`.

Models go to `./models`. To prepare them manually for offline setup:

```sh
mkdir -p ./models
cp -a /path/to/prepared-qwen3-asr-1.7b ./models/qwen3-asr-1.7b
cp -a /path/to/prepared-whisper-model ./models/whisper-large-v3
cp -a /path/to/prepared-whisper-small ./models/whisper-small
```

Qwen3-ASR needs valid `config.json`, `generation_config.json`, `model.safetensors`,
`tokenizer.json`, `tokenizer_config.json`, `processor_config.json` and `chat_template.jinja`.
Whisper models need valid `model.bin`, `config.json`, `tokenizer.json` and, where supplied,
`preprocessor_config.json`.

| Setting                                        | Purpose                                  |
| ---------------------------------------------- | ---------------------------------------- |
| `ASR_MODEL_HOST_PATH`                        | Model directory on the host.             |
| `ASR_GPU_MODEL_PATH`, `ASR_CPU_MODEL_PATH` | Model directories in the container.      |
| `ASR_PHOWHISPER_MODEL_PATH`, `ASR_QWEN_MODEL_PATH` | Optional model directories in the container. |
| `ASR_DEVICE`                                 | `auto` (default), `cpu` or `cuda`. |
| `ASR_MODEL_PATH`, `ASR_COMPUTE_TYPE`       | Explicit overrides for special tests.    |

### Check the ASR models

**Settings → ASR Model** lists Whisper large-v3, Whisper small, PhoWhisper-large and Qwen3-ASR-1.7B as
the app sees them now. Each row says **Ready**, **Not prepared**, **Installing**, **Needs an NVIDIA GPU**
or **Not checked** (no worker is running). Qwen3-ASR has no install button on a CPU-only machine because
it cannot run there; the others can.

**Default model** above the list decides what **Automatic** uses, and a **Default** badge marks that
model. **Automatic for this hardware** is Qwen3-ASR-1.7B on a GPU and Whisper small on a CPU-only
machine. You can pick any model that is ready here. The choice applies to new runs, survives a restart,
and is stored in `asr-default.json` in the data directory. Choosing a model for one run does not change
it. If the chosen model stops being usable, Settings says so and Automatic runs are refused rather than
silently using another model; install it again or choose another default.

For a model that is not prepared, the row shows how much free space it needs and how much is
left, and **Install model**. The model downloads on this machine and no recording is sent
anywhere. While it runs the row shows a progress bar with the bytes received, or the converting
step for PhoWhisper (a few minutes, with no percentage). **Cancel** stops it and removes the
partial download. When it finishes, the row says **Ready** and the model can be chosen for a
new run at once, with no restart. You can install while a task is transcribing. If it fails,
the row says why (no network, not enough space, a damaged download, a model folder that already
holds other files) and **Try again** starts it over. Restarting the services in the middle ends
the job as interrupted; start it again.

Install needs the `model-installer` service, which `./scripts/start.sh` starts. It is the only
service that downloads models and writes `./models`; it does not mount your recordings,
transcripts or sign-in data. Behind a proxy, set `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in
`.env`. It writes `./models` as the user that owns it, so set `ASR_MODEL_UID` and
`ASR_MODEL_GID` if you start Compose without `start.sh`. Only the four models above can be
installed; none needs a token. If the service is not running or cannot write `./models`, the row
shows the command to copy, as below. The dialog reads progress only while it is open.

### Choose a model for one run

Next to the start button, **Transcription model for this run** lists each model and whether it is
ready. The choice is sent with that run only. It does not change the system default, and each run
keeps its own transcript generation, so you can compare them with the generation selector. The
selector keeps your choice while the page stays open; a new page starts on **Automatic**.

| Choice           | Use it for                                 | Limits                                                                |
| ---------------- | ------------------------------------------ | --------------------------------------------------------------------- |
| Automatic        | The default model from Settings.           | Unless you chose one: `qwen3-asr-1.7b` on GPU, `whisper-small` on CPU. |
| Whisper large-v3 | Multilingual meetings with language detection. | Slowest of the large models and the most likely to repeat sentences. On Vietnamese it can return text that is not in the audio ([#168](https://github.com/nguyencanhtrung/asr-studio/issues/168)). |
| Whisper small    | A CPU-only machine.                        | Lower accuracy than PhoWhisper-large on Vietnamese and Qwen3-ASR-1.7B overall. |
| PhoWhisper-large | Meetings in Vietnamese.                    | Not for English or content that mixes in many technical terms. Segments are longer, so speaker labels are coarser. |
| Qwen3-ASR-1.7B   | Mixed Vietnamese and English, and speed.   | GPU only, 8336 MiB peak VRAM in the app with batch 4. It has no confidence score, so `asr_uncertain` does not fire for Qwen; **Listen again** catches repetition and speaker issues instead. |

A [short measurement](release/performance-v0.2.1.md) on public datasets backs these notes, with its limits.

Running the same recording twice may not give the same transcript. On a 10-minute excerpt run three
times, Qwen3-ASR-1.7B and Whisper large-v3 gave identical text and PhoWhisper-large gave three
different texts. Whisper small on a CPU gave identical text in three runs, but this was not measured
on a full meeting or a GPU. On a full meeting, Whisper large-v3 can still differ between runs: three runs of one
67-minute meeting gave 553, 601 and 611 segments. Before this release, Whisper large-v3 gave three different texts
on the same excerpt, and turning sampling off made it stable, so the likely source is the decoder's
sampling when it retries a segment. That attribution is an inference from the contrast.

An existing installation keeps large-v3 if its existing `.env` still has
`ASR_GPU_MODEL_PATH=/models/whisper-large-v3`; `.env.example` is not applied automatically. To move
to Qwen, change that line to `/models/qwen3-asr-1.7b` and prepare the model, or choose Qwen as the
default in Settings. The Qwen artifact is about 4.1 GB to stage when making that move.
See the [model comparison](model-candidates.md#đo-trên-bản-ghi-thật-11-2026) and [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168) for the measured decision.

These results come from a few real meetings without a reference transcript, so treat them as a
starting point and compare on your own recordings.

The two optional models are not downloaded at setup. An unprepared model cannot start a task. Install it
from Settings, or without the installer run this command in the application folder and reload the
page. It downloads model files only and sends no recording. The worker does not need a restart:

```sh
docker compose -f compose.yaml -f compose.model-prepare.yaml run --pull never --rm --no-deps \
  --user "$(id -u):$(id -g)" -e HF_HOME=/tmp/huggingface -e HF_HUB_OFFLINE=0 \
  -e TRANSFORMERS_OFFLINE=0 worker python -m asr_studio.cli model prepare --asr-model pho-whisper-large
```

The model-preparation override deliberately puts this one-shot command on the default network so
it can reach the model hub. The regular transcription worker remains on the internal-only network;
the override does not change the running worker's network or provider mounts.

Run it with the same `COMPOSE_PROJECT_NAME`, `ASR_IMAGE` and `ASR_MODEL_HOST_PATH` as the running
installation, or the model lands where the app does not look. Use `--asr-model qwen3-asr-1.7b`
for Qwen. For an offline machine, copy a prepared directory to
`./models/pho-whisper-large` or `./models/qwen3-asr-1.7b`.

The image contains CUDA 12.8 and cuDNN 9. At runtime, the worker opens models from local paths
only, in the model hub's offline mode, with telemetry off. Audio does not leave the machine. The collapsed
**Processing details** section of the Transcript block shows the backend, compute type and default
model. Each transcript generation shows the model, backend and compute type of the run that made it.

## Speaker diarization model

Accept the conditions of
[Community-1 on Hugging Face](https://huggingface.co/pyannote/speaker-diarization-community-1).
Then download the full pipeline on the host. When `git clone` asks for a password, type your
Hugging Face access token:

```sh
git lfs install
mkdir -p ./models
git clone https://hf.co/pyannote/speaker-diarization-community-1 \
  ./models/speaker-diarization-community-1
```

- If `ASR_MODEL_HOST_PATH` is not `./models`, clone to
  `$ASR_MODEL_HOST_PATH/speaker-diarization-community-1`.
- `ASR_DIARIZATION_MODEL_PATH` must stay below `/models` in the container.
- `scripts/start.sh` checks the required files. The worker rejects Git LFS pointer files and
  symlinks that leave the model directory.
- Use the Hugging Face token only for the download on the host. Do not put the token or the
  model ID in `.env` or Compose.

The worker uses a local path, the model hub's offline mode, `token=False` and telemetry off. ASR and diarization
run in two child processes, one after the other. Each process exits before the next phase starts,
which releases model memory.

### Speaker label limits

Expect more labels than people, and check labels at important moments. On three acceptance meetings
with about 6 people each, Qwen3-ASR-1.7B with Community-1 gave 7, 8 and 10 labels. Labels with fewer
than 16 segments held 2.8%, 2.2% and 5.1% of speech time. The original acceptance review found 6
wrong labels in 42 checked passages on Whisper large-v3. An automatic comparison on Qwen found 7 in
42, but it is a different score and does not replace that review.

The defaults stay, because no tested change was safe. One pass per meeting and setting:

| Change | Labels | Wrong labels of 42 | Clusters that may hold two people |
| --- | ---: | ---: | ---: |
| Default, threshold 0.60 | 7 / 8 / 10 | 7 | 0 / 0 / 2 |
| Threshold 0.50 | 6 / 7 / 8 | 9 | 0 / 1 / 1 |
| Threshold 0.70 | 5 / 6 / 10 | 8 | 1 / 0 / 2 |
| Threshold 0.80 | 6 / 6 / 9 | 7 | 1 / 0 / 1 |

- A threshold of 0.80 gives the fewest short labels but does not reduce wrong labels. The last
  column is a cautious warning from the annotations, not proof of a wrong merge.
- Merging every short label into another raised wrong labels to 9 of 42. A similarity limit of 0.70
  or more merged nothing.
- Splitting segments at speaker turns left wrong labels at 7 of 42 and raised segment counts about
  fourfold, from 855, 742 and 642 to 3488, 2982 and 2452.

The cause of the wrong labels is not known. The app has no setting for the number of speakers.

## Meetings and transcripts

### Create a meeting

Supported formats: `wav`, `mp3`, `m4a`, `mp4`, `mov`, `webm`, `ogg`, `opus`, `flac`. The backend
checks codec and duration with FFmpeg and stores an app-managed M4A/AAC copy. You do not need the
source file after the upload.

For `mp4`, `mov` and `webm` videos, the browser first cuts the audio track out of the file (a
stream copy, no re-encoding). It then uploads that audio in sequential requests. Each request is
at most 64 MiB by default, so a Cloudflare Tunnel request-body limit near 100 MB does not block a
long file. The browser reports cumulative progress, retries a failed part up to six times with
exponential delays of 1, 2, 4, 8, 16 and 32 seconds (a bounded 63-second window), and resumes
from the byte count that the server confirms. A temporary failure while reading that byte count is
retried with the same bounded window without deleting the upload. Audio at a higher bitrate still uses more
total bandwidth. The app duration limit remains 4 hours. The step shows as **Extracting audio**,
and **Cancel** works in it. If the browser cannot extract audio (an unsupported audio codec, no
audio track, or an old browser), the workspace uploads the original file in the same sequential
parts and says so below the status line. The meeting is the same either way. Only AAC audio in
`mp4`/`mov` and Opus audio in `webm` are copied.

Browser extraction keeps the extracted audio in memory before it starts the chunk upload. A local
one-hour measurement produced 67,177,975 upload bytes and increased Chromium RSS by 759,345,152
bytes. A linear four-hour estimate is about 268,711,900 upload bytes. A product limit or a
streaming extraction target is still needed before the app can promise four-hour video imports on
memory-constrained browsers.

In **New meeting**, **Create meeting** uploads the file and creates the meeting. The name comes
from the file name until you type one. The language hint is only metadata. The runner always
uses `transcribe` and detects the language per segment.

Known limitation: in Chrome on Ubuntu 20.04 (GTK 3.24), a double-click or Enter on a file in
the file picker closes the picker without passing the file to the page, so no file is selected.
This happens on any web page, not only in this workspace. Select the file and click **Open**,
or drag the file onto the upload area. Closing the picker without a file leaves **New meeting**
open.

**Transcribe after create** is on by default. The browser remembers that checkbox choice. When it
is on, the dialog also shows **Transcription model** after the model catalog loads: **Automatic**
is always available, while a model that is not ready is shown but cannot be selected. The model
choice is for this one meeting only and is not saved as a preference. The meeting page sends one
transcription request. Reload or Back does not send it again. If the machine is busy, the worker
is not ready, or the selected model was removed, the page shows the reason and a **Start
transcription** button with the preparation guidance. It does not retry.

### Transcription

- **Re-run recording** makes a new transcript generation. If the new run fails or is cancelled,
  the app keeps the old generation. The app publishes only complete generations.
- The progress bar shows the stage and the percentage (last timestamp ÷ recording duration, at
  most 100 %). It also shows elapsed time, segment count and last timestamp. There is no ETA.
- **Cancel transcription** stops the task.
- If diarization or alignment fails, the app keeps the current generation.

Community-1 gives regular turns (to detect overlap) and exclusive turns. The app assigns each ASR
segment to the speaker with the largest time overlap. It does not split or move ASR text or
timestamps.

The app marks a segment **Needs check** for multiple speakers, overlap, low speaker dominance or
no speaker match. It marks a segment **Listen again** when `avg_logprob < -1.0`,
`no_speech_prob >= 0.6` or `compression_ratio > 2.4`. These are Whisper decode thresholds, not
probabilities that the text is correct. Qwen3-ASR reports only `compression_ratio`.

### Meeting page

The page has two tabs: **Meeting notes** and **Transcript & recording**. The header shows the
meeting name, date, language hint and a status strip (**Recording · Transcript · Notes**).

- Without a hash, the page opens **Meeting notes** if notes exist or run. Otherwise it opens
  **Transcript & recording**.
- `#notes` and `#transcript` select a tab. A tab change adds a history entry, so Back returns to
  the previous tab.
- Without a complete transcript, **Meeting notes** shows only **Transcript needed first**.

On **Transcript & recording**, the player sticks to the top of the content area. During playback,
the app highlights the current segment and scrolls the list to it. Auto-scroll stops while audio
is paused, for 2 seconds after a manual scroll, while you edit, and while the list is not
visible. The **Meeting notes** tab does not pause audio.

### Edit a transcript

- **Edit** on a segment opens its editor: text, speaker select, **Save segment** and **Cancel**.
  Only one editor is open at a time.
- A speaker change saves immediately. **Save segment** or Ctrl/⌘+Enter saves the text. **Cancel**
  or Esc discards the typed text.
- If you open a different segment, the app keeps the unsaved text as a draft
  (**Unsaved draft**).
- **Details** shows the **Original assignment** and the model metrics.
- Old generations are read-only.
- While notes generation locks the source, **Edit** and speaker chips are disabled.

**Speakers in this version** shows one chip per speaker. A default name (**Speaker N**) has a
**name it** mark. Click a chip to rename it. Enter saves. Esc or **Cancel** closes.
**Restore default name** deletes the custom name. **Add speaker** is below the chips.

The app stores the original model speaker and timestamps separately. A speaker edit does not
lose them.

### Delete a meeting

**Delete meeting** requires the exact meeting name. Cancel running transcript or notes tasks
first. The app:

1. Marks the meeting as deleted in SQLite.
2. Deletes the audio, uploads and temporary files that the workspace manages. It does not change
   external source files.
3. Deletes transcripts, speakers, notes snapshots and notes revisions from the database.

If cleanup fails or the process stops, the meeting stays in a pending or failed state. You can
retry. The app also continues pending deletions at startup.

## Connect ChatGPT

The sidebar has two status buttons: **OpenAI** (ChatGPT) and **Google** (Antigravity).
Each shows **Connected**, **Not connected**, **Sign in again** or **Unavailable**, and opens the
**Settings** dialog at that provider. The **Settings** gear next to the GitHub link (on a phone,
the last button in the top bar) opens the same dialog at **AI provider**. It has four sections,
listed on the left and as a row on a phone:

- **AI provider:** sign in, model, effort, check and disconnect for each provider.
- **ASR Model:** see [Check the ASR models](#check-the-asr-models).
- **Theme:** the language and display mode (system, light or dark) for this browser. Changing the
  language reloads the page and returns to this section.
- **About:** what the software does, its version, and the developer's name and website.

Press **Esc** to close it; focus
returns to the button that opened it.

1. Open **OpenAI** and select **Continue with ChatGPT**.
2. Select a model from your account list and save it.
3. Optional: select `reasoning.effort` and save it with the model.
4. Select **Run check with synthetic data**. This sends one small request without meeting data.
   It can use ChatGPT quota. It does not retry.

| Model                        | Effort values                                          |
| ---------------------------- | ------------------------------------------------------ |
| GPT-5.6 Luna, GPT-6 Luna/Sol | `none` to `max` (GPT-5.6 Luna default: `medium`) |
| GPT-6.1 Sol, GPT-6 Astra     | `low` to `max`                                     |

**Model default** sends no effort parameter. The check uses the effort on screen, also when it is
not saved. A higher effort can take longer and use more quota. Each notes task fixes the saved
model and effort when the app accepts it.

The app stores the session in the `provider` volume. It does not read credentials from Codex or
other personal tools. A model that the account no longer offers must be selected again. The app
does not change models automatically. The model list does not prove quota or inference access.

### Callback

The callback is `http://127.0.0.1:<port>/auth/chatgpt/callback`. For the automatic callback, open the browser on the Docker
host. Do not change the callback to `localhost` or to a public redirect URL.

When the GUI is opened through the HTTPS reverse proxy from another machine:

1. Set `ASR_PUBLIC_URL` to the exact HTTPS GUI origin and keep the Compose port bound to
   `127.0.0.1`.
2. Set `ASR_CHATGPT_CALLBACK_URL` separately to the HTTP loopback callback URL that reaches the
   app, such as `http://127.0.0.1:8765/auth/chatgpt/callback`.
3. Select **Continue with ChatGPT**, complete authorization in the ChatGPT window, then paste the
   complete callback URL into **URL callback after ChatGPT sign-in** and select **Send callback URL**.
   The pasted URL is sent to this app only in a POST JSON body; ASR Studio parses it and never
   fetches it. The URL must be the configured HTTP `127.0.0.1` callback with its OAuth query.

The callback input is protected by the workspace session and the pending sign-in session. If it is
rejected, paste the complete URL again after correcting it. The normal callback browser tab still
works when it can reach the loopback address directly.

- Compose and the image disable the access log, so the log does not record the authorization
  code. If you run Uvicorn yourself, add `--no-access-log`. A proxy must drop or mask the
  callback query.
- Behind a TLS terminator, set `ASR_CHATGPT_CALLBACK_URL` to an HTTP `127.0.0.1` URL that routes
  to the app. Without it, only the ChatGPT sign-in fails, with a callback configuration error.

The registration name is stored by ChatGPT. New dynamic registration requests suggest **ASR
Studio**. A connected account keeps its existing registration during ordinary sign-in. To change
an older display name, select **Recreate ChatGPT registration**, confirm the impact, edit the
suggested name, and complete a new sign-in. The current credential is revoked only after your confirmation. If remote revocation fails,
ASR Studio keeps the current local connection and does not start a new registration. The old named
app entry may remain in ChatGPT; this action does not claim to delete that entry.

**Disconnect** deletes the stored ChatGPT token and tries to revoke the session at OpenAI. It
does not delete meetings. If remote revocation is not confirmed, check
[ChatGPT Settings → Usage](https://chatgpt.com/settings/usage). Token rotation does not disconnect
ChatGPT.

See [ChatGPT integration check](chatgpt-integration-check.md) for a real-account check. Mock
tests do not prove entitlement or quota.

## Connect Google Antigravity through OMP

1. Open **Google** and select **Connect Google Antigravity**.
2. Open the sign-in URL from OMP. If the provider asks, paste the code or redirect URL. The
   sign-in expires after 10 minutes. You can cancel it.
3. Select a model from the catalog and a **Thinking level**, then save.
4. Run the synthetic check. The catalog does not prove inference access or quota.

| Thinking level | Effect                                                            |
| -------------- | ----------------------------------------------------------------- |
| High (default) | More complete, slower (about 7–10 minutes for a 1-hour meeting). |
| Medium         | Faster, can miss action items.                                    |

Each notes task fixes the thinking level when the app accepts it.

The image pins OMP `18.5.0` with checksums for `amd64` and `arm64`. The runner uses its own home,
agent store, XDG directories and working directory in the `provider` volume. It does not read
personal Claude, Codex or OMP configuration. Each RPC process runs with `--no-session`,
`--no-tools`, `--no-extensions`, `--no-skills`, `--no-rules`, `--no-lsp`, `--no-pty`, `--no-ui`
and a neutral text-only `--system-prompt`. MCP, memory, auto-compaction, retry and model fallback
are off. The backend checks that RPC offers no tools before each request.

The synthetic check must return exactly `{"ok":true}`. The app treats the transcript as untrusted
data and validates schema and source IDs before it saves notes. The UI shows grouped errors
(revoke, quota, model access, network) without raw error text or credentials. Each call can run
15 minutes, inside the 60-minute task limit. A provider error finish reason (for example
`MALFORMED_FUNCTION_CALL`) shows as an unusable response.

**Disconnect Antigravity** disables the `google-antigravity` credential in the local store. OMP
does not confirm revocation at Google. To revoke remotely, check
[Google account permissions](https://myaccount.google.com/permissions). This does not affect
ChatGPT, the access token, transcripts or notes.

## Notes

### Create notes

Open a meeting with a complete transcript. On **Meeting notes**, the main button shows the saved
choice, for example **Create notes · English · ChatGPT GPT 6.1 Sol**. **Change…** shows the output
language, provider and model, and what data the app sends. Changes save immediately. The app does
not change provider or model automatically. If the provider is not connected, the button opens
the connection dialog.

Notes use the speaker names at creation time. If default names (**Speaker N**) remain, the page
shows a reminder with a **Name speakers first** link. The reminder does not block notes.

Notes contain an overview, main topics and arguments, decisions, open questions, confirmed action
items and items to confirm. A missing owner or due date shows as **Undetermined**. The app
converts a relative deadline only when the meeting date and wording are clear. It always keeps
the original wording. Schema validation does not replace your review.

### What the worker sends

The worker sends the transcript with segment IDs, timestamps, speaker names and the meeting date.
It does not send audio.

- Long sources split at segment boundaries into parts (24 maximum). Each part gets a summary with
  checked citations. The app then merges the summaries in transcript order.
- Each part starts with the last complete segments of the previous part as context (2,000 bytes
  maximum).
- A segment too large for one call splits into text pieces. Each piece keeps the segment ID and
  times. Adjacent pieces overlap by 300 characters at most.
- If merged summaries are too long, the app reduces the larger summary. The task fails only when
  it reaches the call limit or 8 reduce levels, or when a summary cannot get smaller.
- Before the first call, the backend checks that each segment is in at least one call. It stores
  a coverage manifest with the notes (not shown in UI or API).
- The app rejects a source above 1,000,000 bytes before it creates the task. It never truncates
  the transcript.
- Each call has 25,000 tokens for reasoning and output. Each task has a 60-minute limit.
- The app does not retry, or change model or effort, after a failure, a timeout or a cancel.

Intermediate summaries can lose details. Compare the final notes with the source.

### While notes run

The app takes an immutable source snapshot. SQLite rejects every `UPDATE` on the snapshot table
(schema v11). The app locks the transcript, speakers and meeting date of that meeting. Do not
change or disconnect the provider account while the task runs. You can read sources, edit old
notes and use other meetings.

The steps **Accept request**, **Start**, **Create notes** and **Save notes** show done, running or
waiting, with elapsed time and provider · model. **Cancel notes** sets the task to stopping. The
app releases the lock after the provider process exits. A failure, cancel or restart keeps the
transcript and saved notes. The app does not publish partial output. It can repair complete but
invalid output one time.

### Saved notes

The toolbar has a version select, **View | Write**, **Save draft**, **Copy saved version**,
**Download saved Markdown**, **Re-create notes · …** and **Change…**. Re-create always makes a new
version and keeps the old versions.

- **View** (default) renders the current draft. **Write** is a Markdown editor. The browser
  remembers the last mode.
- Save checks the version. Ctrl/⌘+Enter in the editor also saves. On a conflict, the app keeps
  your draft.
- With unsaved changes, the browser warns before close or reload.
- Copy and download use the saved content of the selected version. They contain no timestamp
  links.
- If edited Markdown has a source link that does not match the snapshot, the app saves it
  unchanged and returns `warnings`. **View** shows the link as text with a warning.

### Citations

Each notes revision stores citations in `notes_revisions.citations_json` (schema v10). For each
item, a citation stores the section title, the item text and the source segments (`id`,
`start_seconds`). The backend validates citations against the snapshot. `GET /api/notes/{id}`
returns `citations` (an empty list when there are none).

In **View**, each cited item shows one `mm:ss` chip per source segment (`h:mm:ss` after one
hour). The chip links to
`/meetings/{id}?notes_source={snapshot}&segment={segment}#transcript`.

- A chip shows only while the item keeps its original section and text.
- For duplicate items, the group keeps its chips only while the item count stays the same.
  Otherwise, the whole group loses its chips to prevent wrong links.
- Notes from before citations (NULL column) show no chips.

A chip opens **Transcript & recording**. The **Source saved at notes creation** card shows the
snapshot text, speaker, date and timestamp. The player seeks and plays (a browser can block
autoplay for a pasted link). The transcript marks the segment **Source of notes item**. If the
current transcript or meeting date differs from the snapshot, the card says where. **Seek
recording to this time** seeks only. A meeting rename alone does not make notes outdated.

## Development and checks

Install Python 3.12, uv, Node.js 24, Docker Compose and actionlint v1.7.11. Then run all quality
gates (see [development guide](agents/development.md)):

```sh
./scripts/check.sh
```

Browser test (fragment deletion, token only in the body, cookie hidden from JavaScript):

```sh
uv run playwright install chromium
ASR_RUN_BROWSER_TESTS=1 uv run pytest -m browser
```

End-to-end video test (skipped by default). It imports a video through the API, runs the worker
and reads the stored transcript:

```sh
ASR_TEST_VIDEO=/absolute/path/to/meeting.mp4 \
ASR_MODEL_PATH=/absolute/path/to/prepared-whisper-model \
ASR_EXPECTED_TEXT='expected phrase in the recording' \
uv run pytest tests/test_transcription_video_e2e.py -q
```

`ASR_EXPECTED_TEXT` is optional. It checks the stored transcript after case and whitespace
normalization.
