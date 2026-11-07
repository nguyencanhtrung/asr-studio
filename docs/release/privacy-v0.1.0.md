# Historical privacy evidence

The [original report](archive/v0.1.0/privacy-v0.1.0.md) retains methods, sources, and limitations.
These observations apply to the recorded revisions only.

## Telemetry: #74 and PR #91

The control at `151f3e4` opened Microsoft telemetry connections during diarization.
The change set `ORT_DISABLE_TELEMETRY=1` before library imports in the affected runner environments.

The comparison used a 14-minute clip with CUDA float16 and network access.
Monitoring sampled worker connections every 0.2 seconds and recorded SYN, DNS, and SNI metadata.
It did not inspect TLS payloads.

| Observation | Control: `151f3e4` | Changed: `b153a25` |
| --- | --- | --- |
| Diarization | 115.2 s | 109.0 s |
| Microsoft telemetry | Observed | Not observed |
| Other worker connections | Self-check connection | Self-check connection only |

One manual run does not prove the absence of every telemetry path.
Dependency changes can require a new egress check.
The original report records related work under #75, #92, and #93.
Those references do not state current issue status.

## Default logs: #17 and PR #112

The change restricted default logs to fixed event, stage, and error codes.
The worker drained child stderr without retaining its content.
Non-interactive startup did not call `access show`.
The historical report recorded 134 passing focused tests.
That count describes the original test run only.

Explicit diagnostics can display local data.
Keep tokens, recording content, and private paths outside public evidence.
See the [user guide](../user-guide.md) for operating commands.
Use the [release process](process.md) to select new privacy checks.

## Service boundary for issue #166

The transcription worker is now an internal-network service without the provider volume. The
notes service owns provider access and uses the default network. The model preparation override
retains default-network access for its one-shot model-hub download; this is a deployment contract,
not evidence that a runtime stack or provider call was exercised here. See
[`compose.yaml`](../../compose.yaml), [`compose.model-prepare.yaml`](../../compose.model-prepare.yaml)
and [ADR 0013](../adr/0013-notes-service-boundary.md).

The notes service still mounts the shared `data` volume. Current SQLite, recording and publication
paths therefore remain visible to that service at filesystem level; splitting provider egress does
not prove audio filesystem isolation. The notes adapter sends transcript/source material selected
for notes, not recording bytes, but this document does not claim that a mount boundary prevents
recording reads. Runtime network/provider verification remains an independent check.
