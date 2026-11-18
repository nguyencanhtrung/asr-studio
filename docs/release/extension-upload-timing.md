# Extension upload timing

This report records the server per-chunk cost and a live single-run comparison of
upload-after-Stop with upload-while-recording.

## Measurement method

The measurements were taken on 2026-11-18. Both live runs used a server container
built from revision `4697f5a0e9fd06c59cab7befd8412cb26847e6bb`, reached over
loopback HTTP at `http://127.0.0.1:8765`, and a Chromium client with a disposable
profile under Xvfb. The before client was the extension at that revision; the
after client was that revision plus the upload-while-recording change. The extension bundle was built with the repository's
`scripts/bundle-extension.py`. Capture used a real tab with looping tab audio and
a synthetic 440 Hz microphone tone. Each run requested 600 seconds; each live
figure below is one run, so there is no variance estimate.

Timing started when Stop was clicked and ended when the extension reported a
verified full-session receipt. Status was polled every 100 ms.

## In-process per-chunk server cost

The baseline used an in-process ASGI client with no network, a data directory on
NVMe ext4, and sequential PUT requests to the recording-session chunk endpoint.
The measured revision was `4697f5a0e9fd`.

| Chunk size | PUT count | Total | Per chunk | 57.6 MB projection |
| ---: | ---: | ---: | ---: | ---: |
| 16,000 bytes | 600 | 34.48 s | 57.5 ms | ~207 s |
| 160,000 bytes | 60 | 3.53 s | 58.8 ms | ~21 s |
| 960,000 bytes | 10 | 0.62 s | 61.8 ms | ~4 s |

The 57.6 MB values are projections from the measured per-byte rate, not
measurements. The requests were sequential; the fixed per-chunk server cost is
the measured cause for the long upload-after-Stop tail.

## Live Stop-to-receipt comparison

| Build | Recorded | Chunks | Bytes | Chunks on server before Stop | Stop to capture complete | Stop to receipt |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Before: 1,000 ms timeslice, upload after Stop | 601.1 s | 590 | 9,688,150 | 0 | 0.12 s | 45.68 s |
| After: 5,000 ms timeslice, upload while recording | 600.8 s | 120 | 9,675,381 | 119 | 0.11 s | 1.86 s |

Both runs ended with a server receipt and a ready recording. The server reported
durations of 601.169 s for the before run and 600.837 s for the after run.
