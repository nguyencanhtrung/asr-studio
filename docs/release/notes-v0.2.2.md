# v0.2.2 — ASR Studio

## Changes

- Notes run in a separate `notes-service`. The transcription `worker` now uses an internal-only network and has no provider mount. [#181](https://github.com/nguyencanhtrung/asr-studio/pull/181)
- Ground notes in the transcript: fewer omitted points and fewer claims without a source, for ChatGPT and Gemini. [#79](https://github.com/nguyencanhtrung/asr-studio/issues/79) [#183](https://github.com/nguyencanhtrung/asr-studio/issues/183)
- Keep "by tomorrow" deadlines in notes, and accept transcripts of about two hours. [#180](https://github.com/nguyencanhtrung/asr-studio/issues/180)
- Sign in to ChatGPT from a remote browser in Settings, and refresh the registered app name. [#177](https://github.com/nguyencanhtrung/asr-studio/issues/177)
- Default to Qwen3-ASR-1.7B on a GPU, and flag repeated transcript segments. [#168](https://github.com/nguyencanhtrung/asr-studio/issues/168) [#80](https://github.com/nguyencanhtrung/asr-studio/issues/80)
- Clear a stale provider error after notes recover. [#95](https://github.com/nguyencanhtrung/asr-studio/issues/95)
- Recover pinned OMP downloads after a GitHub 504 during the image build. [#185](https://github.com/nguyencanhtrung/asr-studio/issues/185)

## Upgrade

- Check out tag `v0.2.2` and use its Compose files and scripts. Use `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.2` with `./scripts/start.sh --pull`. Do not delete volumes to upgrade.
- **Finish or cancel pending Notes tasks and back up the `data` volume first.** The database schema moves from 15 to 16. After the upgrade, v0.2.1 refuses to start with `Database schema is newer than this application`.
- A Notes task accepted with an old prompt can fail with `notes_contract_changed`. Use **Create notes** again from the saved transcript.
- `app`, `worker`, `notes-service` and `model-installer` must all use the same image. `./scripts/start.sh` handles this and starts the new service.
- If Docker cannot allocate the internal worker network, set `ASR_WORKER_NETWORK_SUBNET` in `.env`.

See the [user guide](../user-guide.md#install-a-release) and [Network access](../user-guide.md#network-access).

## Known limitations

- Gemini notes do not pass acceptance: 4 important claims without a source remain on the long sample. Use ChatGPT for notes that need full source support. See the [acceptance report](../acceptance-report.md).
- `app`, `notes-service` and `model-installer` can reach the internet. `--offline` does not block this.
- Speaker labels can still drift. See [#80](https://github.com/nguyencanhtrung/asr-studio/issues/80).

## Measurements

New measurements: notes acceptance for ChatGPT and Gemini Flash in the [acceptance report](../acceptance-report.md) ([#181](https://github.com/nguyencanhtrung/asr-studio/pull/181), [#184](https://github.com/nguyencanhtrung/asr-studio/pull/184)); diarization memory in [diarization-memory.md](diarization-memory.md).
Reused evidence: none.
Omitted measurements: the full ASR acceptance dataset and a new ASR benchmark. ASR timing applies only to [the v0.2.1 measurement](performance-v0.2.1.md).
