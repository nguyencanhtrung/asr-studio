"""Host update decisions, checkout transitions and Linux request admission."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from asr_studio import updates
from asr_studio.storage import database_path, initialize

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_update", ROOT / "scripts/update.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
IMAGE = host.IMAGE + "@sha256:" + "b" * 64


def test_check_requires_newer_stable_and_a_platform_digest():
    # https://docs.github.com/en/rest/releases/releases#get-the-latest-release
    stable = {"tag_name": "v0.10.0", "draft": False, "prerelease": False}
    assert host.release_tag(stable, "0.9.8") == "v0.10.0"
    for release in [
        dict(stable, prerelease=True),
        dict(stable, draft=True),
        dict(stable, tag_name="v0.10.0-rc1"),
    ]:
        assert host.release_tag(release, "0.9.8") is None
    assert host.release_tag(stable, "0.11.0") is None
    # docker buildx imagetools inspect --raw returns the registry manifest/index document.
    # https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/
    # https://github.com/opencontainers/image-spec/blob/v1.1.1/image-index.md
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + "b" * 64}
        ]
    }
    assert host.image_for(manifest, "linux/amd64") == IMAGE
    with pytest.raises(ValueError, match="No published image"):
        host.image_for(manifest, "linux/arm64")


def test_offline_makes_no_release_request(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "PUBLIC", tmp_path)
    monkeypatch.setattr(sys, "argv", ["update", "--scheduled"])
    (tmp_path / "mode").write_text("offline")
    monkeypatch.setattr(host, "discover", lambda: pytest.fail("offline made a release request"))
    host.main()


def test_exclusive_admission_and_durable_pending_work(tmp_path, monkeypatch):
    for setting in ["ASR_UPDATE_DIR", "ASR_DATA_DIR", "ASR_INSTALL_DIR"]:
        monkeypatch.setenv(setting, str(tmp_path))
    db = initialize(tmp_path)
    lock = tmp_path / "admission.lock"
    lock.touch()
    with updates.admit_work() as ok:
        assert ok
        with pytest.raises(SystemExit, match="deferred"), host.exclusive(lock):
            pytest.fail("host obtained exclusive lock during a request")
    with host.exclusive(lock):
        with updates.admit_work() as ok:
            assert not ok
        updates.require_idle()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO uploads(id, filename, declared_size, status, raw_key) "
                "VALUES('a','clip.webm',64,'receiving','raw/a')"
            )
        with pytest.raises(SystemExit, match="pending upload a"):
            updates.require_idle()


def _seed_update_work(
    data_dir: Path,
    *,
    workflow_state: str,
    waiting_reason: str | None = None,
    task_status: str | None = None,
) -> None:
    """Create the smallest valid durable workflow/task fixture for admission checks."""
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) "
            "VALUES ('update-meeting', 'Update', 'en')"
        )
        connection.execute(
            "INSERT INTO uploads(id, filename, declared_size, status, raw_key, managed_key) "
            "VALUES ('update-upload', 'clip.webm', 1, 'ready', 'raw/update', 'recordings/update')"
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, upload_id, storage_key, content_type, "
            "duration_seconds) VALUES ('update-recording', 'update-meeting', "
            "'update-upload', 'recordings/update', 'audio/mp4', 1)"
        )
        connection.execute(
            "INSERT INTO processing_workflows(id, meeting_id, recording_id, state, asr_language) "
            "VALUES (?, 'update-meeting', 'update-recording', ?, 'en')",
            ("w" * 32, workflow_state),
        )
        connection.execute(
            "INSERT INTO processing_steps(id, workflow_id, kind, state, waiting_reason) "
            "VALUES (?, ?, 'notes', ?, ?)",
            ("s" * 32, "w" * 32, workflow_state, waiting_reason),
        )
        if task_status is not None:
            connection.execute(
                "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
                "model_name, device, compute_type, language_hint, status, stage, "
                "audio_duration_seconds) VALUES (?, ?, ?, 'update-meeting', 'transcription', "
                "'/models/whisper-small', 'whisper-small', 'cpu', 'int8', 'en', ?, 'running', 1)",
                ("t" * 32, "update-task-request", "h" * 64, task_status),
            )
        connection.commit()


def _require_idle_with_seeded_work(tmp_path, monkeypatch, **seed) -> None:
    data_dir = tmp_path / "data"
    initialize(data_dir)
    for setting, value in (
        ("ASR_UPDATE_DIR", tmp_path),
        ("ASR_DATA_DIR", data_dir),
        ("ASR_INSTALL_DIR", tmp_path / "install"),
    ):
        monkeypatch.setenv(setting, str(value))
    (tmp_path / "admission.lock").touch()
    _seed_update_work(data_dir, **seed)
    with host.exclusive(tmp_path / "admission.lock"):
        updates.require_idle()


def test_provider_unconfigured_waiting_notes_workflow_allows_update(tmp_path, monkeypatch):
    _require_idle_with_seeded_work(
        tmp_path,
        monkeypatch,
        workflow_state="waiting",
        waiting_reason="provider_unconfigured",
    )


def test_running_workflow_defers_and_identifies_the_blocker(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match=r"pending processing workflow w{32}"):
        _require_idle_with_seeded_work(tmp_path, monkeypatch, workflow_state="running")


def test_active_task_defers_and_identifies_the_blocker(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="pending task t{32}"):
        _require_idle_with_seeded_work(
            tmp_path, monkeypatch, workflow_state="succeeded", task_status="running"
        )


def test_model_installation_defers_and_identifies_request_filename(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    install_dir = tmp_path / "install"
    request_dir = install_dir / "requests"
    request_dir.mkdir(parents=True)
    (request_dir / "model-2026-11-22.json").write_text("{}")
    for setting, value in (
        ("ASR_UPDATE_DIR", tmp_path),
        ("ASR_DATA_DIR", data_dir),
        ("ASR_INSTALL_DIR", install_dir),
    ):
        monkeypatch.setenv(setting, str(value))
    lock = tmp_path / "admission.lock"
    lock.touch()
    with (
        host.exclusive(lock),
        pytest.raises(SystemExit, match="model installation model-2026-11-22[.]json"),
    ):
        updates.require_idle()


def test_backup_data_does_not_publish_a_failed_archive_and_preserves_existing_backup(
    tmp_path, monkeypatch
):
    backup = tmp_path / "before-release.tar"
    calls = []

    def fail_tar(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "run"):
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(host, "run", fail_tar)
    with pytest.raises(subprocess.CalledProcessError):
        host.backup_data(backup, "old-container", "old-image")
    assert not backup.exists()
    assert not list(tmp_path.glob(".cold-backup-*"))

    def successful_tar(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "run"):
            kwargs["stdout"].write(b"verified archive")

    monkeypatch.setattr(host, "run", successful_tar)
    host.backup_data(backup, "old-container", "old-image")
    assert backup.read_bytes() == b"verified archive"
    assert not list(tmp_path.glob(".cold-backup-*"))

    original = backup.read_bytes()
    with pytest.raises(FileExistsError):
        host.backup_data(backup, "old-container", "old-image")
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob(".cold-backup-*"))
    assert calls


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    checkout = tmp_path / "installation"
    checkout.mkdir()
    monkeypatch.setattr(host, "ROOT", checkout)
    public = checkout / ".asr-studio/updates"
    public.mkdir(parents=True)
    monkeypatch.setattr(host, "PUBLIC", public)
    monkeypatch.setattr(host, "STATE", public.parent)
    source = checkout / "src/asr_studio"
    source.mkdir(parents=True)
    (checkout / ".gitignore").write_text(".asr-studio/\n.env\n")
    (checkout / ".env").write_text("COMPOSE_PROJECT_NAME=demo\nASR_IMAGE=asr:prior\n")
    remote = tmp_path / "releases.git"
    host.run("git", "init", "-qb", "main")
    host.run("git", "config", "user.name", "Fixture")
    host.run("git", "config", "user.email", "test@example.invalid")
    host.run("git", "config", "commit.gpgsign", "false")
    host.run("git", "config", "core.hooksPath", "/dev/null")
    host.run("git", "config", f"url.{remote}.insteadOf", f"https://github.com/{host.REPO}.git")
    (source / "__init__.py").write_text('__version__ = "0.9.0"\n')
    (source / "storage.py").write_text("SCHEMA_VERSION = 20\n")
    host.run("git", "add", ".")
    host.run("git", "commit", "-qm", "installed")
    previous = host.output("git", "rev-parse", "HEAD")
    host.run("git", "switch", "-qc", "release")
    (source / "__init__.py").write_text('__version__ = "0.10.0"\n')
    host.run("git", "commit", "-qam", "new version")
    target = host.output("git", "rev-parse", "HEAD")
    host.run("git", "tag", "v0.10.0")
    host.run("git", "clone", "--bare", "--quiet", ".", str(remote))
    host.run("git", "switch", "-q", "main")
    real_output = host.output
    real_run = host.run
    # test_host_updates_docker exercises these fields against the engine and Compose directly.
    # https://docs.docker.com/reference/api/engine/ (ContainerInspect and ImageInspect)
    # https://docs.docker.com/reference/cli/docker/compose/config/
    mounts = [
        {"Type": "volume", "Name": "demo_data", "Source": "/docker/data", "Destination": "/data"}
    ]
    config = {
        "name": "demo",
        "volumes": {"data": {"name": "demo_data"}},
        "services": {"app": {"volumes": [{"type": "volume", "target": "/data", "source": "data"}]}},
    }
    responses = {
        (*host.COMPOSE, "ps", "-q", "app"): "container",
        ("docker", "inspect", "--format", "{{.Image}}", "container"): "oldid",
        ("docker", "inspect", "--format", "{{.Config.Image}}", "container"): "asr:prior",
        ("docker", "image", "inspect", "--format", "{{.Id}}", "asr:prior"): "oldid",
        ("docker", "inspect", "container"): json.dumps(
            [{"Mounts": mounts, "Config": {"Labels": {"com.docker.compose.project": "demo"}}}]
        ),
        (*host.COMPOSE, "config", "--format", "json"): json.dumps(config),
    }
    monkeypatch.setattr(
        host, "output", lambda *a: responses[a] if a[0] == "docker" else real_output(*a)
    )
    monkeypatch.setattr(host, "image_revision", lambda ref: {"oldid": previous, IMAGE: target}[ref])
    calls = []

    def commands(*args, **kwargs):
        if args[0] != "docker":
            return real_run(*args, **kwargs)
        assert args in [
            ("docker", "pull", IMAGE),
            (*host.COMPOSE, "exec", "-T", "app", "python", "-m", "asr_studio.updates"),
        ]
        calls.append(args)

    monkeypatch.setattr(host, "run", commands)
    return checkout, public, previous, target, calls


@pytest.mark.parametrize("bad_ref", ["oldid", IMAGE])
def test_revision_mismatch_preserves_checkout_and_image_pin(deployment, monkeypatch, bad_ref):
    checkout, public, previous, _, _ = deployment
    image_revision = host.image_revision
    monkeypatch.setattr(
        host, "image_revision", lambda ref: "wrong" if ref == bad_ref else image_revision(ref)
    )
    before = (checkout / ".env").read_bytes()
    with pytest.raises(ValueError, match="must match|does not match"):
        host.apply({"tag": "v0.10.0", "image": IMAGE}, scheduled=False)
    assert host.output("git", "rev-parse", "HEAD") == previous
    assert (checkout / ".env").read_bytes() == before
    assert not (public / "maintenance").exists()


def test_concurrent_host_command_defers_before_discovery(deployment, monkeypatch):
    _, public, _, _, calls = deployment
    (public / "mode").write_text("online")
    monkeypatch.setattr(sys, "argv", ["update", "--apply"])
    monkeypatch.setattr(host, "discover", lambda: pytest.fail("concurrent release discovery"))
    with (
        host.exclusive(public.parent / "updater.lock"),
        pytest.raises(SystemExit, match="another operation"),
    ):
        host.main()
    assert not calls


@pytest.mark.parametrize("health_failure", [False, True])
def test_apply_and_health_failure_restore_matching_checkout(
    deployment, monkeypatch, health_failure
):
    checkout, public, previous, target, calls = deployment

    def startup(mode, image):
        assert (public / "maintenance").exists()
        assert (public.parent / "update-transaction.json").is_file()
        calls.append((mode, image))
        if health_failure and mode == "--pull":
            raise subprocess.CalledProcessError(1, "installation check")

    monkeypatch.setattr(host, "startup", startup)
    release = {"tag": "v0.10.0", "image": IMAGE}
    if health_failure:
        with pytest.raises(subprocess.CalledProcessError):
            host.apply(release, scheduled=True)
        assert calls[-1] == ("--offline", "asr:prior")
        assert host.output("git", "rev-parse", "HEAD") == previous
        assert host.output("git", "branch", "--show-current") == "main"
        assert (checkout / ".env").read_text() == "COMPOSE_PROJECT_NAME=demo\nASR_IMAGE=asr:prior\n"
    else:
        host.apply(release, scheduled=True)
        assert calls[-1] == ("--pull", IMAGE)
        assert host.output("git", "rev-parse", "HEAD") == target
        assert (checkout / ".env").read_text() == f"COMPOSE_PROJECT_NAME=demo\nASR_IMAGE={IMAGE}\n"
        assert (
            json.loads((public.parent / "last-update.json").read_text())["previous_commit"]
            == previous
        )
    assert not (public / "maintenance").exists()
    assert not (public.parent / "update-transaction.json").exists()


def test_scheduled_schema_change_and_dirty_checkout_leave_installation_running(
    deployment, monkeypatch
):
    checkout, _, previous, target, calls = deployment
    release = {"tag": "v0.10.0", "image": IMAGE}
    monkeypatch.setattr(host, "schema_at", lambda commit: 21 if commit == target else 20)
    with pytest.raises(ValueError, match="Schema change"):
        host.apply(release, scheduled=True)
    (checkout / "local-changes").touch()
    with pytest.raises(ValueError, match="local changes"):
        host.apply(release, scheduled=False)
    assert host.output("git", "rev-parse", "HEAD") == previous
    assert not calls


def test_failed_migration_retains_backup_and_closes_admission(deployment, monkeypatch):
    checkout, public, previous, target, _ = deployment
    monkeypatch.setattr(host, "schema_at", lambda commit: 21 if commit == target else 20)
    stops = []
    run = host.run

    def docker_stop(*args, **kwargs):
        if args == (*host.COMPOSE, "stop"):
            stops.append(host.output("git", "rev-parse", "HEAD"))
        else:
            return run(*args, **kwargs)

    def cold_copy(backup, container, image):
        assert stops == [previous]
        backup.write_bytes(b"fixture archive")

    def failed_start(mode, image):
        assert mode == "--pull"
        raise subprocess.CalledProcessError(1, "installation check")

    monkeypatch.setattr(host, "run", docker_stop)
    monkeypatch.setattr(host, "backup_data", cold_copy)
    monkeypatch.setattr(host, "startup", failed_start)
    with pytest.raises(SystemExit, match="Maintenance retained"):
        host.apply({"tag": "v0.10.0", "image": IMAGE}, scheduled=False)
    journal = json.loads((public.parent / "update-transaction.json").read_text())
    assert journal["previous_commit"] == previous
    assert Path(journal["backup"]).read_bytes() == b"fixture archive"
    assert stops == [previous, target]
    assert (public / "maintenance").is_file()
    assert IMAGE in (checkout / ".env").read_text()


def test_check_publishes_verified_result_and_hides_it_if_new_image_is_missing(
    tmp_path, monkeypatch
):
    import io

    public = tmp_path / "updates"
    public.mkdir()
    (public / "mode").write_text("online")
    monkeypatch.setattr(host, "STATE", tmp_path)
    monkeypatch.setattr(host, "PUBLIC", public)
    monkeypatch.setenv("ASR_UPDATE_DIR", str(public))
    monkeypatch.setattr(sys, "argv", ["update", "--check"])
    release = {"tag_name": "v99.0.0", "draft": False, "prerelease": False}
    monkeypatch.setattr(
        host, "urlopen", lambda *args, **kw: io.BytesIO(json.dumps(release).encode())
    )
    images = [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + "b" * 64}
    ]

    def docker(*args):
        if args[:2] == ("docker", "version"):
            return "linux/amd64"
        assert args[-1] == f"{host.IMAGE}:{release['tag_name']}"
        return json.dumps({"manifests": images})

    monkeypatch.setattr(host, "output", docker)
    host.main()
    assert updates.available_version() == "v99.0.0"
    release["tag_name"] = "v99.0.1"
    images.clear()
    with pytest.raises(ValueError, match="No published image"):
        host.main()
    assert updates.available_version() == ""
