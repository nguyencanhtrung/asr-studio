#!/usr/bin/env python3
"""Inventory the executing runtime; metadata is evidence, not redistribution approval.

Run inside each final image with no network or user volumes. JSON goes to stdout.
License texts remain in the image; their hashes identify the reviewed copies.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

GNU_LICENSE_TEXT_RE = re.compile(
    r"(?:\bGNU\s+(?:(?:Lesser|Library)\s+)?General\s+Public\s+License\b"
    r"|\bL?GPL(?:\s*(?:v(?:ersion)?\s*)?\d+(?:\.\d+)*(?:\s+(?:or later|only))?)?\b"
    r"|common-licenses/(?:L?GPL)\b)",
    re.IGNORECASE,
)


def has_gnu_license_text(text: str) -> bool:
    """Detect GNU license prose after treating wrapped lines as whitespace."""

    normalized = re.sub(r"\s+", " ", text)
    return bool(GNU_LICENSE_TEXT_RE.search(normalized))


def file_record(path: Path, name: str) -> dict[str, str]:
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def web_vendor_packages(vendor: Path) -> list[dict]:
    """Name, version, license and source pointer of each vendored package (from PROVENANCE.md)."""

    packages = []
    for provenance in sorted(vendor.glob("*/PROVENANCE.md")):
        text = provenance.read_text(encoding="utf-8")
        title = re.match(r"# (\S+) (\S+)", text)
        license_id = re.search(r"^- License: ([\w.-]+)", text, re.M)
        tarball = re.search(r"^- Source tarball: (\S+)", text, re.M)
        integrity = re.search(r"^- Integrity: `(sha512-[^`]+)`", text, re.M)
        if not (title and license_id and tarball and integrity):
            raise SystemExit(f"{provenance}: missing name, license, tarball or integrity")
        packages.append(
            {
                "name": title[1],
                "version": title[2],
                "license": license_id[1],
                "source_tarball": tarball[1],
                "source_integrity": integrity[1],
            }
        )
    return packages


def distribution_record(distribution) -> dict:
    metadata = distribution.metadata
    notices = []
    missing = []
    for relative in distribution.files or []:
        if not re.search(r"license|licence|copying|copyright|notice", str(relative), re.I):
            continue
        path = Path(distribution.locate_file(relative))
        if path.is_file():
            notices.append(file_record(path, str(relative)))
        else:
            missing.append(str(relative))
    classifiers = [
        value for value in metadata.get_all("Classifier", []) if value.startswith("License ::")
    ]
    expression = metadata.get("License-Expression", "")
    legacy = metadata.get("License", "")
    return {
        "name": metadata["Name"],
        "version": distribution.version,
        "license_expression": expression,
        "license_metadata": legacy,
        "license_classifiers": classifiers,
        "notices": sorted(notices, key=lambda item: item["path"]),
        "missing_notice_files": sorted(missing),
        "needs_license_identification": not (
            expression
            or legacy.strip().upper() not in {"", "UNKNOWN", "UNSPECIFIED"}
            or classifiers
        ),
        "redistribution_review": "pending",
    }


def system_packages() -> list[dict]:
    if not shutil.which("dpkg-query"):
        raise RuntimeError("dpkg-query is required; run inside the Ubuntu runtime image")
    result = subprocess.run(
        # Keep the installed source identity alongside the binary identity.  A binary
        # index can be incomplete for a base-image package even when the source index
        # still contains the exact corresponding source record.
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\t${Architecture}\t${Status}"
            "\t${source:Package}\t${source:Version}\n",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    packages = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 4:
            name, version, architecture, status = fields
            source_package = source_version = None
        elif len(fields) == 6:
            name, version, architecture, status, source_package, source_version = fields
            source_package = source_package or None
            source_version = source_version or None
        else:
            raise RuntimeError(f"unexpected dpkg-query record: {line!r}")
        if status.split()[-1:] != ["installed"]:
            continue
        copyright_path = Path("/usr/share/doc") / name / "copyright"
        license_fields = []
        license_text_contains_gnu = False
        if copyright_path.is_file():
            copyright_text = copyright_path.read_text(errors="replace")
            license_fields = sorted(
                set(re.findall(r"^License:[ \t]*(.+)$", copyright_text, flags=re.MULTILINE))
            )
            # Older Debian copyright files are prose, not DEP-5 fields.
            # Include only prose with an explicit GNU/GPL/LGPL marker.  An empty
            # DEP-5 field is uncertainty, not evidence that the package is license-free.
            license_text_contains_gnu = has_gnu_license_text(copyright_text)
            if license_text_contains_gnu:
                license_fields.append("GPL/LGPL mentioned in copyright text; review exact terms")
        license_fields = sorted(set(license_fields))
        if license_text_contains_gnu:
            license_detection = "gnu-prose"
        elif license_fields:
            license_detection = "dep5"
        elif copyright_path.is_file():
            license_detection = "unknown"
        else:
            license_detection = "missing"
        packages.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "source_package": source_package,
                "source_version": source_version,
                "dpkg_status": status,
                "debian_license_fields": license_fields,
                "license_detection": license_detection,
                "copyright": file_record(copyright_path, str(copyright_path))
                if copyright_path.is_file()
                else None,
                "redistribution_review": "pending",
            }
        )
    return sorted(packages, key=lambda item: item["name"])


def main(*, system_only: bool = False) -> None:
    if system_only:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "scope": "Installed system packages only",
                    "architecture": platform.machine(),
                    "python_packages": [],
                    "system_packages": system_packages(),
                },
                indent=2,
            )
        )
        return
    distributions = sorted(importlib.metadata.distributions(), key=lambda d: d.name.lower())
    omp = Path("/usr/local/bin/omp")
    notices = Path("/usr/local/share/licenses/omp")
    web = Path(importlib.metadata.distribution("asr-studio").locate_file("asr_studio/web"))
    fonts = web / "fonts"
    report = {
        "schema": 1,
        "scope": "Installed runtime only; no models, credentials or user data inspected",
        "redistribution_review": "pending",
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "python_packages": [distribution_record(d) for d in distributions],
        "system_packages": system_packages(),
        "omp": {
            "binary": file_record(omp, str(omp)),
            "notices": [file_record(p, str(p)) for p in sorted(notices.glob("*")) if p.is_file()],
        },
        "fonts": [
            file_record(p, f"asr_studio/web/fonts/{p.name}") for p in sorted(fonts.glob("LICENSE*"))
        ],
        # Unmodified vendored browser code (MPL-2.0): the file, its license and its source pointer.
        "web_vendor": [
            file_record(p, f"asr_studio/web/vendor/{p.relative_to(web / 'vendor')}")
            for p in sorted((web / "vendor").glob("*/*"))
            if p.is_file()
        ],
        "web_vendor_packages": web_vendor_packages(web / "vendor"),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-only", action="store_true")
    main(system_only=parser.parse_args().system_only)
