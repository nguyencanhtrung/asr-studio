# v0.2.8 release evidence

This preparation record covers [#247](https://github.com/nguyencanhtrung/asr-studio/issues/247). The [GitHub Release](https://github.com/nguyencanhtrung/asr-studio/releases/tag/v0.2.8) records verification for the immutable tag.

| Field | Value |
| --- | --- |
| Version and tag | `0.2.8 / v0.2.8` |
| Preparation base | `159f210395ebbf22875bbfc5a63be2378a5c025c` |
| Previous tag | `v0.2.7` |
| Changed areas | Host update admission and rollback, release delivery, Chrome update notifications and compatibility |
| Storage schema | 20 in both releases |
| Release scope | Linux host and Linux Chrome verification. Native Windows and macOS follow-ups remain open. |

| Check | Evidence |
| --- | --- |
| Feature candidate | [PR #244](https://github.com/nguyencanhtrung/asr-studio/pull/244), [feature CI](https://github.com/nguyencanhtrung/asr-studio/actions/runs/45045194951) and [AC9 and AC10 evidence](https://github.com/nguyencanhtrung/asr-studio/issues/243#issuecomment-7082271473) |
| Exact tag | Tag pipeline for native amd64 and arm64 builds, corresponding source, public registry digests, smoke tests and the versioned Chrome bundle |
| Fresh installation | Source and public-image smoke runs from the tag with separate evidence directories |
| Primary upgrade | Retain the prior checkout and image pin. Verify the new app version, revision, schema, installation and service health after upgrade. |

The feature does not change inference models or Notes prompts. The automated recording and provider checks use synthetic data. No new performance benchmark is planned. Native Windows #245 and macOS #246 remain required for closing #243.
