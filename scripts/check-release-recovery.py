#!/usr/bin/env python3
"""Require verified frozen source before recovering an interrupted amd64 publication."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REQUIRED_JOBS = (
    "Quality and browser tests",
    "Build container (linux/amd64)",
    "Build container (linux/arm64)",
    "Publish linux/arm64 container",
)


def validate_context(tag, revision, repository, latest_tag, run, jobs):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Recovery requires a stable canonical version tag.")
    if latest_tag != tag:
        raise ValueError("Recovery must target the latest GitHub Release.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("The source revision must be a full commit SHA.")
    if (
        run.get("repository", {}).get("full_name") != repository
        or run.get("head_sha") != revision
        or run.get("head_branch") != tag
        or run.get("event") != "push"
        or run.get("path") != ".github/workflows/pipeline.yml"
    ):
        raise ValueError("The verified run must identify this repository, tag and source commit.")
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        raise ValueError("Recovery requires a completed failed tag pipeline.")
    for name in REQUIRED_JOBS:
        executed = [
            job for job in jobs if job["name"] == name and job.get("conclusion") != "skipped"
        ]
        if not executed or max(executed, key=lambda job: job["id"]).get("conclusion") != "success":
            raise ValueError(f"Required job must have passed on the frozen source: {name}")


def gh_json(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--run", required=True, type=int)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    if args.run <= 0:
        parser.error("Run ID must be positive.")
    revision = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(args.source), "diff", "--exit-code"], check=True)
    run = gh_json(f"repos/{args.repository}/actions/runs/{args.run}")
    report = gh_json(
        f"repos/{args.repository}/actions/runs/{args.run}/jobs?filter=all&per_page=100"
    )
    if report["total_count"] != len(report["jobs"]):
        parser.error("The run exceeds the bounded job report; inspect it before recovery.")
    latest = gh_json(f"repos/{args.repository}/releases/latest")
    try:
        validate_context(
            args.tag, revision, args.repository, latest["tag_name"], run, report["jobs"]
        )
    except ValueError as error:
        parser.error(str(error))
    schema = re.search(
        r"^SCHEMA_VERSION = ([0-9]+)$",
        (args.source / "src/asr_studio/storage.py").read_text(),
        flags=re.MULTILINE,
    )
    if schema is None:
        parser.error("Source schema version is missing.")
    print(json.dumps({"sha": revision, "schema": int(schema[1]), "version": args.tag[1:]}))


if __name__ == "__main__":
    main()
