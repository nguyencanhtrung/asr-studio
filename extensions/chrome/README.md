# ASR Studio Capture

This directory is the maintained production client. It was promoted from the
pinned client source revision `2ede7185358b359eb93a657b498c84a00578df0e` with
`git archive`. The prototype server, operator UI, browser harness, and
prototype validation files are not part of this directory.

## Install for internal testing

Download the artifact named `asr-studio-extension-<commit>` from a successful
**Pipeline** run on `main` in the repository's GitHub **Actions** tab. It contains
`asr-studio-extension.zip`, its `.sha256`, and its `.metadata.json` sidecars.
GitHub may require sign-in; Actions artifacts expire according to the workflow's
retention setting. Check the metadata's `source_commit` against that run's commit.
On Linux, run `sha256sum -c asr-studio-extension.zip.sha256` before extracting.

Alternatively, build the same bundle from the repository root:

```sh
python3 scripts/bundle-extension.py --output /tmp/asr-studio-extension.zip
```

The command writes the zip, a checksum sidecar, and a metadata sidecar. The zip
also contains `BUILD-METADATA.json` with the source revision and source-tree
checksum. The archive contains only production extension files. It excludes
credentials, user data, tests, and prototype paths.

The manifest's optional host declaration supports Chrome's permission model;
pairing requests the exact selected API origin and base path at runtime.

Extract the zip on the Chrome client. In `chrome://extensions`, enable
Developer mode, choose **Load unpacked**, and select the extracted directory
that contains `manifest.json`. Keep the old prototype checkout and its browser
profile data separate. The production client uses a separate namespaced
`chrome.storage` credential set and IndexedDB database. It does not migrate or
adopt prototype buffers.

## Pair and record

1. Open the configured ASR Studio web root and choose **Settings → Devices**.
   The extension opens only this canonical root. It does not use a `/settings`
   route.
2. Create a pairing code in the web app. In the extension setup page, enter the
   exact API URL, grant the requested origin, allow microphone access, and pair.
   When recovering retained audio after expiry or revocation, use **Pair again**
   for the existing device in **Settings → Devices**. Do not create a new device
   code for that recovery.
3. Return to the meeting tab and start from the popup. The popup and control page
   can close while the offscreen capture and upload continue.
4. Audio chunks upload while recording. Stop from the popup or control page; the
   client waits for the final local persistence barrier, sends only the tail
   chunks, and cleans local chunks only after a matching durable raw receipt from
   the same backend. The recorder's 5-second timeslice bounds an interruption's
   unsaved tail to at most one slice.
If a create response is lost after the server commits, recovery replays the idempotent create and cancels the interrupted session while retaining local audio.

The selected API URL is the authority for capabilities, pairing, session,
chunk, finalize, and receipt requests. HTTP is limited to literal loopback.
Remote API use requires HTTPS. The capability `web_url` is stored separately
for explicit click-only navigation. The client never sends credentials or audio
to that web URL. Result links must match the pinned web root and the exact
`/meetings/<id>` shape. A bad result link cannot prevent receipt cleanup.

If Chrome server access is removed, audio stays local. **Retry upload** asks for
access to that recording's original server before resuming. If the server token
expires or is revoked, use **Pair again** for the existing device in **Settings →
Devices**; same-binding retained uploads resume without repointing the recording.
An interrupted recording remains in local history and does not block that repair.
If a wrong new-device code causes a 404, the audio remains local; pair the
existing device and retry. The client never silently discards or rebinds it to a
different endpoint.

The receipt ends extension work. The client does not poll transcript or meeting
notes processing. Those results remain in the ASR Studio web app.

## Updates and verification limits

Update the unpacked extension only after active capture and upload work ends.
No claim is made here about physical microphone quality or long-duration
reliability. Synthetic capture tests and focused Node checks do not replace
those checks. The native browser gate uses a disposable Chromium profile, a synthetic tab tone
and a fake microphone tone against the real application API over loopback HTTP
and a local HTTPS fixture. Its test-only certificate exception is never part of
the extension. See `tests/test_browser_extension_capture.py`; these checks do not
validate a deployed tunnel, physical devices, echo, or a multi-hour meeting.

The automatic-processing integration checks use synthetic inference output and
a synthetic notes-provider response. They exercise the real scheduler, task
worker, source snapshots, notes runner, rendering, and revision publication;
they do not measure transcription accuracy or provider quality. The client
receipt is checked before starting that worker, then the test opens the
completed transcript and notes in the existing meeting page.
