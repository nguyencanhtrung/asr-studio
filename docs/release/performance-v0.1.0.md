# Historical performance evidence

These measurements describe the recorded revisions and configurations.
They do not certify the v0.1.0 image or a later release.
The [original report](archive/v0.1.0/performance-v0.1.0.md) retains all tables, sources, and tradeoffs.

## Diarization: #71 and PR #98

The sample contains 7,366 seconds from two joined recordings.
It is not one continuous meeting.
The environment used an RTX 5080, Torch 2.11.0+cu128, pyannote.audio 4.0.7, and TorchCodec 0.11.1.

| Revision | Diarization | Transcript ready | Timing start |
| --- | --- | --- | --- |
| `151f3e4` | 7,222.6 s | 128.9 min | `POST /api/tasks` |
| `d515f36` | 103.0 s | 768 s | Import |
| `4a88ec8` | 96.73 s | 626.53 s | `POST /api/tasks` |

PR #98 decoded audio once to a 16 kHz mono waveform.
Different timing boundaries prevent direct comparison of every total.
The four-hour synthetic sample reached 4.93 GiB peak RSS and exceeded the 4 GiB criterion.
Issue #97 records the memory limitation.
Speaker-label agreement with the earlier output does not establish speaker accuracy.

## Continuous AC-13 run: #99 and PR #103

Revision `4a88ec8` used ChatGPT `gpt-5.6-sol`, medium effort, and English notes.
The same composite sample required 29 sequential provider calls.
Notes failed with `provider_output_too_large` after 1,463.47 seconds.
The task reached failure after 2,090.2 seconds from `POST /api/tasks`.

**Result: FAIL.** No successful endpoint with both transcript and notes exists for this run.
Issues #101 and #102 record the observed latency and output-limit problems.
These links identify historical findings, not current issue status.

## Notes-only runs: #104 and PR #109

Revision `b78711d` used prompt `notes-hierarchical-v7`.
Three ChatGPT `gpt-5.6-sol` medium runs completed in 116.5, 67.2, and 81.3 seconds.
Each run used one provider call.
Strict findings were 31/37, 28/38, and 48/75.
Partly supported findings were 4, 7, and 19.
A final Flash run took 241.7 seconds without a quality score.

The reported 733.2-second total combines separate stage measurements.
It does not establish continuous end-to-end acceptance.
Missing findings and misplaced decisions remain limitations in this evidence.

See the [acceptance report](../acceptance-report.md) for criterion definitions and results.
Use the [release process](process.md) before reusing any measurement.
