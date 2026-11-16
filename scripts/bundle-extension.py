#!/usr/bin/env python3
"""Create a reproducible internal bundle for the production Chrome client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    "credentials",
    "credential",
    "node_modules",
    "prototype",
    "prototypes",
    "test",
    "tests",
    "user-data",
    "userdata",
}
EXCLUDED_FILE_NAMES = {
    "credential.json",
    "credentials.json",
    "user-data.json",
    "userdata.json",
}
METADATA_NAME = "BUILD-METADATA.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_files(source: Path) -> list[tuple[str, bytes]]:
    if not source.is_dir():
        raise SystemExit(f"source directory does not exist: {source}")
    files: list[tuple[str, bytes]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.name.casefold() in EXCLUDED_FILE_NAMES:
            continue
        if path.is_symlink():
            raise SystemExit(f"refusing symlink in bundle source: {relative}")
        if not path.is_file():
            continue
        if relative.name == METADATA_NAME:
            continue
        files.append((relative.as_posix(), path.read_bytes()))
    if not any(name == "manifest.json" for name, _ in files):
        raise SystemExit(f"source does not contain a root manifest: {source}")
    return files


def source_tree_hash(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in files:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def git_revision(root: Path, requested: str | None) -> str:
    if requested:
        return requested
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("cannot determine source revision; pass --source-commit") from error
    return result.stdout.strip()


def zip_entry(name: str, content: bytes) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.extra = b""
    info.comment = b""
    info.flag_bits = 0x800
    return info


def write_bundle(output: Path, files: list[tuple[str, bytes]], revision: str) -> dict[str, object]:
    tree_hash = source_tree_hash(files)
    build_metadata = {
        "format": "asr-extension-bundle-v1",
        "source_commit": revision,
        "source_tree_sha256": tree_hash,
        "files": [name for name, _ in files],
    }
    metadata_bytes = (
        json.dumps(build_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    entries = [(METADATA_NAME, metadata_bytes), *files]

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", dir=output.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(temporary_path, "w", compression=ZIP_STORED) as archive:
            for name, content in sorted(entries):
                archive.writestr(zip_entry(name, content), content)
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)

    zip_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum_path = output.with_name(f"{output.name}.sha256")
    checksum_path.write_text(f"{zip_hash}  {output.name}\n", encoding="utf-8")
    sidecar = {
        "artifact": output.name,
        "sha256": zip_hash,
        "source_commit": revision,
        "source_tree_sha256": tree_hash,
        "file_count": len(files),
    }
    metadata_path = output.with_name(f"{output.name}.metadata.json")
    metadata_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "artifact": str(output),
        "checksum": str(checksum_path),
        "metadata": str(metadata_path),
        **sidecar,
    }


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "extensions/chrome")
    parser.add_argument("--output", type=Path, default=root / "dist/asr-studio-extension.zip")
    parser.add_argument("--source-commit")
    args = parser.parse_args()

    source = args.source if args.source.is_absolute() else root / args.source
    output = args.output if args.output.is_absolute() else root / args.output
    files = source_files(source)
    revision = git_revision(root, args.source_commit)
    print(json.dumps(write_bundle(output, files, revision), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
