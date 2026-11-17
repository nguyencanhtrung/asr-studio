# v0.2.5 — ASR Studio

Date: `2026-11-17`
Source commit: resolve the immutable tag with `git rev-list -n 1 v0.2.5`.
Final artifact evidence: [GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.5).
Evidence: [v0.2.5 release evidence](evidence-v0.2.5.md)

## Changes

- Add an internal production Chrome capture client with stable extension identity, device pairing,
  bounded resumable recording upload, durable receipts, recovery, and local cleanup only after a
  matching server receipt. See [extension devices](../extension-devices.md) and the
  [internal client guide](../../extensions/chrome/README.md).
- Start automatic transcription and optional Notes processing after a recording reaches the ready
  handoff. Settings, source adoption, cancellation, Notes-only retry, and provider readiness remain
  explicit; the workflow does not silently choose a provider or rerun ASR. See
  [automatic processing](../extension-automatic-processing.md).
- Make device and processing recovery visible in the existing UI: pairing activates immediately,
  devices sort newest first, revoked history is hidden by default, and metadata confirmation keeps
  its feedback and manual controls.
- Add durable extension device, recording-session, processing workflow, attempt, and outbox state.
  The application schema moves from 16 to 19 through startup migrations.
- Preserve recoverable device and recording state across pairing loss, temporary upload responses,
  restart, deletion and media-finalization failure, with bounded staging, upload and recovery
  guards.

## Upgrade

- Check out tag `v0.2.5` and follow the current [release installation instructions](../user-guide.md#install-a-release).
  For a public image install, set `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.5`; for a
  source install, use `ASR_IMAGE=asr-studio:v0.2.5`.
- Back up the data volume and finish or cancel pending Notes tasks before upgrading. The startup
  migration advances the application schema from 16 to 19. Use a matching pre-upgrade backup for
  rollback; do not run an older binary against the upgraded database.
- For internal Chrome testing, install the v0.2.5 bundle from the release evidence or build it
  from the tagged source. Keep the existing production extension identity and pair the intended
  endpoint; the former prototype bundle is incompatible with this API.

## Known limitations

- Live speaker echo remains unresolved in [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228).
  This release does not claim physical-microphone quality, long-session reliability, or full live
  speaker acceptance.
- Synthetic provider fixtures cover local state and failure handling only. Real provider
  authentication and inference are not claimed.
- ASR quality, diarization accuracy, Notes quality, and latency benchmarks are omitted because the
  release changes extension capture, storage and task orchestration rather than the decoder,
  models or provider prompt. Exact artifact and installation checks remain in the release evidence.

## Measurements

New preparation checks: application version/tag validation, the project fast gate, and deterministic
credential-free extension bundle verification. Reused evidence: none for the candidate. Omitted
measurements and root-owned tag, platform, installation and provider checks are listed in the
[release evidence](evidence-v0.2.5.md).
