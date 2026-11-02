#!/usr/bin/env python3
"""Collect corresponding source for the exact loaded platform image before publication."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

CONFIG_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def validate_config_digest(value: str) -> str:
    if CONFIG_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"invalid BuildKit config digest: {value}")
    return value


def run_platform_coverage(
    image: str,
    output: Path,
    expected_platform: str | None,
) -> dict:
    coverage_path = output / "platform-coverage.json"
    with (
        Path("scripts/check-platform-coverage.py").open("rb") as source,
        coverage_path.open("wb") as target,
    ):
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image,
                "-",
            ],
            stdin=source,
            stdout=target,
            check=True,
        )
    coverage = json.loads(coverage_path.read_text())
    if coverage.get("status") != "passed":
        raise RuntimeError("platform coverage did not pass")
    if expected_platform is not None and coverage.get("platform") != expected_platform:
        raise RuntimeError(
            f"platform coverage mismatch: expected {expected_platform}, "
            f"got {coverage.get('platform')}"
        )
    return coverage


def prepare(
    image: str,
    output: Path,
    version: str,
    *,
    expected_platform: str | None = None,
    config_digest: str | None = None,
) -> Path:
    if expected_platform is not None and expected_platform not in {"amd64", "arm64"}:
        raise ValueError(f"invalid platform: {expected_platform}")
    if config_digest is not None:
        config_digest = validate_config_digest(config_digest)
    output.mkdir(parents=True, exist_ok=True)
    schema_match = re.search(
        r"^SCHEMA_VERSION = (\d+)$",
        Path("src/asr_studio/storage.py").read_text(),
        flags=re.MULTILINE,
    )
    if schema_match is None:
        raise RuntimeError("Cannot determine source schema version")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-m",
            "asr_studio.cli",
            "version",
            "--json",
            "--expect-version",
            version,
            "--expect-schema",
            schema_match.group(1),
        ],
        check=True,
    )
    inventory = output / "runtime-inventory.json"
    with Path("scripts/release-inventory.py").open("rb") as source, inventory.open("wb") as target:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image,
                "-",
            ],
            stdin=source,
            stdout=target,
            check=True,
        )
    coverage = run_platform_coverage(image, output, expected_platform)
    report = json.loads(inventory.read_text())
    image_metadata = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image], text=True)
    )[0]
    report["artifact"] = {
        "local_image_id": image_metadata["Id"],
        "revision": image_metadata.get("Config", {})
        .get("Labels", {})
        .get("org.opencontainers.image.revision", "unknown"),
        "version": version,
        "schema": int(schema_match.group(1)),
    }
    if config_digest is not None:
        report["artifact"]["config_digest"] = config_digest
        report["artifact"]["config_digest_source"] = "buildkit-metadata"
    if expected_platform is not None:
        report["artifact"]["platform"] = expected_platform
    report["platform_coverage"] = coverage
    inventory.write_text(json.dumps(report, indent=2) + "\n")
    coverage_path = output / "platform-coverage.json"
    notice_root = Path("third_party/notices")
    expected_notices = {
        str(path.relative_to(notice_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in notice_root.rglob("*")
        if path.is_file()
    }
    # Check readable bytes as the default image user, not only the build recipe.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-c",
            """
import ctypes,hashlib,json,shutil,sys,sysconfig,tempfile,wave
from pathlib import Path
assert shutil.which('cuda-gdb') is None
assert not list(Path('/usr/local').glob('cuda*/bin/cuda-gdb*'))
base=Path('/usr/local/share/licenses/asr-studio')
assert (base/'LICENSE').is_file()
for name, expected in json.loads(sys.argv[1]).items():
    assert hashlib.sha256((base/'third-party'/name).read_bytes()).hexdigest()==expected
