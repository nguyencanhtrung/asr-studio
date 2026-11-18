# v0.2.6 release evidence

This is the preparation record for issue [#236](https://github.com/nguyencanhtrung/asr-studio/issues/236).
It is a candidate plan, not publication evidence.
The final exact-commit record is the `release-evidence.md` asset on the
[v0.2.6 GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.6).

## Candidate

| Field | Value |
| --- | --- |
| Version and tag | `0.2.6 / v0.2.6` |
| Preparation base | `9f69d4a48f0e6542fed5672fefa0e9261f178bee`; the final release commit is resolved from immutable tag `v0.2.6` |
| Previous release | `v0.2.5 / 0363324014cbd0b078f556548368e6546378839c` |
| Changed areas | Chrome client capture and upload, GUI origin configuration and authentication, CI limits, release metadata |
| Decision date | `2026-11-18 UTC` |
| CI | [PR #235 run 44282253519](https://github.com/nguyencanhtrung/asr-studio/actions/runs/44282253519) passed for the upload change only. Candidate tag CI: `NOT_RUN`. |
| Release decision | `BLOCKED pending tag CI, exact platform artifacts and installation checks` |

## Checks and measurements

| Check | Evidence | Original result | Report or run | Scope or omission reason |
| --- | --- | --- | --- | --- |
| Stop to receipt on a running instance | `NEW` | `PASS` | [Upload timing report](extension-upload-timing.md) | One 600-second run per build over loopback |
| Tag quality, native extension and platform publication | `NOT_RUN` | `UNKNOWN` | None | Runs on immutable `v0.2.6` |
| Fresh source and public-image installation | `NOT_RUN` | `UNKNOWN` | None | Run from the release tag with separate evidence directories |
| Real provider authentication and inference | `NOT_RUN` | `UNKNOWN` | None | Synthetic fixtures do not establish provider acceptance |
| Physical microphone, speaker echo and multi-hour capture | `NOT_RUN` | `UNKNOWN` | [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228) | No acoustic or long-session claim |
| ASR, diarization and Notes benchmarks | `NOT_RUN` | `UNKNOWN` | None | No decoder, model or prompt revision |

## Limitations

- The extension rejects pairing with a timed device lifetime. Pair with the `never` lifetime.
- The internal Chrome bundle is not a Chrome Web Store publication.
