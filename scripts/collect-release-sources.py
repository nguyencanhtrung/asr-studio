#!/usr/bin/env python3
"""Collect exact upstream and Ubuntu corresponding-source artifacts.

The source collector maps installed binary package/version records to the
source package/version in official Ubuntu indexes.  It downloads every source
file listed by the matching ``Sources`` record and verifies the index SHA-256.
Unmapped records, missing indexes, missing hashes, and download failures are
retained as blockers; they never become an automatic pass.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import sleep as retry_sleep

DEFAULT_TIMEOUT = 180
SUITES = {
    "noble": "https://archive.ubuntu.com/ubuntu",
    "noble-updates": "https://archive.ubuntu.com/ubuntu",
    "noble-security": "https://security.ubuntu.com/ubuntu",
}
COMPONENTS = ("main", "universe", "multiverse")
LAUNCHPAD_SOURCE_ALIASES = {
    "base-files": "base-files",
    "libc-bin": "glibc",
    "libc6": "glibc",
    "bsdutils": "util-linux",
    "ca-certificates": "ca-certificates",
    "dpkg": "dpkg",
    "libaudit-common": "audit",
    "libaudit1": "audit",
    "libblkid1": "util-linux",
    "libbz2-1.0": "bzip2",
    "libgnutls30t64": "gnutls28",
    "libldap2": "openldap",
    "liblzma5": "xz-utils",
    "libmount1": "util-linux",
    "libp11-kit0": "p11-kit",
    "libpam-modules": "pam",
    "libpam-modules-bin": "pam",
    "libpam-runtime": "pam",
    "libpam0g": "pam",
    "libproc2-0": "procps",
    "libsmartcols1": "util-linux",
    "libsqlite3-0": "sqlite3",
    "libssl3t64": "openssl",
    "libsystemd0": "systemd",
    "libudev1": "systemd",
    "libuuid1": "util-linux",
    "mount": "util-linux",
    "procps": "procps",
    "util-linux": "util-linux",
}


class CollectionError(RuntimeError):
    """A source artifact could not be proved."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def request_for(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "asr-studio-source-collector/0.1",
            "Accept": "*/*",
        },
    )


def _download_checked_once(
    url: str,
    destination: Path,
    expected_sha256: str | None,
    *,
    offline: bool,
) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        if offline:
            raise CollectionError(f"offline artifact missing: {destination}")
        temporary = destination.with_name(f".{destination.name}.download")
        try:
            with (
                urllib.request.urlopen(request_for(url), timeout=DEFAULT_TIMEOUT) as response,
                temporary.open("wb") as stream,
            ):
                shutil.copyfileobj(response, stream)
            os.replace(temporary, destination)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            temporary.unlink(missing_ok=True)
            raise CollectionError(f"download failed for {url}: {exc}") from exc
    actual = sha256_path(destination)
    if expected_sha256 and actual != expected_sha256:
        raise CollectionError(
            f"sha256 mismatch for {url}: expected {expected_sha256}, got {actual}"
        )
    return {
        "url": url,
        "path": str(destination),
        "expected_sha256": expected_sha256,
        "actual_sha256": actual,
        "bytes": destination.stat().st_size,
        "validated": bool(expected_sha256 and actual == expected_sha256),
    }


def download_checked(
    url: str,
    destination: Path,
    expected_sha256: str | None,
    *,
    offline: bool,
) -> dict[str, object]:
    attempt = 1
    while True:
        try:
            return _download_checked_once(url, destination, expected_sha256, offline=offline)
        except CollectionError as error:
            cause = error.__cause__
            retryable = isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError))
            if isinstance(cause, urllib.error.HTTPError):
                retryable = cause.code in {408, 429, 500, 502, 503, 504}
            if not retryable or attempt >= 3:
                raise
            print(f"Retrying source download ({attempt + 1}/3): {url}", file=sys.stderr)
            retry_sleep(attempt)
            attempt += 1


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"cannot read JSON {path}: {exc}") from exc


def parse_control_records(raw: str) -> list[dict[str, str]]:
    """Parse Debian control paragraphs while retaining multiline fields."""

    records: list[dict[str, str]] = []
    for paragraph in re.split(r"\n\s*\n", raw.strip()):
        record: dict[str, str] = {}
        current: str | None = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")) and current:
                record[current] += "\n" + line[1:]
                continue
            if ": " in line:
                current, value = line.split(": ", 1)
                record[current] = value
            elif line.endswith(":"):
                current = line[:-1]
                record[current] = ""
        if record:
            records.append(record)
    return records


