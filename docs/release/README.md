# Releases

Use this directory for the release process and evidence summaries.
Published versions and assets appear on [GitHub Releases](https://github.com/nguyencanhtrung/asr-studio/releases).
The former v0.1.0 draft does not describe the current release status.

## Prepare a release

1. Select checks with the [release process](process.md).
2. Record results with the [evidence template](evidence-template.md).
3. Write user changes with the [release notes template](notes-template.md).

A release does not require every historical benchmark.
Each image still needs evidence for its exact artifact.
Changes to a measured path can require a new benchmark.
The process defines these conditions.

## Read historical evidence

| Subject | English summary |
| --- | --- |
| Timing, memory, and AC-13 | [Performance](performance-v0.1.0.md) |
| Notes recovery and quality | [Notes](notes-v0.1.0.md) |
| Telemetry and logs | [Privacy](privacy-v0.1.0.md) |
| Media libraries and diagnostics | [Runtime](runtime-v0.1.0.md) |
| Former release draft | [Draft status](notes.md) |

The [archive](archive/README.md) preserves the original Vietnamese reports and their measurement conditions.
These reports do not certify a later image.
The [amd64 inventory](runtime-inventory-amd64.json) describes its recorded artifact only.

## Related documents

- [User guide](../user-guide.md): installation, access, providers, and diagnostics.
- [Third-party notices](../../third_party/README.md): licenses, source bundles, and image checks.
- [Development checks](../agents/development.md): local checks and CI commands.
- [Acceptance report](../acceptance-report.md): historical product acceptance results.
