# Project instructions

## Code review

When reviewing code, apply `CODING_STANDARDS.md`.

## GitHub issue tracker

This repository uses GitHub Issues for specifications and implementation tickets.

Before a skill publishes or triages an issue, read both of these project files:

- `docs/agents/issue-tracker.md`
- `docs/agents/triage-labels.md`

Treat `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix` as canonical triage roles. Resolve each role through the project mapping before applying a GitHub label.

These tracker and triage conventions apply only to this repository and override broader defaults for the same workflow.

## Acceptance criteria before merge

Do not merge a pull request until every acceptance-criteria checkbox in each linked issue has been resolved. Resolve them with `./scripts/tick-criteria.py <ISSUE>` before you merge:

- Tick each criterion that the PR satisfies (`- [x]`) and verify it first.
- Leave each criterion that is not met unticked (`- [ ]`). On that line, append `— Not done: <reason>` with the specific blocker, and add the follow-up issue number when one exists.

An issue with any criterion that is neither ticked nor explained blocks the merge. The required check `Check linked issue criteria` (`.github/workflows/acceptance-criteria.yml`) enforces this. Editing a linked issue re-runs the check on each open PR that closes it. The PR body must link the issue with `Closes #N`. Report the unticked criteria to the user when you report the merge.

## Pull request cleanup

After a pull request has merged, run `./scripts/finish-pr.sh <PR_NUMBER>` to verify the merge and remote cleanup, fast-forward the local base, and safely delete the local source branch. The script stops if commits remain unmerged; resolve the reported blocker before reporting cleanup complete.

## Development checks

Before changing a workflow, the OMP adapter, or running tests in a restricted environment, read `docs/agents/development.md` for related contract tests, workflow linting and environment preflight.
