# v0.2.4 — ASR Studio

## Changes

- Fill the meeting date from recording metadata in MP4, MOV, M4A and WebM files when a usable date exists. The browser reads the original file locally. Metadata reading does not upload the video. Manual dates remain editable and are never overwritten. Missing or invalid dates leave the field empty. [#192](https://github.com/nguyencanhtrung/asr-studio/issues/192), [#193](https://github.com/nguyencanhtrung/asr-studio/pull/193)
- Align form controls and shorten Vietnamese and English labels and help text. [#191](https://github.com/nguyencanhtrung/asr-studio/pull/191)
- Keep ordinary punctuation readable in generated notes while escaping active Markdown syntax. [#191](https://github.com/nguyencanhtrung/asr-studio/pull/191)
- Clarify that the old `asr-en-vi` image is no longer published. Historical tags can still be built from source. [#187](https://github.com/nguyencanhtrung/asr-studio/pull/187)
- Include `notes-service` in the offline installation smoke's startup, restart and network checks. The smoke pauses the notes service while seeding its synthetic fixture.

## Upgrade

- Check out tag `v0.2.4` and use its Compose files and scripts. Set `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.4`, then run `./scripts/start.sh --pull`. Keep the same Compose project, model directory and volumes. See the [user guide](../user-guide.md#install-a-release).
- The database schema stays at 16. This release adds no data migration.

## Known limitations

- Some files do not contain a recording date. File modification time is not used as a substitute. Check the suggested date when the camera clock or timezone was incorrect.
- The limitations of [v0.2.3](notes-v0.2.3.md) still apply.
- Synthetic provider fixtures test local persistence and failure handling. They do not establish real provider authentication or inference.

## Measurements

New checks and exact-artifact installation results are recorded in the release evidence.
Reused benchmark evidence: none.
Omitted measurements: ASR quality, diarization accuracy and latency benchmarks, because decoding, models and inference do not change. Notes prompt and provider benchmarks are omitted because only Markdown rendering changes.
