#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    printf 'Usage: %s BASE HEAD\n' "$0" >&2
    exit 2
fi

# Treat a rename into the generated directory as an addition.
evidence=$(git diff --name-only --no-renames --diff-filter=AMT "$1" "$2" -- docs/evidence/)
if [ -n "$evidence" ]; then
    printf 'Store generated evidence outside Git. Changed paths:\n%s\n' "$evidence" >&2
    exit 1
fi
