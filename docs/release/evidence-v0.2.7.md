# v0.2.7 release evidence

This is the preparation record for [#241](https://github.com/nguyencanhtrung/asr-studio/issues/241). The [GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.7) will carry the final evidence for the immutable tag.

| Field | Value |
| --- | --- |
| Version and tag | `0.2.7 / v0.2.7` |
| Preparation base | `e731d27da8ee80ea99fd0479180dfd0b0a284774` |
| Previous release | `v0.2.6 / 913b040fb4da3621410a82db10b360434456e2aa` |
| Changed areas | Settings, interrupted capture, media handling, persistence and schema upgrade |
| Release decision | Pending required CI and delivery checks |

| Check | Delivery evidence |
| --- | --- |
| Quality and browser tests | Exact tag pipeline, including Settings, interrupted capture and persistence scenarios |
| Internal Chrome extension | Tag pipeline native Chrome tests, bundle metadata and SHA-256 |
| amd64 and arm64 images | Exact tag pipeline inventory, corresponding source, registry and configuration digests, and container smoke tests |
| Fresh source and public-image installation | Isolated smoke runs from the tag with separate evidence directories |
| Schema 19 to 20 | Storage migration tests and post-upgrade installation check on the primary deployment |

The Settings tests use synthetic provider fixtures. Real provider authentication and inference are outside the Settings change. No new quality, acoustic, or latency benchmark is planned.
