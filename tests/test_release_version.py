from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

from asr_studio import __version__
from asr_studio.app import app
from asr_studio.routes import _asset_response

ROOT = Path(__file__).parents[1]


def test_application_metadata_lock_fastapi_and_shell_share_release_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    lock_version = next(
        package["version"] for package in lock["package"] if package["name"] == "asr-studio"
    )
    shell = _asset_response("shell.html", "text/html").body.decode("utf-8")

    assert Version(project["project"]["version"]) == Version(__version__)
    assert Version(lock_version) == Version(__version__)
    assert lock_version == "0.2.4"
    assert app.version == __version__
    assert f"Version {__version__}" in shell
    assert "{{ASR_STUDIO_VERSION}}" not in shell


def test_release_check_accepts_the_matching_release_tag():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-release.py"), "v0.2.4"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "source=0.2.4 lock=0.2.4 tag=v0.2.4" in result.stdout


def test_release_check_rejects_a_pep440_equivalent_noncanonical_tag():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-release.py"), "v0.2.4.0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must exactly match canonical tag 'v0.2.4'" in result.stderr


def test_release_check_rejects_a_tag_with_a_different_version():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-release.py"), "v0.3.0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must exactly match canonical tag 'v0.2.4'" in result.stderr
