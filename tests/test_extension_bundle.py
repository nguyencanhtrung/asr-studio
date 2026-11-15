"""Contract tests for the deterministic production extension bundle."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

SCRIPT = Path(__file__).parents[1] / "scripts/bundle-extension.py"


def run_bundle(source: Path, output: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
            "--output",
            str(output),
            "--source-commit",
            "source-revision",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_bundle_is_reproducible_and_excludes_non_client_data(tmp_path: Path) -> None:
    source = tmp_path / "extension"
    (source / "icons").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "prototype").mkdir()
    (source / "userdata").mkdir()
    (source / "credentials").mkdir()
    (source / "manifest.json").write_text('{"manifest_version":3}\n', encoding="utf-8")
    (source / "icons/LICENSE.txt").write_text("license\n", encoding="utf-8")
    (source / "tests/should-not-ship.js").write_text("test\n", encoding="utf-8")
    (source / "prototype/old.js").write_text("old\n", encoding="utf-8")
    (source / "userdata/audio.webm").write_bytes(b"audio")
    (source / "credentials/token").write_text("secret\n", encoding="utf-8")
    (source / "credentials.json").write_text("secret\n", encoding="utf-8")
    (source / "userdata.json").write_text("audio\n", encoding="utf-8")

    output = tmp_path / "client.zip"
    first = run_bundle(source, output)
    first_bytes = output.read_bytes()
    second = run_bundle(source, output)

    assert output.read_bytes() == first_bytes
    assert first["sha256"] == second["sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert first["source_commit"] == "source-revision"
    with ZipFile(output) as archive:
        assert archive.namelist() == ["BUILD-METADATA.json", "icons/LICENSE.txt", "manifest.json"]
        metadata = json.loads(archive.read("BUILD-METADATA.json"))
    assert metadata["source_commit"] == "source-revision"
    assert metadata["files"] == ["icons/LICENSE.txt", "manifest.json"]


def test_bundle_metadata_has_sidecar_checksum(tmp_path: Path) -> None:
    source = tmp_path / "extension"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    output = tmp_path / "client.zip"
    result = run_bundle(source, output)

    checksum = Path(str(output) + ".sha256").read_text(encoding="utf-8")
    sidecar = json.loads(Path(str(output) + ".metadata.json").read_text(encoding="utf-8"))
    assert checksum == f"{result['sha256']}  {output.name}\n"
    assert sidecar["sha256"] == result["sha256"]
    assert sidecar["file_count"] == 1
