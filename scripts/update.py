"""Opt-in host updater for a running Linux Docker Compose installation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

REPO = "nguyencanhtrung/asr-studio"
IMAGE = f"ghcr.io/{REPO}"
ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".asr-studio"
PUBLIC = STATE / "updates"
COMPOSE = ("docker", "compose", "-f", "compose.yaml")
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, cwd=ROOT, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True, timeout=90).stdout.strip()


def write_file(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def exclusive(path):
    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Update deferred: another operation is active.") from None
        yield


def release_tag(release, installed):
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise ValueError("Invalid release response from GitHub.")
    tag = release["tag_name"]
    target = TAG.fullmatch(tag)
    current = TAG.fullmatch("v" + installed)
    if not current:
        raise ValueError("The checkout does not identify a stable installed version.")
    if not target or release["draft"] is not False or release["prerelease"] is not False:
        return None
    return tag if tuple(map(int, target.groups())) > tuple(map(int, current.groups())) else None


def image_for(manifest, platform):
    for item in manifest.get("manifests", []):
        target = item.get("platform", {})
        if f"{target.get('os')}/{target.get('architecture')}" == platform:
            digest = item["digest"]
            if DIGEST.fullmatch(digest):
                return f"{IMAGE}@{digest}"
    raise ValueError(f"No published image for {platform}.")


def discover():
    source = (ROOT / "src/asr_studio/__init__.py").read_text()
    installed = re.search(r'^__version__ = "([^"]+)"$', source, re.M)[1]
    request = Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urlopen(request, timeout=15) as response:
        tag = release_tag(json.load(response), installed)
    if not tag:
        return None
    platform = output("docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}")
    manifest = output("docker", "buildx", "imagetools", "inspect", "--raw", f"{IMAGE}:{tag}")
    return {"tag": tag, "image": image_for(json.loads(manifest), platform)}


def pinned_env(previous, image):
    lines = previous.decode().splitlines(keepends=True)
    matches = [
        i for i, line in enumerate(lines) if re.match(r"^\s*(export\s+)?ASR_IMAGE\s*=", line)
    ]
    if len(matches) > 1:
        raise ValueError("Resolve duplicate ASR_IMAGE entries in .env before applying.")
    assignment = f"ASR_IMAGE={image}\n"
    if matches:
        lines[matches[0]] = assignment
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(assignment)
    return "".join(lines).encode()


def schema_at(commit):
    source = output("git", "show", f"{commit}:src/asr_studio/storage.py")
    match = re.search(r"^SCHEMA_VERSION = ([0-9]+)$", source, re.M)
    if not match:
        raise ValueError("Cannot determine the schema for the release.")
    return int(match[1])


def image_revision(ref):
    return output(
        "docker",
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        ref,
    )


def startup(mode, image):
    env = {**os.environ, "ASR_IMAGE": image, "ASR_UPDATER_OWNS_LOCK": "1"}
    env.pop("ASR_SOURCE_REVISION", None)
    run("./scripts/start.sh", mode, env=env)


def running_layout(container):
    details = json.loads(output("docker", "inspect", container))[0]
    project = details["Config"]["Labels"]["com.docker.compose.project"]
    mounts = {
        m["Destination"]: m.get("Name", m["Source"])
        for m in details["Mounts"]
        if m["Type"] in {"volume", "bind"}
    }
    return project, mounts


def check_layout(layout):
    project, mounts = layout
    config = json.loads(output(*COMPOSE, "config", "--format", "json"))
    planned = {}
    for mount in config["services"]["app"]["volumes"]:
        source = mount["source"]
        if mount["type"] == "volume":
            source = config["volumes"][source]["name"]
        planned[mount["target"]] = source
    if config["name"] != project or any(planned.get(k) != v for k, v in mounts.items()):
        raise ValueError("Compose project, volume or host mount changed. Review manually.")


def backup_data(backup, container, old_id):
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as archive:
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--pull=never",
            "--volumes-from",
            container + ":ro",
            "--entrypoint",
            "tar",
            old_id,
            "-C",
            "/var/lib/asr-studio/data",
            "-cf",
            "-",
            ".",
            stdout=archive,
        )
        os.fsync(archive.fileno())
    run("tar", "-tf", str(backup), stdout=subprocess.DEVNULL)


def require_clean_checkout():
    if output("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Update deferred: checkout has local changes.")


def apply(release, scheduled):
    if sys.platform != "linux" or not shutil.which("flock"):
        raise ValueError("Applying updates requires Linux with flock on the Docker host.")
    require_clean_checkout()
    if Path(output("git", "rev-parse", "--show-toplevel")).resolve() != ROOT:
        raise ValueError("The installation directory must be its own Git checkout.")
    if os.environ.get("COMPOSE_ENV_FILES"):
        raise ValueError("Use the installation checkout and its default .env for updates.")
    previous = output("git", "rev-parse", "HEAD")
    branch = output("git", "branch", "--show-current")
    container = output(*COMPOSE, "ps", "-q", "app")
    old_id = output("docker", "inspect", "--format", "{{.Image}}", container)
    old_image = output("docker", "inspect", "--format", "{{.Config.Image}}", container)
    if (
        image_revision(old_id) != previous
        or output("docker", "image", "inspect", "--format", "{{.Id}}", old_image) != old_id
    ):
        raise ValueError("Running app, local image and checkout must match before an update.")
    tag, image = release["tag"], release["image"]
    run(
        "git",
        "fetch",
        "--no-tags",
        f"https://github.com/{REPO}.git",
        f"refs/tags/{tag}:refs/tags/{tag}",
    )
    target = output("git", "rev-parse", f"refs/tags/{tag}^{{commit}}")
    target_version = output("git", "show", f"{target}:src/asr_studio/__init__.py")
    if f'__version__ = "{tag[1:]}"' not in target_version.splitlines():
        raise ValueError("Release tag does not match the target application version.")
    migration = schema_at(previous) != schema_at(target)
    if migration and scheduled:
        raise ValueError("Schema change: run --apply manually to create a cold data backup.")
    run("docker", "pull", image)
    if image_revision(image) != target:
        raise ValueError("Image revision does not match release tag.")
    env_path = ROOT / ".env"
    if env_path.is_symlink():
        raise ValueError("Manage the image pin manually for a symlinked .env.")
    had_env = env_path.exists()
    before = env_path.read_bytes() if had_env else b""
    mode = env_path.stat().st_mode & 0o777 if had_env else 0o600
    after = pinned_env(before, image)
    layout = running_layout(container)
    check_layout(layout)
    with exclusive(PUBLIC / "admission.lock"):
        run(*COMPOSE, "exec", "-T", "app", "python", "-m", "asr_studio.updates")
        record = {
            "previous_commit": previous,
            "previous_image": old_image,
            "previous_image_id": old_id,
            "previous_branch": branch,
            "target": tag,
            "target_image": image,
            "schema_change": migration,
            "data_volume": layout[1].get("/var/lib/asr-studio/data"),
        }
        backup = STATE / f"before-{tag}-{previous[:12]}.tar"
        if migration and backup.exists():
            raise ValueError(f"Preserve or move the existing backup before retrying: {backup}")
        record["backup"] = str(backup) if migration else None
        journal = STATE / "update-transaction.json"
        write_file(journal, json.dumps(record).encode())
        marker = PUBLIC / "maintenance"
        marker.touch()
        switched = False
        pin_written = False
        startup_attempted = False
        try:
            if migration:
                run(*COMPOSE, "stop")
                backup_data(backup, container, old_id)
            require_clean_checkout()
            if output("git", "rev-parse", "HEAD") != previous:
                raise ValueError("Checkout changed while preparing the update.")
            if (
                env_path.is_symlink()
                or env_path.exists() != had_env
                or (had_env and env_path.read_bytes() != before)
            ):
                raise ValueError("The .env file changed while preparing the update.")
            run("git", "switch", "--detach", target)
            switched = True
            write_file(env_path, after, mode)
            pin_written = True
            check_layout(layout)
            startup_attempted = True
            startup("--pull", image)
        except (Exception, KeyboardInterrupt):
            if migration and startup_attempted:
                run(*COMPOSE, "stop")
                raise SystemExit(
                    f"Maintenance retained. Restore {backup} and the previous release "
                    f"using {journal} before reopening."
                ) from None
            if switched:
                if branch:
                    run("git", "switch", branch)
                else:
                    run("git", "switch", "--detach", previous)
            if pin_written:
                if env_path.is_symlink() or env_path.read_bytes() != after:
                    raise SystemExit(
                        f"The image pin changed. Recover manually using {journal}."
                    ) from None
                if had_env:
                    write_file(env_path, before, mode)
                else:
                    env_path.unlink(missing_ok=True)
            if startup_attempted or migration:
                startup("--offline", old_image)
            marker.unlink()
            journal.unlink()
            raise
        marker.unlink()
        journal.replace(STATE / "last-update.json")


def main():
    parser = argparse.ArgumentParser(prog="./scripts/update.sh", description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Refresh the cached release notice.")
    actions.add_argument("--apply", action="store_true", help="Apply an available stable release.")
    actions.add_argument(
        "--scheduled", action="store_true", help="Apply only when the database schema is unchanged."
    )
    args = parser.parse_args()
    mode = PUBLIC / "mode"
    if not mode.exists() or mode.read_text().strip() != "online":
        print("Updates disabled. Start the installation in online mode to enable host checks.")
        return
    with exclusive(STATE / "updater.lock"):
        if mode.read_text().strip() != "online":
            return
        if (STATE / "update-transaction.json").exists() or (PUBLIC / "maintenance").exists():
            raise ValueError(
                "Unfinished update. Recover using .asr-studio/update-transaction.json."
            )
        public = PUBLIC / "release.json"
        try:
            release = discover()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            # --apply rechecks the latest tag. Hide a cached command it can no longer fulfill.
            public.unlink(missing_ok=True)
            raise
        write_file(public, json.dumps(release or {}).encode(), 0o644)
        if not release:
            print("No newer stable release.")
        elif args.check:
            print(f"{release['tag']} is available: {release['image']}")
        else:
            apply(release, args.scheduled)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
