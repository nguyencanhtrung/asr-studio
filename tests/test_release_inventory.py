from __future__ import annotations

import importlib.util
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "release_inventory", ROOT / "scripts/release-inventory.py"
)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def test_inventory_preserves_bundled_notices_and_reports_missing_files(tmp_path):
    metadata = Message()
    metadata["Name"] = "example"
    metadata["License-Expression"] = "MIT"
    license_path = tmp_path / "LICENSE"
    license_path.write_text("upstream license")
    distribution = SimpleNamespace(
        metadata=metadata,
        version="1.0",
        files=[Path("LICENSE"), Path("NOTICE-missing"), Path("module.py")],
        locate_file=lambda relative: tmp_path / relative,
    )
    record = inventory.distribution_record(distribution)
    assert record["license_expression"] == "MIT"
    assert record["notices"] == [inventory.file_record(license_path, "LICENSE")]
    assert record["missing_notice_files"] == ["NOTICE-missing"]
    assert record["redistribution_review"] == "pending"


def test_inventory_does_not_infer_license_from_presence_of_notice(tmp_path):
    metadata = Message()
    metadata["Name"] = "unknown"
    (tmp_path / "LICENSE").write_text("Terms require review")
    distribution = SimpleNamespace(
        metadata=metadata,
        version="1",
        files=[Path("LICENSE")],
        locate_file=lambda relative: tmp_path / relative,
    )
    assert inventory.distribution_record(distribution)["needs_license_identification"]
    metadata["License"] = "UNKNOWN"
    assert inventory.distribution_record(distribution)["needs_license_identification"]


def test_inventory_detects_wrapped_gnu_license_prose_and_abbreviations():
    assert inventory.has_gnu_license_text("GNU Lesser General Public\nLicense, version 2.1")
    assert inventory.has_gnu_license_text("This package is distributed under the LGPL-2.1+")
    assert inventory.has_gnu_license_text("See /usr/share/common-licenses/GPL.")
    assert not inventory.has_gnu_license_text(
        "Copyright 2026 Example; terms are reviewed separately."
    )


def test_system_inventory_includes_held_installed_packages(monkeypatch):
    monkeypatch.setattr(inventory.shutil, "which", lambda name: "/usr/bin/dpkg-query")
    monkeypatch.setattr(
        inventory.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "example-installed\t1\tamd64\tinstall ok installed\texample-source\t1\n"
                "example-held\t2\tamd64\thold ok installed\texample-source\t2\n"
                "example-removed\t3\tamd64\tdeinstall ok config-files\texample-source\t3\n"
            )
        ),
    )
    packages = inventory.system_packages()
    assert [package["name"] for package in packages] == ["example-held", "example-installed"]
    assert packages[0]["dpkg_status"] == "hold ok installed"
    assert packages[0]["source_package"] == "example-source"
    assert packages[0]["source_version"] == "2"


WEB = ROOT / "src/asr_studio/web"
VENDORED = WEB / "vendor/mediabunny"


def test_inventory_lists_the_vendored_browser_library_with_license_and_source(monkeypatch, capsys):
    real_record = inventory.file_record
    monkeypatch.setattr(
        inventory,
        "file_record",
        lambda path, name: real_record(path, name) if path.exists() else {"path": name},
    )
    monkeypatch.setattr(inventory.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(inventory, "system_packages", lambda: [])
    monkeypatch.setattr(
        inventory.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(locate_file=lambda relative: ROOT / "src" / relative),
    )
    inventory.main()
    report = json.loads(capsys.readouterr().out)
    assert sorted(item["path"] for item in report["web_vendor"]) == [
        "asr_studio/web/vendor/mediabunny/LICENSE",
        "asr_studio/web/vendor/mediabunny/PROVENANCE.md",
        "asr_studio/web/vendor/mediabunny/mediabunny.min.mjs",
    ]
    assert all(len(item["sha256"]) == 64 for item in report["web_vendor"])
    (package,) = report["web_vendor_packages"]
    assert package["name"] == "Mediabunny" and package["version"] == "1.61.6"
    assert package["license"] == "MPL-2.0"
    assert package["source_tarball"].endswith("/mediabunny-1.61.6.tgz")
    assert package["source_integrity"].startswith("sha512-")


def test_notice_manifest_and_bundle_carry_the_vendored_library_evidence():
    # collect() needs network-shaped specs, so check the wiring in the sources.
    collector = (ROOT / "scripts/collect-release-notices.py").read_text(encoding="utf-8")
    assert '"web_vendor_packages": inventory.get("web_vendor_packages"' in collector
    bundle = (ROOT / "scripts/prepare-release-bundle.py").read_text(encoding="utf-8")
    assert 'archive.add("src/asr_studio/web/vendor", arcname="web-vendor")' in bundle


def test_vendored_library_is_the_unmodified_upstream_file_with_its_license():
    import hashlib

    provenance = (VENDORED / "PROVENANCE.md").read_text(encoding="utf-8")
    digest = hashlib.sha256((VENDORED / "mediabunny.min.mjs").read_bytes()).hexdigest()
    assert f"`{digest}`" in provenance  # any edit of the vendored file changes this hash
    assert "mediabunny-1.61.6.tgz" in provenance and "sha512-" in provenance
    assert "Mozilla Public License Version 2.0" in (VENDORED / "LICENSE").read_text(
        encoding="utf-8"
    )
    assert "web/vendor/mediabunny/*" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_system_only_inventory_does_not_require_an_installed_application(monkeypatch, capsys):
    def unexpected_application_lookup(*_args):
        raise AssertionError("System inventory must not inspect Python application packages")

    monkeypatch.setattr(
        inventory.importlib.metadata, "distributions", unexpected_application_lookup
    )
    monkeypatch.setattr(inventory.importlib.metadata, "distribution", unexpected_application_lookup)
    packages = [{"name": "libexample1", "version": "1.0", "source_package": "example"}]
    monkeypatch.setattr(inventory, "system_packages", lambda: packages)

    inventory.main(system_only=True)

    report = json.loads(capsys.readouterr().out)
    assert report["system_packages"] == packages
    assert report["python_packages"] == []
    assert "system" in report["scope"].lower()
