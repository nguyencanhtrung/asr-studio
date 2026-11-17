# v0.2.5 release evidence

This is the preparation record for issue [#230](https://github.com/nguyencanhtrung/asr-studio/issues/230).
It is a candidate plan, not publication or deployment evidence.
The final exact-commit record is the `release-evidence.md` asset on the
[v0.2.5 GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.5).

## Candidate

| Field | Value |
| --- | --- |
| Version and tag | `0.2.5 / v0.2.5` |
| Preparation base | `2fcdd177585e96306d37094bfe312d33bc187734`; final release commit is resolved from immutable tag `v0.2.5` |
| Previous release | `v0.2.4 / 2e46bbdd732f34c5750997b7d86c59eeb551eff0` |
| Changed areas | Extension device/auth/recording ingest, automatic processing and task lifecycle, schema 16→19, UI/text, release metadata and installation guidance |
| Decision date | `2026-11-17 UTC` |
| CI | Candidate tag CI: `NOT_RUN`. [Main CI run 43877615288](https://github.com/nguyencanhtrung/asr-studio/actions/runs/43877615288) passed at the preparation base only; it does not cover this candidate. |
| Release decision | `BLOCKED pending root-owned tag CI, exact platform artifacts, installation checks and independent review` |

## Artifacts

| Artifact | Registry/configuration digest | Inventory/source archive | Checksum or publication checks |
| --- | --- | --- | --- |
| Internal Chrome client candidate | Not applicable; not published | `asr-studio-extension-v0.2.5.zip`, source tree SHA-256 `e936a27fae8fd5f87f6dba137ab3fe739775b1f5ea0ba974116f131ee6b168f0` | Two local preparation builds matched; 39 production files; manifest `0.2.5`; stable key unchanged. Final ZIP is rebuilt from the release tag; use its published checksum and build metadata |
| linux/amd64 application image | `NOT_RUN` | `NOT_RUN` | Root-owned tag pipeline must build, inventory, collect corresponding source, compare configuration digest, check version/schema/media/health, then publish |
| linux/arm64 application image | `NOT_RUN` | `NOT_RUN` | Root-owned tag pipeline must build, inventory, collect corresponding source, compare configuration digest, check version/schema/media/health, then publish |

Combined platform manifest digest: `NOT_RUN`.

## Checks and measurements

| Check | Evidence | Original result | Report or run | Scope or omission reason |
| --- | --- | --- | --- | --- |
| Application pyproject/source/lock/shell metadata and canonical tag | `NEW` | `PASS` | Preparation check logs; final evidence is linked above | `scripts/check-release.py v0.2.5` reports source, lock and tag `0.2.5`; shell renders the same source version |
| Chrome manifest version and stable key | `NEW` | `PASS` | Preparation check logs; final evidence is linked above | Candidate manifest is `0.2.5`; the public key is unchanged |
| Credential-free reproducible Chrome bundle | `NEW` | `PASS` | Preparation check logs; final evidence is linked above | Two deterministic builds match; the bundle excludes tests, prototype, credentials and user-data paths |
| Release-version, bundle and release contract tests | `NEW` | `PASS` | Preparation check logs; final evidence is linked above | Focused checks: `62 passed`; the project fast gate: `2381 passed, 602 skipped` |
| Main CI | `NOT_RUN` for candidate | `PASS` at base only | [Run 43877615288](https://github.com/nguyencanhtrung/asr-studio/actions/runs/43877615288) | Base-commit context only; not reused as candidate evidence |
| Tag quality/native/publication checks | `NOT_RUN` | `UNKNOWN` | None | Run on immutable `v0.2.5`; verify both platforms and exact artifacts before publication |
| Fresh source installation | `NOT_RUN` | `UNKNOWN` | None | Root-owned `scripts/offline-smoke.py --mode source` from the release tag with a separate evidence directory |
| Fresh public image installation | `NOT_RUN` | `UNKNOWN` | None | Root-owned `scripts/offline-smoke.py --mode pull` with empty Docker configuration and a separate evidence directory |
| UI, extension capture and automatic-processing browser scenarios | `NOT_RUN` | `UNKNOWN` | None | Run the affected synthetic browser scenarios in tag CI; the local Node ESM check could not start because the host has Node 18 and the repository requires Node 24; browser scenarios do not establish physical microphone, echo or long-session behavior |
| Storage/task lifecycle integration and persistence smoke | `NOT_RUN` for the candidate release record | `UNKNOWN` | None | Run focused and CI integration checks for schema 19 and restart/recovery behavior |
| Real provider authentication and inference | `NOT_RUN` | `UNKNOWN` | None | Synthetic fixtures do not establish account/provider acceptance |
| Physical microphone, live speaker echo and long-session acceptance | `NOT_RUN` | `UNKNOWN` | [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228) | Explicitly unresolved; no claim is made |
| ASR/diarization quality, latency and Notes provider benchmarks | `NOT_RUN` | `UNKNOWN` | None | No decoder/model/prompt revision; no new benchmark is required by the changed paths, and no benchmark claim is made |

## Limitations and blockers

- [#228](https://github.com/nguyencanhtrung/asr-studio/issues/228) remains unresolved for live speaker echo. Physical-microphone, long-session and provider acceptance are outside this preparation result.
- The candidate has no immutable tag, published application image, source archive, combined manifest, fresh source installation, fresh image installation or real-provider result yet.
- The base main CI pass is recorded for context only and must not be presented as a pass for the v0.2.5 candidate.
- The local Node ESM suite was not executed because Node 24 is unavailable in this worker environment; tag CI remains required for that check.
- The internal Chrome bundle is not a Chrome Web Store publication. Root owns release upload, deployment, and local build cleanup after verified rollout.

Synthetic provider checks: `NOT_RUN for this release record; existing tests use synthetic fixtures only`.
Real provider authentication and inference: `NOT_RUN`.
Fresh source installation: `NOT_RUN`.
Fresh image installation: `NOT_RUN`.
