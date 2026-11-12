from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_recovery", ROOT / "scripts/check-release-recovery.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
VERIFIED_JOBS = (
    "Quality and browser tests",
    "Build container (linux/amd64)",
    "Build container (linux/arm64)",
    "Publish linux/arm64 container",
)


def context():
    run = {
        "repository": {"full_name": "owner/repo"},
        "head_sha": "a" * 40,
        "head_branch": "v0.2.4",
        "event": "push",
        "path": ".github/workflows/pipeline.yml",
        "status": "completed",
        "conclusion": "failure",
    }
    jobs = [
        {"id": i, "name": name, "conclusion": "success"} for i, name in enumerate(VERIFIED_JOBS, 1)
    ]
    return run, jobs


def test_recovery_reuses_verified_tag_checks_and_arm64_image():
    run, jobs = context()
    jobs.append({"id": 9, "name": "Publish linux/amd64 container", "conclusion": "failure"})
    recovery.validate_context("v0.2.4", "a" * 40, "owner/repo", "v0.2.4", run, jobs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("head_branch", "main"),
        ("event", "pull_request"),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "other/repo"}),
        ("status", "in_progress"),
        ("conclusion", "success"),
    ],
)
def test_recovery_rejects_a_run_for_other_source_or_an_active_publisher(field, value):
    run, jobs = context()
    run[field] = value
    with pytest.raises(ValueError):
        recovery.validate_context("v0.2.4", "a" * 40, "owner/repo", "v0.2.4", run, jobs)


@pytest.mark.parametrize("name", VERIFIED_JOBS)
def test_recovery_requires_each_verification_job(name):
    run, jobs = context()
    jobs = [job for job in jobs if job["name"] != name]
    with pytest.raises(ValueError, match="Required job"):
        recovery.validate_context("v0.2.4", "a" * 40, "owner/repo", "v0.2.4", run, jobs)


def test_recovery_rejects_a_newer_failed_check_instead_of_reusing_stale_success():
    run, jobs = context()
    jobs.append({"id": 10, "name": VERIFIED_JOBS[0], "conclusion": "failure"})
    with pytest.raises(ValueError, match="Required job"):
        recovery.validate_context("v0.2.4", "a" * 40, "owner/repo", "v0.2.4", run, jobs)


@pytest.mark.parametrize("tag,latest", [("main", "main"), ("v0.2.4", "v0.2.5")])
def test_recovery_rejects_nonversioned_tags_and_alias_rollback(tag, latest):
    run, jobs = context()
    run["head_branch"] = tag
    with pytest.raises(ValueError):
        recovery.validate_context(tag, "a" * 40, "owner/repo", latest, run, jobs)


def test_recovery_rejects_a_malformed_source_revision():
    run, jobs = context()
    run["head_sha"] = "not-a-commit"
    with pytest.raises(ValueError, match="full commit SHA"):
        recovery.validate_context("v0.2.4", "not-a-commit", "owner/repo", "v0.2.4", run, jobs)


def test_recovery_keeps_passed_checks_when_an_unchanged_job_was_skipped():
    run, jobs = context()
    jobs.append({"id": 10, "name": VERIFIED_JOBS[0], "conclusion": "skipped"})
    recovery.validate_context("v0.2.4", "a" * 40, "owner/repo", "v0.2.4", run, jobs)


@pytest.fixture
def cli_context(monkeypatch, tmp_path):
    import sys

    run, jobs = context()
    report = {"total_count": len(jobs), "jobs": jobs}
    storage = tmp_path / "src/asr_studio/storage.py"
    storage.parent.mkdir(parents=True)
    storage.write_text("SCHEMA_VERSION = 16\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check-release-recovery.py",
            "--tag",
            "v0.2.4",
            "--run",
            "1",
            "--repository",
            "owner/repo",
            "--source",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(recovery.subprocess, "check_output", lambda *_a, **_kw: "a" * 40 + "\n")
    monkeypatch.setattr(recovery.subprocess, "run", lambda *_a, **_kw: None)

    def gh_json(path):
        if "jobs?" in path:
            return report
        if path.endswith("releases/latest"):
            return {"tag_name": "v0.2.4"}
        return run

    monkeypatch.setattr(recovery, "gh_json", gh_json)
    return report, storage


def test_recovery_cli_emits_verified_source_context(cli_context, capsys):
    import json

    recovery.main()
    assert json.loads(capsys.readouterr().out) == {
        "sha": "a" * 40,
        "schema": 16,
        "version": "0.2.4",
    }


def test_recovery_cli_rejects_a_truncated_job_report(cli_context, capsys):
    report, _ = cli_context
    report["total_count"] += 1
    with pytest.raises(SystemExit) as failure:
        recovery.main()
    assert failure.value.code == 2
    assert "bounded job report" in capsys.readouterr().err


def test_recovery_cli_requires_source_schema(cli_context, capsys):
    _, storage = cli_context
    storage.write_text("# Missing schema marker\n")
    with pytest.raises(SystemExit) as failure:
        recovery.main()
    assert failure.value.code == 2
    assert "Source schema version is missing" in capsys.readouterr().err


def test_recovery_cli_rejects_nonpositive_run_id(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check-release-recovery.py",
            "--tag",
            "v0.2.4",
            "--run",
            "0",
            "--repository",
            "owner/repo",
            "--source",
            ".",
        ],
    )
    monkeypatch.setattr(
        recovery.subprocess,
        "check_output",
        lambda *_a, **_kw: pytest.fail("Invalid run must stop before Git or API calls"),
    )
    with pytest.raises(SystemExit) as failure:
        recovery.main()
    assert failure.value.code == 2


def test_recovery_cli_requires_a_clean_source_tree(cli_context, monkeypatch):
    def git_diff(*_args, check, **_kwargs):
        if check:
            raise recovery.subprocess.CalledProcessError(1, "git diff")

    monkeypatch.setattr(recovery.subprocess, "run", git_diff)
    with pytest.raises(recovery.subprocess.CalledProcessError):
        recovery.main()
