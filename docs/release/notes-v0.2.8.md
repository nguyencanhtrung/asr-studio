# v0.2.8 — ASR Studio

## Changes

- Linux Docker hosts can check for stable server releases and apply the matching Git tag and platform image digest. The workspace displays the host's cached update notice.
- The updater waits for active recordings, uploads, processing tasks and model installs. Same-schema startup failures restore the prior checkout and cached image. A manual schema-changing update requires a cold backup.
- Chrome profiles can opt into release notifications. The popup shows the ZIP and checksum when capture, upload and recovery permit it. A paired client warns about incompatible APIs and retains its audio until it can retry.

## Upgrade

- Bootstrap from v0.2.7 by checking out `v0.2.8`, pinning the published image, and running `./scripts/start.sh --pull`. Keep the existing Compose project, volumes and model path. See the [release install and host update instructions](../user-guide.md#host-updates).
- This release keeps storage schema 20. Preserve the prior image pin and checkout for rollback.
- For the unpacked extension, stop and wait for the durable receipt. Verify the ZIP checksum, replace the existing unpacked directory and reload the same Chrome profile. Follow the [extension update instructions](../../extensions/chrome/README.md#updates-and-verification-limits).

## Verification scope

Linux Chrome profile update and rollback were verified with synthetic audio. Native Windows and macOS profile acceptance is deferred because those environments are unavailable. The follow-ups remain open. [Windows #245](https://github.com/nguyencanhtrung/asr-studio/issues/245) · [macOS #246](https://github.com/nguyencanhtrung/asr-studio/issues/246)
