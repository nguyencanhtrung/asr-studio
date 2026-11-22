"""Opt-in Compose regression. Set ASR_RUN_DOCKER_UPDATE_REGRESSION=1 with a local Docker engine."""

import os
import subprocess
import uuid

import pytest
from test_host_updates import host

pytestmark = pytest.mark.skipif(
    os.environ.get("ASR_RUN_DOCKER_UPDATE_REGRESSION") != "1",
    reason="requires opt-in disposable Docker containers, images and volumes",
)


def test_same_schema_health_failure_restores_previous_container_and_mounts(tmp_path, monkeypatch):
    checkout = tmp_path / "installation"
    checkout.mkdir()
    project = f"asr-update-test-{uuid.uuid4().hex}"
    compose = ("docker", "compose", "-p", project, "-f", "compose.yaml")
    old_image, new_image = (f"{project}:{version}" for version in ("old", "new"))
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key in {"COMPOSE_ENV_FILES", "ASR_IMAGE"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setattr(host, "ROOT", checkout)
    monkeypatch.setattr(host, "COMPOSE", compose)
    monkeypatch.setattr(host, "STATE", checkout / ".asr-studio")
    monkeypatch.setattr(host, "PUBLIC", host.STATE / "updates")
    host.PUBLIC.mkdir(parents=True)
    run, output = host.run, host.output

    (checkout / ".gitignore").write_text(".asr-studio/\n.env\nmodels/\n")
    original_env = f"COMPOSE_PROJECT_NAME={project}\nASR_IMAGE={old_image}\nKEEP_ME=yes\n"
    (checkout / ".env").write_text(original_env)
    models = checkout / "models"
    models.mkdir()
    source = checkout / "src/asr_studio"
    source.mkdir(parents=True)
    (source / "storage.py").write_text("SCHEMA_VERSION = 20\n")
    compose_file = checkout / "compose.yaml"
    compose_file.write_text("""services:
  app:
    image: ${ASR_IMAGE}
    command: ["sleep", "3600"]
    stop_grace_period: 1s
    healthcheck:
      test: ["CMD", "true"]
      interval: 1s
      timeout: 1s
      retries: 1
    volumes:
      - data:/data
      - ./models:/models:ro
volumes:
  data: {}
""")
    (checkout / "scripts").mkdir()
    start = checkout / "scripts/start.sh"
    # Exercise host.startup's image, environment, working directory and exit handling.
    # The fixture checks Compose health without starting ASR workers or downloading models.
    start.write_text(
        '#!/bin/sh\nset -eu\nexec docker compose -p "$COMPOSE_PROJECT_NAME" '
        "-f compose.yaml up --pull never --detach --wait --wait-timeout 20 app\n"
    )
    start.chmod(0o755)
    run("git", "init", "-qb", "main")
    for key, value in [
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ]:
        run("git", "config", key, value)
    (source / "__init__.py").write_text('__version__ = "0.9.0"\n')
    run("git", "add", ".")
    run("git", "commit", "-qm", "installed")
    previous = output("git", "rev-parse", "HEAD")
    run("git", "switch", "-qc", "release")
    (source / "__init__.py").write_text('__version__ = "0.10.0"\n')
    compose_file.write_text(compose_file.read_text().replace('["CMD", "true"]', '["CMD", "false"]'))
    run("git", "commit", "-qam", "release with a failing health check")
    target = output("git", "rev-parse", "HEAD")
    run("git", "tag", "v0.10.0")
    remote = tmp_path / "releases.git"
    run("git", "clone", "--quiet", "--bare", ".", str(remote))
    run("git", "config", f"url.{remote}.insteadOf", f"https://github.com/{host.REPO}.git")
    run("git", "switch", "-q", "main")

    # Registry release transfer and the ASR-specific idle probe are separate contracts.
    def local_release(*args, **kwargs):
        if args not in {
            ("docker", "pull", new_image),
            (*compose, "exec", "-T", "app", "python", "-m", "asr_studio.updates"),
        }:
            return run(*args, **kwargs)
        return subprocess.CompletedProcess(args, 0)

    actual_start = host.startup
    starts = []

    def verified_start(mode, image):
        try:
            actual_start(mode, image)
        finally:
            container = output(*compose, "ps", "--all", "-q", "app")
            starts.append(
                (
                    mode,
                    output(
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Config.Image}} {{.State.Health.Status}}",
                        container,
                    ),
                )
            )

    monkeypatch.setattr(host, "run", local_release)
    monkeypatch.setattr(host, "startup", verified_start)
    try:
        # Both local image refs carry the real commit label checked by the updater.
        seed = output("docker", "create", "alpine:3", "true")
        try:
            for image, revision in [(old_image, previous), (new_image, target)]:
                run(
                    "docker",
                    "commit",
                    "--change",
                    f"LABEL org.opencontainers.image.revision={revision}",
                    seed,
                    image,
                )
        finally:
            run("docker", "rm", seed)
        actual_start("--offline", old_image)
        before = output(*compose, "ps", "-q", "app")
        layout = host.running_layout(before)
        assert layout == (project, {"/data": project + "_data", "/models": str(models)})
        run(*compose, "exec", "-T", "app", "sh", "-c", "echo prior-audio > /data/recording")
        with pytest.raises(subprocess.CalledProcessError):
            host.apply({"tag": "v0.10.0", "image": new_image}, scheduled=True)
        assert starts == [
            ("--pull", new_image + " unhealthy"),
            ("--offline", old_image + " healthy"),
        ]
        after = output(*compose, "ps", "-q", "app")
        assert after != before and host.running_layout(after) == layout
        assert output(*compose, "exec", "-T", "app", "cat", "/data/recording") == "prior-audio"
        assert output("git", "rev-parse", "HEAD") == previous
        assert output("git", "branch", "--show-current") == "main"
        assert (checkout / ".env").read_text() == original_env
        assert not (host.PUBLIC / "maintenance").exists()
        assert not (host.STATE / "update-transaction.json").exists()
    finally:
        subprocess.run([*compose, "down", "--volumes"], cwd=checkout, check=False, timeout=60)
        subprocess.run(["docker", "image", "rm", old_image, new_image], check=False, timeout=60)
