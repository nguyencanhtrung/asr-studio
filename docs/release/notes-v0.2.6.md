# v0.2.6 — ASR Studio

Date: `2026-11-18`
Source commit: resolve the immutable tag with `git rev-list -n 1 v0.2.6`.
Final artifact evidence: [GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.6).
Evidence: [v0.2.6 release evidence](evidence-v0.2.6.md)

## Changes

- The internal Chrome client uploads audio while it records, so Stop sends only the final part and
  finalizes. An interrupted capture cancels its server session and keeps the local audio.
  See [#234](https://github.com/nguyencanhtrung/asr-studio/issues/234) and the
  [upload timing report](extension-upload-timing.md).
- The recorder now saves audio in 5-second slices. If the capture owner crashes, up to 5 seconds of
  the most recent audio can be lost.
- An installation can serve explicit additional GUI origins through `ASR_WEB_ORIGINS`, and
  `access show --origin` prints the access link for a configured origin.
  See [#232](https://github.com/nguyencanhtrung/asr-studio/issues/232).

## Upgrade

- Check out tag `v0.2.6` and follow the current [release installation instructions](../user-guide.md#install-a-release).
  For a public image install, set `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.6`; for a
  source install, use `ASR_IMAGE=asr-studio:v0.2.6`.
- The application schema stays at 19. No data migration is required from v0.2.5.
- `ASR_WEB_ORIGINS` is optional. Use comma-separated origins without spaces and ASCII or punycode
  hostnames. Hostnames that contain underscores now fail validation.
- Finish active captures and uploads, replace the files in the existing unpacked-extension
  directory with the v0.2.6 bundle, and click Reload in `chrome://extensions`.

## Known limitations

- The extension rejects pairing with a timed device lifetime: the server sends `device_expires_at`
  with six fractional digits and the client accepts at most three. Pair with the `never` lifetime.
- A canceled server session keeps its staged chunks until the operator deletes it.
- Live speaker echo remains unresolved in [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228).
  This release does not claim physical-microphone quality, multi-hour reliability, or full live
  speaker acceptance.
- Synthetic provider fixtures cover local state and failure handling only. Real provider
  authentication and inference are not claimed.

## Measurements

New measurements: the [upload timing report](extension-upload-timing.md). On a 600-second capture,
Stop to receipt fell from 45.68 s to 1.86 s. Each build ran once over loopback.
Reused evidence: none.
Omitted measurements: ASR, diarization, and Notes quality or latency benchmarks, because the release
does not change the decoder, models, or provider prompts. The
[release evidence](evidence-v0.2.6.md) lists the tag, platform, and installation checks.
