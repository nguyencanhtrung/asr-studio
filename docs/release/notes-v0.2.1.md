# v0.2.1 — ASR Studio

## Changes

- Install ASR models from **Settings → ASR Model**, with real progress, cancel and retry. A separate `model-installer` service does the download. [#159](https://github.com/nguyencanhtrung/asr-studio/issues/159)
- Choose the default model for **Automatic** in Settings. A choice for one run still overrides it. [#163](https://github.com/nguyencanhtrung/asr-studio/issues/163)
- Choose the ASR model for each processing run: Whisper large-v3, Whisper small, PhoWhisper-large or Qwen3-ASR-1.7B. [#155](https://github.com/nguyencanhtrung/asr-studio/pull/155)
- Fix a fresh install on a CPU-only machine. Model preparation failed and the worker restarted forever, because Whisper small ships no `preprocessor_config.json`. [#159](https://github.com/nguyencanhtrung/asr-studio/issues/159)
- Organize Settings into AI provider, ASR Model, Theme and About, and make text larger across the interface. [#156](https://github.com/nguyencanhtrung/asr-studio/issues/156) [#158](https://github.com/nguyencanhtrung/asr-studio/issues/158)
- Group adjacent citations in notes. [#154](https://github.com/nguyencanhtrung/asr-studio/pull/154)
- Correct the documentation of what each service can reach on the network. [#165](https://github.com/nguyencanhtrung/asr-studio/issues/165)

## Upgrade

- Check out tag `v0.2.1` and use its Compose files and scripts. Use `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.1` with `./scripts/start.sh --pull`. Do not delete volumes to upgrade.
- **Back up the `data` volume first if you may need to return to v0.2.0.** The database schema moves from 14 to 15. After the upgrade, v0.2.0 refuses to start with `Database schema is newer than this application`.
- `app`, `worker` and the new `model-installer` must all use the same image. `./scripts/start.sh` handles this and starts the new service.
- Without `start.sh`, set `ASR_MODEL_UID` and `ASR_MODEL_GID` to the owner of the model directory.

See the [user guide](../user-guide.md#install-a-release).

## Known limitations

- `model-installer` runs all the time, with internet access and write access to `/models`. It has no published port and no access to recordings, transcripts or credentials. `--offline` leaves it stopped.
- All containers can reach the internet, including `worker`, which holds recordings. `--offline` does not block this. See [Network access](../user-guide.md#network-access).
- If the default model for this hardware is missing, the worker does not start and Settings offers no install for it. `./scripts/start.sh` prepares it first.
- Whisper small is visibly less accurate on Vietnamese. This release makes no new ASR quality, diarization accuracy or latency claim.
- Not tested: Safari, and an integrity failure on a real download (automated tests only).

## Measurements

New measurements: automated tests, browser scenarios and real installs and transcriptions recorded in the release evidence.
Reused benchmark evidence: none.
Omitted measurements: model quality and timing benchmarks, including for Whisper small, PhoWhisper-large and Qwen3-ASR-1.7B.
