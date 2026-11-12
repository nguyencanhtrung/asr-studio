# Release process

## Select checks

Use the changes since the previous release to select additional checks.
Required CI and publication checks still apply.
The table defines additional checks for each affected area.
Apply every relevant row when a release changes several areas.

| Changed area | Additional checks | Expensive measurement |
| --- | --- | --- |
| Documentation only | Check links, commands, and wording. | No model benchmark. |
| UI or text | Run affected browser scenarios with synthetic fixtures. | No model benchmark unless processing changes. |
| Storage, worker, or task lifecycle | Run affected integration tests and persistence smoke. | Measure long tasks if resource use or scheduling changes. |
| ASR, decoding, diarization, or model revision | Run a fixed audio sample on the affected path. | Measure quality and timing. Measure long audio if memory or scaling changes. |
| Notes prompt, model, provider, or concurrency | Run fixed transcript samples with the affected configuration. | Measure notes quality, calls, and latency. |
| Image, dependency, or native runtime | Run platform, media, and installation checks. | Measure affected processing paths if numerical behavior or performance can change. |

For a performance claim, measure the path that supports the claim.
For an end-to-end latency claim, measure one continuous task through the stated endpoint.
A sum of separate stage timings does not establish end-to-end latency.

Run the full acceptance dataset for a major pipeline change or a claim about full product acceptance.
Other full runs can follow an agreed budget or a release candidate review.
This process does not require a fixed GPU schedule.

## Reuse benchmark evidence

Evidence reuse means that an earlier result remains relevant to a stated scope.
It does not mean that the new artifact passed a new measurement.

1. Link the original report and measured commit.
2. Record the measurement date and dataset identifier.
3. Record the model, prompt, provider, runtime, and hardware configuration.
4. Compare that configuration with the release candidate.
5. Explain why each relevant difference does not affect the reused result.
6. Mark the evidence as `REUSED`.

If a required configuration field is unknown, treat the report as historical context.
Run a new measurement when the comparison cannot establish relevance.
The age of a report alone does not establish relevance.
Remote providers can change behavior without a local code change.
Provider timings describe the observed date and configuration only.

Keep each historical `FAIL` or `PARTIAL` result in its original report.
Link a new result when a later measurement resolves the limitation.
Do not change the original result to describe a later revision.

## Check each artifact

Benchmark reuse does not cover artifact identity or corresponding source.
Every new platform image needs its own inventory and source bundle.

The [tag workflow](../../.github/workflows/pipeline.yml) performs these checks:

1. Check the tag against the package version.
2. Build each platform candidate.
3. Create the inventory and corresponding-source archive for that candidate.
4. Publish the source archive before the platform image.
5. Pull the published image with an empty Docker configuration.
6. Compare the published configuration digest with the candidate digest.
7. Check version, source revision, media decoding, and health.
8. Publish the combined manifest after both platforms pass.

Use the [bundle instructions](../../third_party/README.md) for manual collection.
Keep configuration digests separate from registry manifest digests.
A local image ID does not identify a registry manifest.

When source collection fails, read each blocker in the CLI output and retain
`source-manifest.json`. Transient HTTP and connection failures retry at most three
times. Retries preserve checksum validation and discard interrupted partial files.
The `Release source probe` workflow collects the same system
package sources from the `python-base` stage without building the application.
Select the source tag or commit when running it manually. Its report artifact contains
the inventory and source manifest; it does not publish an image.

Record fresh installation evidence for source and image delivery.
Use `scripts/offline-smoke.py --mode source` and `--mode pull` from the release tag.
Use separate evidence directories for the two runs.
These smoke tests use a synthetic provider.
They do not prove real provider authentication or inference.
Run a real provider check when authentication or the provider path changes.
Record `NOT_RUN` when a real check cannot run.

## Record the decision

1. Create a release record from the [evidence template](evidence-template.md).
2. Link successful CI runs for the candidate commit.
3. Record new, reused, and omitted measurements.
4. Explain each omitted check.
5. Record known limitations and unresolved acceptance criteria.
6. Write the [release notes](notes-template.md).

A failed required check blocks release.
An unresolved release requirement also blocks release.
An existing limitation needs an explicit scope statement and release decision.
`REUSED` and `NOT_RUN` do not satisfy a requirement for a new measurement.

Keep tokens, meeting content, personal paths, and private evidence outside the repository.
Publish only a redacted summary.
Explicit diagnostic commands can display private installation data.

## Maintain documents

Write canonical release documents in English with short, direct sentences.
Keep release notes limited to changes, upgrade actions, and known limitations.
Keep detailed measurements in separate reports with their original configuration.
Update operating instructions in the user guide when behavior changes.
Link those instructions from the release notes.
Do not copy the full architecture history into each release.
