"""Exercise terminal certificates without running the full pytest lane."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "scripts/run-frozen-full-pytest.py"


@pytest.mark.parametrize(
    ("behavior", "status", "pytest_exit", "runner_exit", "verified"),
    [
        ("raise SystemExit(0)", "pass", 0, 0, True),
        ("raise SystemExit(1)", "fail", 1, 1, True),
        ("import time; time.sleep(30)", "fail", None, 143, False),
        ('open("dirty", "w").close()', "fail", 0, 1, False),
    ],
)
def test_frozen_runner_certificate(tmp_path, behavior, status, pytest_exit, runner_exit, verified):
    repo = tmp_path / "checkout"
    scripts = repo / "scripts"
    package = repo / "src/asr_studio"
    scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (scripts / "check-environment.py").touch()
    (repo / "src/pytest.py").write_text(f'print("pytest started", flush=True)\n{behavior}\n')
    runner = scripts / RUNNER.name
    shutil.copyfile(RUNNER, runner)
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=repo,
            env=git_env,
            text=True,
            timeout=5,
        ).strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "fixture")
    commit = git("rev-parse", "HEAD")
    output = tmp_path / "output"
    with subprocess.Popen([sys.executable, str(runner), commit, str(output)], env=git_env) as run:
        try:
            if pytest_exit is None:
                deadline = time.monotonic() + 10
                log = output / "pytest.log"
                while not log.exists() or "pytest started" not in log.read_text():
                    assert run.poll() is None and time.monotonic() < deadline
                    time.sleep(0.02)
                assert not (output / "result.json").exists()
                run.send_signal(signal.SIGTERM)
            assert run.wait(timeout=10) == runner_exit
        finally:
            if run.poll() is None:
                run.terminate()
                run.wait(timeout=10)

    result = json.loads((output / "result.json").read_text())
    assert result["commit"] == commit
    assert result["status"] == status
    assert result["pytest_exit"] == pytest_exit
    assert result.get("after_state_verified", False) is verified
