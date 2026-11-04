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

The app opens at `http://127.0.0.1:8765`. Compose publishes the port on loopback only. Open the
exact URL that the terminal prints. The app rejects other hosts (also `localhost`) to prevent DNS
rebinding.

### Start modes

| Mode                   | Behavior                                                                                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--source` (default) | Builds `ASR_IMAGE` from the checkout.                                                                                                                    |
| `--pull`             | Pulls `ASR_IMAGE`. The image must have a version tag (`:v0.1.3`) or a digest (`@sha256:...`). The script rejects `local`, `latest` and `edge`. |
| `--offline`          | Uses a local image and local models. It does not build, pull or download.                                                                                  |

App and worker always use the same `ASR_IMAGE`. Before it starts the services, the script:

1. Compares the app version and schema version in the image with the checkout.
2. Compares the image label `org.opencontainers.image.revision` with the checkout commit. A
   mismatch stops the script. An `unknown` revision gives only a warning. A checkout has a
   revision only when its directory is the root of a git repository.
3. Downloads a missing ASR model (`--source` and `--pull` only). Only the `model prepare` step
   can access the model hub.
4. Waits until both services are healthy.
5. Checks that the worker runs and that both models are readable through the read-only mount.

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
| NVIDIA GPU visible to Docker | CUDA   | `float16`  | `whisper-large-v3`               |
| No GPU                       | CPU    | `int8`     | `whisper-small` (lower accuracy) |

If the host has a GPU but Docker cannot access it, setup stops with an NVIDIA Container Toolkit
error. It does not fall back to the CPU.

### Install a release

The commands below install the ASR Studio release `v0.2.0`. For a real installation, use the tag,
commit or digest from the release notes. Do not overwrite old tags.

`v0.1.3` predates the rename: its published image remains under `asr-en-vi` and
its Python module is `asr_en_vi`. Use the Compose file and CLI instructions from
that tag. The current checkout uses `asr_studio` and must be rebuilt with
`./scripts/start.sh --source` and `ASR_IMAGE=asr-studio:local`.

Keep `COMPOSE_PROJECT_NAME` and `ASR_MODEL_HOST_PATH` the same on each run, so that Compose
uses the same volumes and model directory. Prepare the ASR model while online. Download
Community-1 only after you accept its conditions (see
[Speaker diarization model](#speaker-diarization-model)).

Build from source:

```sh
git clone --branch v0.2.0 --depth 1 https://github.com/nguyencanhtrung/asr-studio.git
cd asr-studio
export COMPOSE_PROJECT_NAME=asr-source-v1
export ASR_MODEL_HOST_PATH="$PWD/models"
export ASR_IMAGE=asr-studio:v0.2.0
./scripts/start.sh --source
```

Pull the public image (same checkout):

```sh
export COMPOSE_PROJECT_NAME=asr-pull-v1
export ASR_PORT=8766
export ASR_MODEL_HOST_PATH="$PWD/models"
export ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.0
./scripts/start.sh --pull
```

A different project name gives separate data, token and provider volumes. Port 8766 prevents a
conflict with the source installation. The local tag `asr-studio:v0.2.0` shows that the image
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

### Troubleshooting

| Message            | Check                                                               |
| ------------------ | ------------------------------------------------------------------- |
| Worker not ready   | The `worker` service and the shared `data` volume.              |
| Model not readable | `ASR_MODEL_HOST_PATH` and the `/models` mount in both services. |

### Volumes and settings

| Volume               | Content                                                    | Mounted by  |
| -------------------- | ---------------------------------------------------------- | ----------- |
| `data`             | SQLite database and worker liveness state                  | app, worker |
| `auth`             | Access token and cookie signing key (restricted file mode) | app         |
| `provider`         | ChatGPT and OMP registration and credentials               | app, worker |
| Host model directory | ASR and diarization models (read-only, not in the image)   | app, worker |

Restart and recreate keep the volumes. A new Compose project makes a new token.

| Setting                       | Purpose                                                                 |
| ----------------------------- | ----------------------------------------------------------------------- |
| `ASR_PORT`                  | Published port. The default link follows it.                            |
| `ASR_PUBLIC_URL`            | A specific loopback URL.                                                |
| `ASR_COOKIE_SECURE`         | Default `false` for HTTP. Set `true` behind a local TLS terminator. |
| `ASR_MAX_UPLOAD_BYTES`      | Upload limit. Default 4 GiB.                                            |
| `ASR_UPLOAD_CHUNK_BYTES`    | Maximum browser upload request size. Default 64 MiB.                   |
| `ASR_MAX_RECORDING_SECONDS` | Duration limit. Default 4 hours.                                        |

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
| ASR GPU     | [Systran faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3), MIT, public    | `model prepare` can download it. No gated terms.                                 |
| ASR CPU     | [Systran faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small), MIT, public          | `model prepare` can download it. No gated terms.                                 |
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
cp -a /path/to/prepared-whisper-model ./models/whisper-large-v3
cp -a /path/to/prepared-whisper-small ./models/whisper-small
```