def parse_index(path: Path, source: bool) -> list[dict[str, str]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
            raw = stream.read()
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    records = parse_control_records(raw)
    required = ("Package", "Version")
    return [record for record in records if all(key in record for key in required)]


def archive_base(suite: str, architecture: str) -> str:
    return "https://ports.ubuntu.com/ubuntu-ports" if architecture == "arm64" else SUITES[suite]


def index_url(suite: str, component: str, kind: str, architecture: str = "amd64") -> str:
    base = archive_base(suite, architecture)
    if kind == "binary":
        return f"{base}/dists/{suite}/{component}/binary-{architecture}/Packages.gz"
    return f"{base}/dists/{suite}/{component}/source/Sources.gz"


def index_filename(suite: str, component: str, kind: str, architecture: str = "amd64") -> str:
    if kind == "binary":
        return f"{suite}-{component}-binary-{architecture}-Packages.gz"
    return f"{suite}-{component}-source-Sources.gz"


def ensure_indexes(
    index_dir: Path, *, offline: bool, architecture: str = "amd64"
) -> tuple[list[dict], list[str]]:
    indexes: list[dict] = []
    blockers: list[str] = []
    for suite in SUITES:
        for component in COMPONENTS:
            for kind in ("binary", "source"):
                path = index_dir / index_filename(suite, component, kind, architecture)
                url = index_url(suite, component, kind, architecture)
                try:
                    artifact = download_checked(url, path, None, offline=offline)
                    indexes.append(
                        {
                            "suite": suite,
                            "component": component,
                            "kind": kind,
                            "url": url,
                            "path": str(path),
                            "sha256": artifact["actual_sha256"],
                            "bytes": artifact["bytes"],
                        }
                    )
                except CollectionError as exc:
                    blockers.append(str(exc))
    return indexes, blockers


def source_from_binary_field(binary: dict[str, str]) -> tuple[str, str]:
    source = binary.get("Source", binary["Package"])
    match = re.match(r"([^ ]+) \(([^)]+)\)$", source)
    if match:
        return match.group(1), match.group(2)
    return source, binary["Version"]


def launchpad_source_alias(binary_package: str) -> str | None:
    return LAUNCHPAD_SOURCE_ALIASES.get(binary_package)


def parse_launchpad_files(page: str) -> list[dict[str, str]]:
    """Read Launchpad's public source-file table and its SHA-256 values."""

    entries: list[dict[str, str]] = []
    for row in re.findall(r"<tr>(.*?)</tr>", page, flags=re.DOTALL | re.IGNORECASE):
        link = re.search(r'href="([^"]+/\+sourcefiles/[^\"]+/([^\"/]+))"', row)
        checksum = re.search(r"\b([0-9a-f]{64})\b", row, flags=re.IGNORECASE)
        if not link or not checksum:
            continue
        entries.append(
            {
                "url": html.unescape(link.group(1)),
                "filename": html.unescape(link.group(2)),
                "sha256": checksum.group(1).lower(),
            }
        )
    return entries


def checksum_entries(source_record: dict[str, str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    checksums = source_record.get("Checksums-Sha256", "")
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 3:
            checksum, size, filename = parts
            entries.append({"sha256": checksum, "size": size, "filename": filename})
    if entries:
        return entries
    for line in source_record.get("Files", "").splitlines():
        parts = line.split()
        if len(parts) == 3:
            checksum, size, filename = parts
            entries.append({"md5": checksum, "size": size, "filename": filename})
    return entries


def load_inventory_groups(
    inventory: dict,
    binary_indexes: list[tuple[dict, list[dict]]],
    exclusions: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    binary_by_key: dict[tuple[str, str], list[dict]] = {}
    for metadata, records in binary_indexes:
        for record in records:
            binary_by_key.setdefault((record["Package"], record["Version"]), []).append(
                {**record, "index": metadata}
            )

    groups: dict[tuple[str, str], dict] = {}
    unmapped: list[dict] = []
    for package in inventory.get("system_packages", []):
        if any(
            item["name"] == package["name"]
            and item["version"] == package["version"]
            and item["copyright_sha256"] == (package.get("copyright") or {}).get("sha256")
            for item in (exclusions or [])
        ):
            continue
        licenses = " ".join(package.get("debian_license_fields", []))
        has_gnu_license = package.get("license_detection") == "gnu-prose"
        if "GPL" not in licenses and "LGPL" not in licenses and not has_gnu_license:
            continue
        hits = binary_by_key.get((package["name"], package["version"]), [])
        if not hits:
            unmapped.append(
                {
                    "binary_package": package["name"],
                    "version": package["version"],
                    "architecture": package.get("architecture"),
                    "source_package": package.get("source_package"),
                    "source_version": package.get("source_version"),
                    "license_fields": package.get("debian_license_fields", []),
                    "license_detection": package.get("license_detection", "unknown"),
                    "reason": (
                        "exact binary package/version absent from downloaded official indexes"
                    ),
                }
            )
            continue
        binary = hits[0]
        source_name, source_version = source_from_binary_field(binary)
        key = (source_name, source_version)
        group = groups.setdefault(
            key,
            {
                "source_package": source_name,
                "source_version": source_version,
                "binary_packages": [],
                "binary_indexes": [],
            },
        )
        group["binary_packages"].append(
            {
                "name": package["name"],
                "version": package["version"],
                "architecture": package.get("architecture"),
                "source_package": package.get("source_package"),
                "source_version": package.get("source_version"),
                "license_fields": package.get("debian_license_fields", []),
                "license_detection": package.get("license_detection", "unknown"),
            }
        )
        group["binary_indexes"].append(binary["index"])
    return groups, unmapped


def find_source_records(
    source_indexes: list[tuple[dict, list[dict]]],
) -> dict[tuple[str, str], list[dict]]:
    found: dict[tuple[str, str], list[dict]] = {}
    for metadata, records in source_indexes:
        for record in records:
            if "Package" not in record or "Version" not in record:
                continue
            found.setdefault((record["Package"], record["Version"]), []).append(
                {**record, "index": metadata}
            )
    return found


def validate_archive(path: Path) -> str | None:
    if not path.name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        return None
    try:
        with tarfile.open(path, mode="r:*") as archive:
            first = archive.next()
            return first.name if first else None
    except (OSError, tarfile.TarError) as exc:
        raise CollectionError(f"invalid source archive {path}: {exc}") from exc


def collect_explicit_sources(
    spec: dict, artifact_root: Path, offline: bool
) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    blockers: list[str] = []
    download_dir = artifact_root / "downloads"
    explicit = [item for item in spec.get("source_artifacts", []) if item.get("url")]
    explicit.extend(spec.get("ctranslate2_submodules", []))
    for item in explicit:
        destination = download_dir / item["artifact_name"]
        try:
            artifact = download_checked(
                item["url"], destination, item.get("sha256"), offline=offline
            )
            first_member = validate_archive(destination)
            records.append(
                {**item, **artifact, "first_archive_member": first_member, "status": "collected"}
            )
        except CollectionError as exc:
            records.append({**item, "status": "blocked", "error": str(exc)})
            blockers.append(str(exc))
    return records, blockers


def collect_one_source_group(
    group: dict,
    source_record: dict,
    artifact_root: Path,
    source_base: str,
    offline: bool,
) -> dict:
    source_name = group["source_package"]
    source_version = group["source_version"]
    safe_name = re.sub(r"[^A-Za-z0-9_.+-]+", "_", f"{source_name}_{source_version}")
    destination_root = artifact_root / "debian-sources" / safe_name
    files = checksum_entries(source_record)
    if not files:
        return {
            **group,
            "status": "blocked",
            "reason": "source index record has no SHA-256 or legacy file entries",
            "source_record": {
                key: source_record.get(key)
                for key in ("Package", "Version", "Directory", "Section")
            },
        }

    file_records: list[dict] = []
    errors: list[str] = []
    directory = source_record.get("Directory", "").strip("/")
    for entry in files:
        filename = entry["filename"]
        url = f"{source_base.rstrip('/')}/{directory}/{urllib.parse.quote(filename)}"
        destination = destination_root / filename
        try:
            artifact = download_checked(url, destination, entry.get("sha256"), offline=offline)
            file_records.append({**entry, **artifact, "status": "collected"})
        except CollectionError as exc:
            file_records.append({**entry, "url": url, "status": "blocked", "error": str(exc)})
            errors.append(str(exc))
    return {
        **group,
        "status": "blocked" if errors else "collected",
        "source_record": {
            key: source_record.get(key)
            for key in ("Package", "Version", "Directory", "Section", "Priority", "Binary")
            if key in source_record
        },
        "source_index": source_record["index"],
        "files": file_records,
        "errors": errors,
        "artifact_directory": str(destination_root),
    }


def collect_launchpad_group(
    group: dict,
    artifact_root: Path,
    offline: bool,
) -> dict:
    source_name = group["source_package"]
    source_version = group["source_version"]
    safe_name = re.sub(r"[^A-Za-z0-9_.+-]+", "_", f"{source_name}_{source_version}")
    version_candidates = [source_version]
    if ":" in source_version:
        version_candidates.append(source_version.split(":", 1)[1])
    page_artifact: dict[str, object] | None = None
    page_url = ""
    page_path = Path()
    page_errors: list[str] = []
    resolved_source_version = source_version
    for candidate in version_candidates:
        quoted_name = urllib.parse.quote(source_name, safe="")
        quoted_version = urllib.parse.quote(candidate, safe="~.+-:")
        page_url = f"https://launchpad.net/ubuntu/+source/{quoted_name}/{quoted_version}/+files"
        page_path = (
            artifact_root
            / "source-manifests"
            / f"launchpad-{safe_name}-{re.sub(r'[^A-Za-z0-9_.+-]+', '_', candidate)}.html"
        )
        try:
            page_artifact = download_checked(page_url, page_path, None, offline=offline)
            resolved_source_version = candidate
            break
        except CollectionError as exc:
            page_errors.append(str(exc))
    if page_artifact is None:
        return {
            **group,
            "status": "blocked",
            "source_index": {"url": page_url},
            "errors": page_errors,
        }
    page = page_path.read_text(encoding="utf-8", errors="replace")
    files = parse_launchpad_files(page)
    if not files:
        return {
            **group,
            "status": "blocked",
            "source_index": {
                "url": page_url,
                "sha256": page_artifact["actual_sha256"],
                "resolved_source_version": resolved_source_version,
            },
            "errors": [f"Launchpad source page has no file table: {page_url}"],
        }
    destination_root = artifact_root / "debian-sources" / safe_name
    file_records: list[dict] = []
    errors: list[str] = []
    for file in files:
        destination = destination_root / file["filename"]
        try:
            artifact = download_checked(file["url"], destination, file["sha256"], offline=offline)
            file_records.append({**file, **artifact, "status": "collected"})
        except CollectionError as exc:
            file_records.append({**file, "status": "blocked", "error": str(exc)})
            errors.append(str(exc))
    return {
        **group,
        "status": "blocked" if errors else "collected",
        "source_index": {
            "kind": "launchpad",
            "url": page_url,
            "sha256": page_artifact["actual_sha256"],
            "resolved_source_version": resolved_source_version,
        },
        "files": file_records,
        "errors": errors,
        "artifact_directory": str(destination_root),
    }


def collect(
    spec: dict, inventory: dict, artifact_root: Path, offline: bool, max_workers: int
) -> dict:
    artifact_root.mkdir(parents=True, exist_ok=True)
    index_dir = artifact_root / "source-manifests"
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
        inventory.get("architecture"), inventory.get("architecture", "amd64")
    )
    if architecture not in {"amd64", "arm64"}:
        raise CollectionError(f"Unsupported inventory architecture: {architecture}")
    index_dir = index_dir if architecture == "amd64" else index_dir / architecture
    index_records, index_blockers = ensure_indexes(
        index_dir, offline=offline, architecture=architecture
    )
    binary_indexes: list[tuple[dict, list[dict]]] = []
    source_indexes: list[tuple[dict, list[dict]]] = []
    for metadata in index_records:
        path = Path(str(metadata["path"]))
        records = parse_index(path, source=metadata["kind"] == "source")
        if metadata["kind"] == "binary":
            binary_indexes.append((metadata, records))
        else:
            source_indexes.append((metadata, records))

    groups, unmapped = load_inventory_groups(
        inventory, binary_indexes, spec.get("source_exclusions", [])
    )
    source_records = find_source_records(source_indexes)

    # A package can be absent from the downloaded binary indexes while its
    # installed dpkg record still gives the exact source identity.  Prefer an
    # official Sources record for that identity so its index and file hashes are
    # retained in the manifest; only use Launchpad as the final fallback.
    source_metadata_unmapped: list[dict] = []
    for item in unmapped:
        source_key = (item.get("source_package"), item.get("source_version"))
        candidates = source_records.get(source_key, []) if all(source_key) else []
        if not candidates:
            source_metadata_unmapped.append(item)
            continue
        group = groups.setdefault(
            source_key,
            {
                "source_package": source_key[0],
                "source_version": source_key[1],
                "binary_packages": [],
                "binary_indexes": [],
                "resolution": "dpkg-source-metadata",
            },
        )
        group["binary_packages"].append(
            {
                "name": item["binary_package"],
                "version": item["version"],
                "architecture": item.get("architecture"),
                "source_package": item.get("source_package"),
                "source_version": item.get("source_version"),
                "license_fields": item.get("license_fields", []),
                "license_detection": item.get("license_detection", "unknown"),
            }
        )

    group_jobs: list[tuple[dict, dict, str]] = []
    unresolved_sources: list[dict] = []
    for key, group in sorted(groups.items()):
        candidates = source_records.get(key, [])
        if not candidates:
            unresolved_sources.append(
                {
                    **group,
                    "status": "blocked",
                    "reason": (
                        "exact source package/version absent from downloaded official "
                        "Sources indexes"
                    ),
                }
            )
            continue
        source_record = candidates[0]
        source_base = archive_base(source_record["index"]["suite"], architecture)
        group_jobs.append((group, source_record, source_base))

    gcc_required = next(
        (item for item in spec.get("source_artifacts", []) if item.get("id") == "gcc-14-source"),
        None,
    )
    if gcc_required:
        gcc_key = (gcc_required["source_package"], gcc_required["version"])
        if gcc_key not in groups:
            candidates = source_records.get(gcc_key, [])
            if candidates:
                gcc_group = {
                    "source_package": gcc_key[0],
                    "source_version": gcc_key[1],
                    "binary_packages": [],
                    "binary_indexes": [],
                    "explicit_requirement": gcc_required["required_for"],
                }
                source_record = candidates[0]
                group_jobs.append(
                    (gcc_group, source_record, SUITES[source_record["index"]["suite"]])
                )
            else:
                unresolved_sources.append(
                    {
                        "source_package": gcc_key[0],
                        "source_version": gcc_key[1],
                        "binary_packages": [],
                        "status": "blocked",
                        "reason": (
                            "explicit GCC source requirement absent from downloaded official "
                            "Sources indexes"
                        ),
                    }
                )

    launchpad_groups: dict[tuple[str, str], dict] = {}
    remaining_unmapped: list[dict] = []
    for item in source_metadata_unmapped:
        source_name = item.get("source_package") or launchpad_source_alias(item["binary_package"])
        if source_name is None:
            remaining_unmapped.append(item)
            continue
        source_version = item.get("source_version") or item["version"]
        key = (source_name, source_version)
        group = launchpad_groups.setdefault(
            key,
            {
                "source_package": source_name,
                "source_version": source_version,
                "binary_packages": [],
                "binary_indexes": [],
                "resolution": (
                    "launchpad-dpkg-source-metadata"
                    if item.get("source_package")
                    else "launchpad-versioned-source-page"
                ),
            },
        )
        group["binary_packages"].append(
            {
                "name": item["binary_package"],
                "version": item["version"],
                "architecture": item.get("architecture"),
                "source_package": item.get("source_package"),
                "source_version": item.get("source_version"),
                "license_fields": item.get("license_fields", []),
                "license_detection": item.get("license_detection", "unknown"),
            }
        )

    source_group_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(collect_one_source_group, group, record, artifact_root, base, offline)
            for group, record, base in group_jobs
        ]
        for future in as_completed(futures):
            source_group_records.append(future.result())
    source_group_records.sort(key=lambda item: (item["source_package"], item["source_version"]))
    source_group_records.extend(unresolved_sources)

    launchpad_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(collect_launchpad_group, group, artifact_root, offline)
            for _, group in sorted(launchpad_groups.items())
        ]
        for future in as_completed(futures):
            launchpad_records.append(future.result())
    launchpad_records.sort(key=lambda item: (item["source_package"], item["source_version"]))
    source_group_records.extend(launchpad_records)

    explicit_records, explicit_blockers = collect_explicit_sources(spec, artifact_root, offline)
    blockers = list(index_blockers) + list(explicit_blockers)
    blockers.extend(
        f"unmapped GPL/LGPL binary {item['binary_package']}={item['version']}: {item['reason']}"
        for item in remaining_unmapped
    )
    blockers.extend(
        f"unresolved source {item['source_package']}={item['source_version']}: {item['reason']}"
        for item in unresolved_sources
    )
    blockers.extend(
        f"source group {item['source_package']}={item['source_version']}: {error}"
        for item in source_group_records
        for error in item.get("errors", [])
    )

    gcc_available = (
        any(
            item["source_package"] == gcc_required.get("source_package")
            and item["source_version"] == gcc_required["version"]
            for item in source_group_records
            if "source_package" in item
        )
        if gcc_required
        else False
    )
    if gcc_required and not gcc_available:
        blockers.append(
            "explicit GCC source "
            f"{gcc_required['source_package']}={gcc_required['version']} was not matched "
            "in official source indexes"
        )

    manifest = {
        "schema": 1,
        "task": "issue17-license-bundle",
        "inventory": {
            "architecture": architecture,
            "sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            "python_count": len(inventory.get("python_packages", [])),
            "system_count": len(inventory.get("system_packages", [])),
        },
        "official_indexes": index_records,
        "explicit_sources": explicit_records,
        "reviewed_source_exclusions": spec.get("source_exclusions", []),
        "gpl_lgpl": {
            "binary_record_count": sum(
                1
                for package in inventory.get("system_packages", [])
                if "GPL" in " ".join(package.get("debian_license_fields", []))
                or "LGPL" in " ".join(package.get("debian_license_fields", []))
                or package.get("license_detection") == "gnu-prose"
            ),
            "source_group_count": len(source_group_records),
            "collected_group_count": sum(
                item.get("status") == "collected" for item in source_group_records
            ),
            "unmapped_binary_records": remaining_unmapped,
            "launchpad_binary_record_count": sum(
                len(item.get("binary_packages", [])) for item in launchpad_records
            ),
            "source_groups": source_group_records,
        },
        "status": "blocked" if blockers else "collected",
        "blockers": sorted(set(blockers)),
    }
    write_json(artifact_root / "source-manifest.json", manifest)
    write_json(
        Path("third_party/SOURCE-MANIFEST.json"),
        {
            "schema": 1,
            "status": manifest["status"],
            "artifact_manifest": str(artifact_root / "source-manifest.json"),
            "official_index_count": len(index_records),
            "gpl_lgpl_binary_record_count": manifest["gpl_lgpl"]["binary_record_count"],
            "source_group_count": manifest["gpl_lgpl"]["source_group_count"],
            "collected_group_count": manifest["gpl_lgpl"]["collected_group_count"],
            "blocker_count": len(manifest["blockers"]),
            "blockers": manifest["blockers"],
        },
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("third_party/bundle-spec.json"))
    parser.add_argument(
        "--inventory", type=Path, default=Path("docs/release/runtime-inventory-amd64.json")
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--artifact-root", type=Path, default=Path("/tmp/asr-issue17-bundle"))
    parser.add_argument(
        "--offline", action="store_true", help="validate existing indexes/artifacts only"
    )
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument(
        "--allow-unresolved", action="store_true", help="write the blocked manifest and exit zero"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    os.chdir(repo_root)
    spec_path = (repo_root / args.spec).resolve() if not args.spec.is_absolute() else args.spec
    inventory_path = (
        (repo_root / args.inventory).resolve()
        if not args.inventory.is_absolute()
        else args.inventory
    )
    spec = load_json(spec_path)
    inventory = load_json(inventory_path)
    manifest = collect(
        spec, inventory, args.artifact_root.resolve(), args.offline, max(1, args.max_workers)
    )
    print(
        json.dumps(
            {"status": manifest["status"], "blockers": len(manifest["blockers"])}, sort_keys=True
        )
    )
    for blocker in manifest["blockers"]:
        print(f"Source collection blocked: {blocker}", file=sys.stderr)
    if manifest["blockers"] and not args.allow_unresolved:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
