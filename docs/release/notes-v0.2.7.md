# v0.2.7 — ASR Studio

The [GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.7) records the source commit, artifacts, and verification.

## Changes

- If a capture loses its audio source, the Chrome extension offers **Save recorded part** or **Cancel server session**. Saved partial recordings keep the interruption reason and wait for you to select **Transcribe recorded part**. [#240](https://github.com/nguyencanhtrung/asr-studio/pull/240)
- **Settings → AI Provider** shows an overview and separate pages for OpenAI and Google Antigravity. **Automatic processing** moves under AI Provider. [#238](https://github.com/nguyencanhtrung/asr-studio/pull/238)

## Upgrade

- Preserve the Compose project and data volumes. Back up the database before upgrading from schema 19 to schema 20.
- Check out tag `v0.2.7`, set `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.7`, and follow the [installation instructions](../user-guide.md#install-a-release).
- Finish captures and uploads before upgrading. Upgrade the server first, then replace the files in the existing unpacked extension directory and click **Reload** in `chrome://extensions`.

## Known limitations

- An interrupted capture can hold staging quota until you save it, cancel the server session, or delete the staging data. Previously canceled captures keep their existing state.
- The extension estimates the recorded duration. Pair with the `never` device lifetime because timed pairing can fail when the server returns a timestamp with six fractional digits.
- Live speaker echo remains open. [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228)

## Measurements

Release checks cover interrupted capture, persistence, Settings, and fresh installations. The release makes no new claims about model quality, live provider authentication, physical microphone quality, or multi-hour capture.