Each model needs valid `model.bin`, `config.json`, `tokenizer.json` and
`preprocessor_config.json`.

| Setting                                        | Purpose                                  |
| ---------------------------------------------- | ---------------------------------------- |
| `ASR_MODEL_HOST_PATH`                        | Model directory on the host.             |
| `ASR_GPU_MODEL_PATH`, `ASR_CPU_MODEL_PATH` | Model directories in the container.      |
| `ASR_PHOWHISPER_MODEL_PATH`, `ASR_QWEN_MODEL_PATH` | Optional model directories in the container. |
| `ASR_DEVICE`                                 | `auto` (default), `cpu` or `cuda`. |
| `ASR_MODEL_PATH`, `ASR_COMPUTE_TYPE`       | Explicit overrides for special tests.    |

### Check the ASR models

**Settings → ASR Model** lists Whisper large-v3, PhoWhisper-large and Qwen3-ASR-1.7B as the app
sees them now. A badge marks the model the system default (**Automatic**) uses on this machine. When that model
is outside this list, as on a CPU-only machine, a line above the list names it instead. Each row says **Ready**, **Not prepared**, **Needs an NVIDIA GPU** or **Not checked** (no
worker is running). For a model that is not prepared, the row shows the command that prepares it
and a **Copy** button. Run it in the app folder; it only downloads the model and reads no
recording. The dialog reads the list each time it opens, so reopen it after the command finishes.
Qwen3-ASR says so when the worker runs on CPU. Settings never starts a download.

### Choose a model for one run

Next to the start button, **Transcription model for this run** lists each model and whether it is
ready. The choice is sent with that run only. It does not change the system default, and each run
keeps its own transcript generation, so you can compare them with the generation selector. The
selector keeps your choice while the page stays open; a new page starts on **Automatic**.

| Choice           | Use it for                                 | Limits                                                                |
| ---------------- | ------------------------------------------ | --------------------------------------------------------------------- |
| Automatic        | The default chosen for this hardware.      | `whisper-large-v3` on GPU, `whisper-small` on CPU.                    |
| Whisper large-v3 | Multilingual meetings with language detection. | Slowest of the three, and the most likely to repeat sentences. |
| PhoWhisper-large | Meetings in Vietnamese.                    | Not for English or content that mixes in many technical terms. Segments are longer, so speaker labels are coarser. |
| Qwen3-ASR-1.7B   | Mixed Vietnamese and English, and speed.   | GPU only, about 8.5 GB of VRAM on a 75-minute meeting. No confidence score: **Listen again** only catches repetition and speaker issues, so its count does not compare with other models. |

These results come from a few real meetings without a reference transcript, so treat them as a
starting point and compare on your own recordings.

The two optional models are not downloaded at setup. An unprepared model cannot start a task, and
the page shows this command for it. Run it in the application folder, then reload the page. It
downloads model files only and sends no recording. The worker does not need a restart:

```sh
docker compose -f compose.yaml -f compose.model-prepare.yaml run --pull never --rm --no-deps \
  --user "$(id -u):$(id -g)" -e HF_HOME=/tmp/huggingface -e HF_HUB_OFFLINE=0 \
  -e TRANSFORMERS_OFFLINE=0 worker python -m asr_studio.cli model prepare --asr-model pho-whisper-large
```

Run it with the same `COMPOSE_PROJECT_NAME`, `ASR_IMAGE` and `ASR_MODEL_HOST_PATH` as the running
installation, or the model lands where the app does not look. Use `--asr-model qwen3-asr-1.7b`
for Qwen. For an offline machine, copy a prepared directory to
`./models/pho-whisper-large` or `./models/qwen3-asr-1.7b`.

The image contains CUDA 12.8 and cuDNN 9. At runtime, the worker opens models from local paths
only, in offline mode, with telemetry off. Audio does not leave the machine. The collapsed
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

The worker uses a local path, offline mode, `token=False` and telemetry off. ASR and diarization
run in two child processes, one after the other. Each process exits before the next phase starts,
which releases model memory.

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

The callback is `http://127.0.0.1:<port>/auth/chatgpt/callback`. Open the browser on the Docker
host. Do not change the callback to `localhost`.

- Compose and the image disable the access log, so the log does not record the authorization
  code. If you run Uvicorn yourself, add `--no-access-log`. A proxy must drop or mask the
  callback query.
- Behind a TLS terminator, set `ASR_CHATGPT_CALLBACK_URL` to an HTTP `127.0.0.1` URL that routes
  to the app. Without it, only the ChatGPT sign-in fails, with a callback configuration error.

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
