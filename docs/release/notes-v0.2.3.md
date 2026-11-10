# v0.2.3 — ASR Studio

## Changes

- The default Compose project is now `asr-studio`, so containers and volumes use the product name. On the first start, `./scripts/start.sh` stops a deployment under the former name `asr-en-vi` and copies its volumes to `asr-studio_*`. The old volumes stay for rollback. [#188](https://github.com/nguyencanhtrung/asr-studio/issues/188)

## Upgrade

- Check out tag `v0.2.3` and use its Compose files and scripts. Use `ASR_IMAGE=ghcr.io/nguyencanhtrung/asr-studio:v0.2.3` with `./scripts/start.sh --pull`. Do not delete volumes to upgrade.
- Upgrade with `./scripts/start.sh`. A plain `docker compose up` does not copy the volumes and starts on empty data.
- A custom `COMPOSE_PROJECT_NAME`, including `asr-en-vi`, is kept and not migrated.
- After you check the new deployment, you can remove the old volumes. See the [user guide](../user-guide.md#access-token).
- The database schema stays at 16. v0.2.2 can read the copied data.

## Known limitations

- The automatic copy is checked with contract tests and a manual copy of a real installation. It has not run end to end on a real v0.2.2 installation.
- The limitations of [v0.2.2](notes-v0.2.2.md) still apply.

## Measurements

New measurements: none.
Reused evidence: none. This release changes only startup and Compose naming, not processing.
Omitted measurements: ASR and notes benchmarks, because processing does not change.
