from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_release_sources", ROOT / "scripts/collect-release-sources.py"
)
assert SPEC and SPEC.loader
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


def test_control_parser_retains_multiline_source_checksums():
    raw = (
        "Package: example\nVersion: 1\nChecksums-Sha256: abc 12 example.dsc\n"
        " def 34 example.tar.xz\n\n"
    )

    record = sources.parse_control_records(raw)[0]

    assert record["Package"] == "example"
    assert "def 34 example.tar.xz" in record["Checksums-Sha256"]
    assert sources.checksum_entries(record) == [
        {"sha256": "abc", "size": "12", "filename": "example.dsc"},
        {"sha256": "def", "size": "34", "filename": "example.tar.xz"},
    ]


def test_source_field_preserves_explicit_source_version():
    binary = {"Package": "libexample1", "Version": "2:1.2-3", "Source": "example (1.2-3)"}
    assert sources.source_from_binary_field(binary) == ("example", "1.2-3")


def test_source_field_defaults_to_binary_version():
    binary = {"Package": "example", "Version": "1.2-3"}
    assert sources.source_from_binary_field(binary) == ("example", "1.2-3")


def test_unmapped_binary_uses_exact_dpkg_source_metadata_before_launchpad(monkeypatch, tmp_path):
    binary_index = {
        "suite": "noble",
        "component": "main",
        "kind": "binary",
        "path": str(tmp_path / "Packages.gz"),
    }
    source_index = {
        "suite": "noble",
        "component": "main",
        "kind": "source",
        "path": str(tmp_path / "Sources.gz"),
    }
    source_record = {
        "Package": "libxcrypt",
        "Version": "4.4.36-4build1",
        "Directory": "pool/main/libx/libxcrypt",
        "Checksums-Sha256": "abc 3 libxcrypt.dsc",
    }

    monkeypatch.setattr(
        sources,
        "ensure_indexes",
        lambda *args, **kwargs: ([binary_index, source_index], []),
    )
    monkeypatch.setattr(
        sources,
        "parse_index",
        lambda path, source: [source_record] if source else [],
    )
    collected_groups = []

    def collect_group(group, *args, **kwargs):
        collected_groups.append(group)
        return {**group, "status": "collected", "errors": []}

    monkeypatch.setattr(sources, "collect_one_source_group", collect_group)
    monkeypatch.setattr(
        sources,
        "collect_launchpad_group",
        lambda *args, **kwargs: pytest.fail("Launchpad alias fallback was used"),
    )
    monkeypatch.setattr(sources, "collect_explicit_sources", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(sources, "write_json", lambda *args, **kwargs: None)

    manifest = sources.collect(
        {},
        {
            "architecture": "x86_64",
            "system_packages": [
                {
                    "name": "libcrypt1",
                    "version": "1:4.4.36-4build1",
                    "architecture": "amd64",
                    "source_package": "libxcrypt",
                    "source_version": "4.4.36-4build1",
                    "debian_license_fields": ["GPL/LGPL mentioned in copyright text"],
                    "license_detection": "gnu-prose",
                    "copyright": None,
                }
            ],
        },
        tmp_path / "artifacts",
        True,
        1,
    )

    assert manifest["status"] == "collected"
    assert len(collected_groups) == 1
    assert collected_groups[0]["resolution"] == "dpkg-source-metadata"
    assert (collected_groups[0]["source_package"], collected_groups[0]["source_version"]) == (
        "libxcrypt",
        "4.4.36-4build1",
    )
    assert collected_groups[0]["binary_packages"][0]["name"] == "libcrypt1"


def test_validate_archive_reads_real_tar_input(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("example-1.0/COPYING")
        content = b"license\n"
        member.size = len(content)
        output.addfile(member, __import__("io").BytesIO(content))

    assert sources.validate_archive(archive) == "example-1.0/COPYING"


def test_validate_archive_rejects_corrupt_archive(tmp_path):
    archive = tmp_path / "broken.tar.gz"
    archive.write_bytes(b"not a tar archive")

    with pytest.raises(sources.CollectionError, match="invalid source archive"):
        sources.validate_archive(archive)


def test_source_index_filename_matches_official_layout():
    assert sources.index_filename("noble-updates", "main", "binary") == (
        "noble-updates-main-binary-amd64-Packages.gz"
    )
    assert sources.index_filename("noble-updates", "main", "source") == (
        "noble-updates-main-source-Sources.gz"
    )


def test_launchpad_fallback_extracts_file_hashes_and_aliases():
    page = """
    <tr><td><a href="https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/
    util-linux/2.39.3-9ubuntu6.2/util-linux_2.39.3.dsc">file</a></td>
    <td>abc</td><td>0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef</td></tr>
    """

    files = sources.parse_launchpad_files(page)

    assert files[0]["filename"] == "util-linux_2.39.3.dsc"
    assert files[0]["sha256"] == (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert sources.launchpad_source_alias("libuuid1") == "util-linux"


def test_cli_reports_the_exact_blocker_and_keeps_failure_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sources.sys, "argv", ["collect-release-sources.py", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setattr(sources, "load_json", lambda _: {})
    blocker = "download failed for https://example.invalid/source.tar.xz: HTTP Error 503"
    monkeypatch.setattr(sources, "collect", lambda *_: {"status": "blocked", "blockers": [blocker]})

    assert sources.main() == 2

    assert blocker in capsys.readouterr().err


def test_source_group_recovers_from_a_transient_download_failure(monkeypatch, tmp_path):
    import hashlib
    import io
    import urllib.error

    payload = b"verified source file"
    checksum = hashlib.sha256(payload).hexdigest()
    calls = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", None, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sources, "retry_sleep", lambda _: None, raising=False)
    group = {"source_package": "example", "source_version": "1"}
    record = {
        "Package": "example",
        "Version": "1",
        "Directory": "pool/main/e/example",
        "Checksums-Sha256": f"{checksum} {len(payload)} example_1.dsc",
        "index": {"url": "https://example.invalid/Sources.gz"},
    }

    result = sources.collect_one_source_group(
        group, record, tmp_path, "https://example.invalid/ubuntu", False
    )

    assert result["status"] == "collected"
    assert result["errors"] == []
    assert result["files"][0]["validated"] is True
    assert len(calls) == 2


def test_source_download_retry_is_bounded(monkeypatch, tmp_path):
    import urllib.error

    calls = []
    delays = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", None, None)

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sources, "retry_sleep", delays.append, raising=False)
    destination = tmp_path / "source.tar.xz"

    with pytest.raises(sources.CollectionError, match="HTTP Error 503"):
        sources.download_checked(
            "https://example.invalid/source.tar.xz", destination, None, offline=False
        )

    assert len(calls) == 3
    assert delays == [1, 2]
    assert not destination.exists()
    assert not list(tmp_path.glob("*.download"))


def test_source_download_does_not_retry_missing_files(monkeypatch, tmp_path):
    import urllib.error

    calls = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sources, "retry_sleep", lambda _: pytest.fail("404 must not be retried"))

    with pytest.raises(sources.CollectionError, match="HTTP Error 404"):
        sources.download_checked(
            "https://example.invalid/source", tmp_path / "source", None, offline=False
        )

    assert len(calls) == 1


def test_source_download_keeps_checksum_mismatch_blocked(monkeypatch, tmp_path):
    import io

    calls = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return io.BytesIO(b"wrong source bytes")

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sources, "retry_sleep", lambda _: pytest.fail("bad hashes stay blocked"))

    with pytest.raises(sources.CollectionError, match="sha256 mismatch"):
        sources.download_checked(
            "https://example.invalid/source", tmp_path / "source", "0" * 64, offline=False
        )

    assert len(calls) == 1


def test_source_retry_discards_a_partial_download(monkeypatch, tmp_path):
    import hashlib
    import io

    payload = b"complete verified source"
    calls = []

    class InterruptedStream(io.BytesIO):
        def read(self, *_args):
            if self.tell():
                raise ConnectionResetError("interrupted source transfer")
            return super().read(4)

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return InterruptedStream(payload) if len(calls) == 1 else io.BytesIO(payload)

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sources, "retry_sleep", lambda _: None)
    destination = tmp_path / "source"

    report = sources.download_checked(
        "https://example.invalid/source",
        destination,
        hashlib.sha256(payload).hexdigest(),
        offline=False,
    )

    assert report["validated"] is True
    assert destination.read_bytes() == payload
    assert len(calls) == 2
    assert not list(tmp_path.glob("*.download"))