r=json.loads((base/'openmp-runtime.json').read_text())
root=Path(sysconfig.get_paths()['purelib'])
patterns=('libgomp*.so*','libgfortran*.so*','libquadmath*.so*')
actual={str(p.relative_to(root)) for pattern in patterns for p in root.rglob(pattern)}
assert actual=={item['path'] for item in r['replacements']}
for item in r['replacements']:
    expected=hashlib.sha256(Path(item['source']).read_bytes()).hexdigest()
    assert hashlib.sha256((root/item['path']).read_bytes()).hexdigest()==expected
assert not list(root.glob('av.libs/libx26*.so*'))
with tempfile.TemporaryDirectory() as directory:
    source=Path(directory)/'input.wav'
    with wave.open(str(source),'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\\x01\\x00'*16000)
    # Load TorchCodec first: this is the path that previously selected Ubuntu's
    # GPL FFmpeg before PyAV's RPATH had a chance to help.
    from torchcodec.decoders import AudioDecoder
    decoded=AudioDecoder(source).get_all_samples()
    assert decoded.data.numel() > 0
    import av
    with av.open(str(source)) as container:
        frames=[frame for frame in container.decode(audio=0)]
    assert frames and sum(frame.samples for frame in frames) > 0
mapped={
    fields[-1].split(' (deleted)',1)[0]
    for line in Path('/proc/self/maps').read_text().splitlines()
    for fields in [line.split()]
    if len(fields) >= 6 and fields[-1].startswith('/')
}
codec_maps=sorted(path for path in mapped if '/libavcodec.so' in path)
assert any(path.startswith('/opt/asr-media/lib/libavcodec.so') for path in codec_maps), codec_maps
assert not [path for path in codec_maps if not path.startswith('/opt/asr-media/lib/')], codec_maps
assert not any('/libx264.so' in path or '/libx265.so' in path for path in mapped), sorted(mapped)
for path in codec_maps:
    codec=ctypes.CDLL(path)
    codec.avcodec_license.restype=ctypes.c_char_p
    assert codec.avcodec_license().decode()=='LGPL version 2.1 or later'
assert 'libx264' not in av.codecs_available and 'libx265' not in av.codecs_available
import ctranslate2
assert ctranslate2.get_supported_compute_types('cpu')
""",
            json.dumps(expected_notices),
        ],
        check=True,
    )
    artifacts = output / "source-artifacts"
    subprocess.run(
        [
            sys.executable,
            "scripts/collect-release-sources.py",
            "--inventory",
            str(inventory),
            "--artifact-root",
            str(artifacts),
        ],
        check=True,
    )
    manifest_path = artifacts / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest["status"] != "collected":
        raise RuntimeError("Corresponding source is incomplete")
    archive_path = output / "corresponding-source.tar.gz"
    # All downloaded source packages are retained with their manifests/indexes.
    # The source collector does not download binary wheels.
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(artifacts, arcname="source-artifacts")
        archive.add(inventory, arcname="runtime-inventory.json")
        archive.add(coverage_path, arcname="platform-coverage.json")
        archive.add("third_party/notices", arcname="notices")
        # MPL-2.0 vendored browser code: unmodified file, LICENSE and source pointer.
        archive.add("src/asr_studio/web/vendor", arcname="web-vendor")
        archive.add("third_party/platform-coverage.json", arcname="build/platform-coverage.json")
        archive.add("Dockerfile", arcname="build/Dockerfile")
        archive.add("scripts/build-lgpl-media.sh", arcname="build/build-lgpl-media.sh")
        archive.add("scripts/use-system-openmp.py", arcname="build/use-system-openmp.py")
        archive.add("uv.lock", arcname="build/uv.lock")
        archive.add("pyproject.toml", arcname="build/pyproject.toml")
    checksum = hashlib.file_digest(archive_path.open("rb"), "sha256").hexdigest()
    (output / "SHA256SUMS").write_text(f"{checksum}  {archive_path.name}\n")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", choices=("amd64", "arm64"))
    parser.add_argument("--config-digest")
    args = parser.parse_args()
    print(
        prepare(
            args.image,
            args.output.resolve(),
            args.version,
            expected_platform=args.platform,
            config_digest=args.config_digest,
        )
    )
