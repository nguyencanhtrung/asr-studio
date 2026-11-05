import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from asr_studio import cli

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "release_bundle", ROOT / "scripts/prepare-release-bundle.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_bundle_gate_uses_actual_version_and_schema_contract(tmp_path, monkeypatch):
    calls = []

    class StopAfterVersion(Exception):
        pass

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            arguments = command[command.index("-m") + 2 :]
            code = cli.main(arguments)
            if code:
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code)
        raise StopAfterVersion

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(StopAfterVersion):
        module.prepare("synthetic-image", tmp_path, "0.2.1")
    assert len(calls) == 2


def test_bundle_inventory_records_supplied_buildkit_config_digest(tmp_path, monkeypatch):
    class StopAfterInventory(Exception):
        pass

    def run(command, **kwargs):
        if "-m" in command:
            return subprocess.CompletedProcess(command, 0)
        if command[-1] == "-":
            kwargs["stdout"].write(b'{"python_packages": []}\n')
            return subprocess.CompletedProcess(command, 0)
        raise StopAfterInventory

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: '[{"Id":"sha256:' + "a" * 64 + '","Config":{"Labels":{}}}]',
    )
    monkeypatch.setattr(
        module,
        "run_platform_coverage",
        lambda image, output, expected_platform: {
            "status": "passed",
            "platform": expected_platform,
        },
    )

    with pytest.raises(StopAfterInventory):
        module.prepare(
            "synthetic-image",
            tmp_path,
            "0.2.1",
            expected_platform="arm64",
            config_digest="sha256:" + "b" * 64,
        )

    report = json.loads((tmp_path / "runtime-inventory.json").read_text())
    assert report["artifact"]["local_image_id"] == "sha256:" + "a" * 64
    assert report["artifact"]["config_digest"] == "sha256:" + "b" * 64
    assert report["artifact"]["config_digest_source"] == "buildkit-metadata"
    assert report["artifact"]["platform"] == "arm64"


def test_bundle_gate_exercises_both_decoders_and_loaded_library_maps():
    source = (ROOT / "scripts/prepare-release-bundle.py").read_text(encoding="utf-8")

    assert "from torchcodec.decoders import AudioDecoder" in source
    assert "AudioDecoder(source).get_all_samples()" in source
    assert "with av.open(str(source))" in source
    assert "Path('/proc/self/maps').read_text()" in source
    assert "startswith('/opt/asr-media/lib/libavcodec.so')" in source
    assert "'/libx264.so'" in source and "'/libx265.so'" in source


def test_source_bundle_pins_the_locked_pyav_sdist():
    spec = json.loads((ROOT / "third_party/bundle-spec.json").read_text())
    av = next(item for item in spec["source_artifacts"] if item["id"] == "av-sdist-18.1.0")

    assert av["url"].endswith("/av-18.1.0.tar.gz")
    assert av["sha256"] == ("47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28")
