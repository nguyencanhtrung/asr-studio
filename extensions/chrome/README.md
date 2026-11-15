# ASR Studio Capture

This directory is the maintained production client. It was promoted from the
pinned client source revision `2ede7185358b359eb93a657b498c84a00578df0e` with
`git archive`. The prototype server, operator UI, browser harness, and
prototype validation files are not part of this directory.

## Install for internal testing

Build a bundle from the repository root:

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
3. Return to the meeting tab and start from the popup. The popup and control page
   can close while the offscreen capture and upload continue.
4. Stop from the popup or control page. The client waits for the final local
   persistence barrier, uploads the retained chunks, and cleans local chunks
   only after a matching durable raw receipt from the same backend.

The selected API URL is the authority for capabilities, pairing, session,
chunk, finalize, and receipt requests. HTTP is limited to literal loopback.
Remote API use requires HTTPS. The capability `web_url` is stored separately
for explicit click-only navigation. The client never sends credentials or audio
to that web URL. Result links must match the pinned web root and the exact
`/meetings/<id>` shape. A bad result link cannot prevent receipt cleanup.

The receipt ends extension work. The client does not poll transcript or meeting
notes processing. Those results remain in the ASR Studio web app.

## Updates and verification limits

Update the unpacked extension only after active capture and upload work ends.
No claim is made here about physical microphone quality or long-duration
reliability. Synthetic capture tests and focused Node checks do not replace
those checks. Production browser E2E and the downloadable CI artifact remain
root integration work after the backend and workflow slices are complete.
