#!/bin/sh
# usage: scripts/wait-ci.sh PR_NUMBER [MINUTES]
# Exit 0: every required check passed. 1: a check failed. 124: still pending after MINUTES (default 40).
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: scripts/wait-ci.sh PR_NUMBER [MINUTES]" >&2
    exit 2
fi
case "$1:${2:-40}" in
    :*|*:|0:*|*:0|*[!0-9:]*)
        echo "PR_NUMBER and MINUTES must be positive integers." >&2
        exit 2
        ;;
esac

deadline=$(($(date +%s) + ${2:-40} * 60))

# Right after a push GitHub has not registered the checks yet, and `gh pr checks`
# exits 1 with "no ... checks reported"; wait for them instead of reporting a failure.
while gh pr checks "$1" --required 2>&1 | grep -q 'no .*checks reported'; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "PR #$1 reported no checks within ${2:-40} min." >&2
        exit 124
    fi
    sleep 15
done

set +e
timeout "$((deadline - $(date +%s)))" gh pr checks "$1" --required --watch --fail-fast --interval 30 >/dev/null
status=$?
set -e
gh pr checks "$1" --required || true
if [ "$status" -eq 124 ]; then
    echo "PR #$1 checks still pending after ${2:-40} min." >&2
fi
exit "$status"
