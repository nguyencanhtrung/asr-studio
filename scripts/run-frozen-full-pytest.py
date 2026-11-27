"""Run the CI pytest lane against a clean, pinned worktree."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, timeout=30).strip()


def verify_checkout(repo: Path, commit: str) -> None:
    if git(repo, "rev-parse", "HEAD") != commit:
        raise ValueError("HEAD does not match the requested commit.")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("The candidate worktree must be clean.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="Full candidate commit SHA")
    parser.add_argument("output", type=Path, help="Fresh result directory outside the worktree")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.is_relative_to(repo):
        parser.error("Output must be outside the candidate worktree.")
    output.mkdir(mode=0o700)

    interrupted = None

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)

    env = {
        **os.environ,
        "PYTHONPATH": str(repo / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ASR_RUN_BROWSER_TESTS": "1",
        "ASR_REQUIRE_MEDIA_TOOLS": "1",
        "ASR_RUN_OMP_CONTRACT": "0",
        "ASR_RUN_DIARIZATION_SMOKE": "0",
        "ASR_RUN_DOCKER_UPDATE_REGRESSION": "0",
        "ASR_RUN_EXTENSION_TESTS": "0",
    }
    env.pop("PYTEST_ADDOPTS", None)

    def run(filename: str, deadline_seconds: int, *args: str) -> int:
        if interrupted is not None:
            raise InterruptedError(f"Run interrupted by signal {interrupted}.")
        with (
            (output / filename).open("w", encoding="utf-8") as log,
            subprocess.Popen(
                [sys.executable, *args],
                cwd=repo,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            ) as process,
        ):
            deadline = time.monotonic() + deadline_seconds
            while interrupted is None and time.monotonic() < deadline:
                try:
                    return process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if interrupted is not None:
                raise InterruptedError(f"Run interrupted by signal {interrupted}.")
            raise TimeoutError(f"{filename} exceeded {deadline_seconds} seconds.")

    result = {"commit": args.commit, "status": "blocked", "pytest_exit": None}
    try:
        verify_checkout(repo, args.commit)
        if run(
            "import.log",
            30,
            "-c",
            "import asr_studio, pathlib, sys; "
            "path = pathlib.Path(asr_studio.__file__).resolve(); print(path); "
            "sys.exit(not path.is_relative_to(pathlib.Path.cwd() / 'src' / 'asr_studio'))",
        ):
            raise ValueError("ASR Studio did not import from the candidate src directory.")
        if run("preflight.log", 60, "scripts/check-environment.py"):
            raise ValueError("Environment preflight failed. See preflight.log.")
        verify_checkout(repo, args.commit)

        result["status"] = "fail"
        result["pytest_exit"] = run(
            "pytest.log", 3600, "-m", "pytest", "-q", "-p", "no:cacheprovider"
        )
        verify_checkout(repo, args.commit)
        if interrupted is not None:
            raise InterruptedError(f"Run interrupted by signal {interrupted}.")
        result["after_state_verified"] = True
        if result["pytest_exit"] == 0:
            result["status"] = "pass"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)

    pending = output / "result.tmp"
    pending.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    pending.replace(output / "result.json")
    print(f"{result['status']}: {output / 'result.json'}")
    if interrupted is not None and result["status"] != "pass":
        return 128 + interrupted
    return int(result["status"] != "pass")


if __name__ == "__main__":
    sys.exit(main())
