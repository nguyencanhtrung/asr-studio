#!/bin/sh

set -eu

# Fast gate for a writer's handoff and before a freeze: lint plus unit tests.
if [ "${1:-}" = "--fast" ]; then
    uv run --no-sync ruff check src tests scripts
    uv run --no-sync ruff format --check src tests scripts
    uv run --no-sync pytest -q
    exit 0
fi

./scripts/check-shell-syntax.sh
python3 scripts/check-environment.py --cache-only

uv sync --all-groups --locked
./scripts/check-workflows.sh
uv run --no-sync python scripts/check-environment.py
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pytest

find src/asr_studio/web -type f -name '*.js' -print | while IFS= read -r javascript_file; do
    node --check "$javascript_file"
done
# Node 24 treats a directory argument as a module path, so pass a glob.
node --test 'tests/web/*.test.mjs'

docker compose config --quiet
