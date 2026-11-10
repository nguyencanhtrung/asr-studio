from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from media_tools import require_media_tools

ROOT = Path(__file__).parents[1]
PIPELINE_PATH = ROOT / ".github" / "workflows" / "pipeline.yml"
DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"
CHECKOUT_REVISION = "a" * 40
COMMUNITY_1_FILES = (
    "config.yaml",
    "embedding/pytorch_model.bin",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
    "segmentation/pytorch_model.bin",
)


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _environment_without_git() -> dict[str, str]:
    # A caller such as a git hook exports GIT_DIR and friends; they would redirect git to its repo.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _checkout_copy(destination: Path) -> Path:
    """Copy the files start.sh reads from a checkout, without git metadata."""
    for relative_path in (
        "scripts/start.sh",
        "src/asr_studio/__init__.py",
        "src/asr_studio/storage.py",
    ):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative_path, target)
    return destination


def _commit_in_new_repository(path: Path) -> str:
    """Make `path` a repository root with one empty commit and return that commit."""
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        identity = ("-c", "user.name=test", "-c", "user.email=test@example.invalid")
        return subprocess.run(
            ["git", *identity, "-c", "commit.gpgsign=false", *args],
            cwd=path,
            env=_environment_without_git(),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("init", "--quiet")
    git("commit", "--quiet", "--allow-empty", "--no-verify", "--message", "checkout")
    return git("rev-parse", "HEAD")


def _run_start(
    tmp_path: Path,
    model_files: tuple[str, ...] = (),
    model_overrides: dict[str, bytes] | None = None,
    fail_install_check: bool = False,
    *,
    args: tuple[str, ...] = (),
    image: str | None = None,
    second_image: str | None = None,
    device: str = "cpu",
    image_present: bool = True,
    version_matches: bool = True,
    gpu_check_ok: bool = False,
    model_check_ok: bool = True,
    prepared_asr_model: bool = False,
    source_revision: str | None = CHECKOUT_REVISION,
    image_revision: str = "match",
    checkout: Path = ROOT,
    compose_project: str = "asr-studio",
    existing_volumes: tuple[str, ...] = (),
    copy_fail: str = "none",
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model_root = tmp_path / "models"
    model_directory = model_root / "speaker-diarization-community-1"
    for relative_path in model_files:
        target = model_directory / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"model data")
    for relative_path, content in (model_overrides or {}).items():
        target = model_directory / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if prepared_asr_model:
        asr_model = model_root / "whisper-small"
        asr_model.mkdir(parents=True, exist_ok=True)
        (asr_model / "model.bin").write_bytes(b"prepared")

    stub_directory = tmp_path / "bin"
    stub_directory.mkdir()
    command_log = tmp_path / "docker.log"
    command_log.write_text("", encoding="utf-8")
    _write_executable(
        stub_directory / "docker",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        'if [ "$1" = compose ] && [ "$4" = config ]; then\n'
        '  if [ "$5" = --images ]; then\n'
        '    printf \'%s\\n%s\\n%s\\n\' "$COMPOSE_IMAGE" "$COMPOSE_SECOND_IMAGE" "$COMPOSE_IMAGE"\n'
        '  elif [ "$#" -eq 4 ]; then\n'
        "    printf 'name: %s\\nservices:\\n' \"$COMPOSE_PROJECT\"\n"
        '  elif [ "$5" = --volumes ]; then\n'
        "    printf 'data\\nauth\\nprovider\\nmodel-install\\n'\n"
        "  else\n"
        "    printf 'ASR_DEVICE=%s\\nASR_MODEL_HOST_PATH=%s\\n"
        "ASR_DIARIZATION_MODEL_PATH=/models/speaker-diarization-community-1\\n' "
        '      "$REQUESTED_DEVICE" "$MODEL_ROOT"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = image ] && [ "$2" = inspect ]; then\n'
        '  [ "$IMAGE_PRESENT" = 1 ] || exit 1\n'
        '  [ "$3" = --format ] && printf \'%s\\n\' "$IMAGE_REVISION"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = volume ] && [ "$2" = inspect ]; then\n'
        '  case " $EXISTING_VOLUMES " in *" $3 "*) exit 0 ;; esac\n'
        "  exit 1\n"
        "fi\n"
        'case "$*" in\n'
        '  *"cp -a /from/. /to/"*) case "$*" in *"$COPY_FAIL:/from"*) exit 1 ;; esac ;;\n'
        '  pull*) [ "$PULL_OK" = 1 ] || exit 1 ;;\n'
        '  *"version --json --expect-version"*) [ "$VERSION_MATCH" = 1 ] || exit 1 ;;\n'
        '  *"--gpus all"*) [ "$GPU_CHECK_OK" = 1 ] || exit 1 ;;\n'
        '  *"asr_studio.cli model check"*) [ "$MODEL_CHECK_OK" = 1 ] || exit 1 ;;\n'
        '  *"asr_studio.cli installation check"*) [ "$FAIL_INSTALL_CHECK" = 0 ] || exit 1 ;;\n'
        "esac\n",
    )
    environment = _environment_without_git()
    environment.pop("ASR_IMAGE", None)
    resolved_image = image or "asr-studio:local"
    if image is not None:
        environment["ASR_IMAGE"] = image
    environment["PATH"] = f"{stub_directory}:{environment['PATH']}"
    environment["DOCKER_LOG"] = str(command_log)
    environment["MODEL_ROOT"] = str(model_root)
    environment["COMPOSE_IMAGE"] = resolved_image
    environment["COMPOSE_SECOND_IMAGE"] = second_image or resolved_image
    environment["REQUESTED_DEVICE"] = device
    environment["FAIL_INSTALL_CHECK"] = "1" if fail_install_check else "0"
    environment["IMAGE_PRESENT"] = "1" if image_present else "0"
    environment["VERSION_MATCH"] = "1" if version_matches else "0"
    environment["GPU_CHECK_OK"] = "1" if gpu_check_ok else "0"
    environment["MODEL_CHECK_OK"] = "1" if model_check_ok else "0"
    environment["PULL_OK"] = "1"
    environment["COMPOSE_PROJECT"] = compose_project
    environment["EXISTING_VOLUMES"] = " ".join(existing_volumes)
    environment["COPY_FAIL"] = copy_fail
    if source_revision is None:
        environment.pop("ASR_SOURCE_REVISION", None)
    else:
        environment["ASR_SOURCE_REVISION"] = source_revision
    environment["IMAGE_REVISION"] = source_revision if image_revision == "match" else image_revision
    result = subprocess.run(
        ["sh", str(checkout / "scripts" / "start.sh"), *args],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    result.docker_log = command_log.read_text(encoding="utf-8")
    return result


def test_pipeline_gates_pull_requests_main_and_version_tags():
    pipeline = load_yaml(PIPELINE_PATH)
    triggers = pipeline["on"]

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["tags"] == ["v*"]
    assert pipeline["permissions"] == {"contents": "read"}

    quality = pipeline["jobs"]["quality"]
    check_step = next(step for step in quality["steps"] if step["name"] == "Run repository checks")
    release_check = next(
        step for step in quality["steps"] if step["name"] == "Check release tag and package version"
    )
    assert check_step["run"] == "./scripts/check.sh"
    assert check_step["env"]["ASR_RUN_BROWSER_TESTS"] == "1"
    assert release_check["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert "scripts/check-release.py" in release_check["run"]
    assert "container" in pipeline["jobs"]
    names = [step["name"] for step in quality["steps"]]
    media_step = quality["steps"][names.index("Install media tools")]
    assert "apt-get install" in media_step["run"]
    for tool in ("ffmpeg", "ffprobe"):
        assert f"command -v {tool}" in media_step["run"].splitlines()
    assert names.index("Install media tools") < names.index("Run repository checks")
    node_step = quality["steps"][names.index("Set up Node.js")]
    assert node_step["uses"].startswith("actions/setup-node@")
    assert node_step["with"]["node-version"] == "24"
    assert names.index("Set up Node.js") < names.index("Run repository checks")
    # Missing media tools must fail CI instead of silently skipping media tests.
    assert check_step["env"]["ASR_REQUIRE_MEDIA_TOOLS"] == "1"


def test_media_gate_skips_locally_but_fails_when_ci_requires_tools(monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ASR_REQUIRE_MEDIA_TOOLS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_media_tools()
    monkeypatch.setenv("ASR_REQUIRE_MEDIA_TOOLS", "1")
    with pytest.raises(pytest.fail.Exception):
        require_media_tools()


def test_container_job_runs_runtime_smoke_check_on_the_built_image():
    container = load_yaml(PIPELINE_PATH)["jobs"]["container"]
    steps = container["steps"]
    build = next(step for step in steps if step["name"] == "Build container")
    smoke = next(step for step in steps if step["name"] == "Smoke-test container runtime")

    assert container["timeout-minutes"] >= 30
    assert build["with"]["load"] is True
    assert build["with"]["build-args"] == "VCS_REF=${{ github.sha }}\n"
    assert build["with"]["cache-from"] == "type=gha,scope=container"
    assert "cache-to" not in build["with"]
    assert steps.index(smoke) > steps.index(build)
    assert smoke["run"] == f"./scripts/container-smoke.sh {build['with']['tags']}"
    script = (ROOT / "scripts" / "container-smoke.sh").read_text(encoding="utf-8")
    assert "normalize_media" in script and "/healthz" in script
    assert "from torchcodec.decoders import AudioDecoder" in script
    assert "AudioDecoder(target).get_all_samples()" in script
    assert "decoded.data.numel() > 0" in script
    assert 'print("torchcodec decode ok")' in script


def test_startup_help_and_invalid_modes_exit_before_docker(tmp_path):
    help_result = _run_start(tmp_path / "help", args=("--help",))
    assert help_result.returncode == 0
    assert "--source" in help_result.stdout
    assert "--pull" in help_result.stdout
    assert "--offline" in help_result.stdout
    assert help_result.docker_log == ""

    conflict = _run_start(tmp_path / "conflict", args=("--source", "--offline"))
    assert conflict.returncode == 2
    assert "Choose only one startup mode" in conflict.stderr
    assert conflict.docker_log == ""

    unknown = _run_start(tmp_path / "unknown", args=("--build",))
    assert unknown.returncode == 2
    assert "Unknown startup option" in unknown.stderr
    assert unknown.docker_log == ""


def test_startup_rejects_incomplete_community_model_before_image_build(tmp_path):
    result = _run_start(tmp_path)

    assert result.returncode != 0
    assert "compose -f compose.yaml build" not in result.docker_log
    guidance = result.stdout + result.stderr
    assert "startup_model_incomplete: stage=startup_model_validation" in guidance
    assert "accept the Community-1 conditions" in guidance
    assert "hf auth login" in guidance
    assert "Run git lfs install" in guidance
    assert str(tmp_path) not in guidance
    assert "anonymous" not in guidance.casefold()


@pytest.mark.parametrize("args", [(), ("--source",)], ids=["default-source", "explicit-source"])
def test_startup_checks_complete_community_model_then_preflights_before_services(tmp_path, args):
    result = _run_start(tmp_path, COMMUNITY_1_FILES, args=args)

    assert result.returncode == 0, result.stdout + result.stderr
    commands = result.docker_log.splitlines()

    def command_index(marker):
        return next(i for i, line in enumerate(commands) if marker in line)

    build = command_index("compose -f compose.yaml build")
    image_version = command_index("version --json --expect-version 0.2.2")
    model_prepare = command_index("asr_studio.cli model prepare")
    runtime_check = command_index("asr_studio.cli runtime check")
    services = command_index("compose -f compose.yaml up --pull never --no-build")
    install_check = command_index("asr_studio.cli installation check")
    assert build < image_version < model_prepare < runtime_check < services < install_check
    assert "asr_studio.cli access show" not in commands
    assert "access details are withheld from non-interactive output" in result.stdout
    assert "docker compose exec -T app python -m asr_studio.cli access show" in result.stdout
    assert "--network none" in commands[image_version]
    assert "HF_HUB_OFFLINE=0" in commands[model_prepare]
    assert "TRANSFORMERS_OFFLINE=0" in commands[model_prepare]
    assert "-f compose.model-prepare.yaml run " in commands[model_prepare]
    assert ":/models:rw" not in commands[model_prepare]
    assert all("--pull never" in line for line in commands if "compose" in line and "run " in line)


def test_startup_does_not_print_access_link_when_app_cannot_see_worker_or_models(tmp_path):
    result = _run_start(tmp_path, COMMUNITY_1_FILES, fail_install_check=True)

    assert result.returncode != 0
    assert "python -m asr_studio.cli installation check" in result.docker_log
    assert "python -m asr_studio.cli access show" not in result.docker_log


def test_startup_rejects_git_lfs_pointer_before_image_build(tmp_path):
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        {
            "embedding/pytorch_model.bin": (
                b"version https://git-lfs.github.com/spec/v1\n"
                b"oid sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
                b"size 10\n"
            )
        },
    )

    assert result.returncode != 0
    assert "startup_model_incomplete: stage=startup_model_validation" in result.stderr
    assert "hf auth login --add-to-git-credential" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert "compose -f compose.yaml build" not in result.docker_log


def test_pull_mode_pulls_the_resolved_versioned_image_without_building(tmp_path):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    result = _run_start(tmp_path, COMMUNITY_1_FILES, args=("--pull",), image=image)

    assert result.returncode == 0, result.stdout + result.stderr
    commands = result.docker_log.splitlines()
    pull = commands.index(f"pull {image}")
    version_check = next(
        index for index, line in enumerate(commands) if "version --json --expect-version" in line
    )
    service_run = next(
        index for index, line in enumerate(commands) if " compose" in line and " run " in line
    )
    assert pull < version_check < service_run
    assert not any(" compose " in f" {line} " and " build" in line for line in commands)
    assert image in commands[version_check]


def test_pull_mode_accepts_an_immutable_digest_reference(tmp_path):
    image = f"ghcr.io/nguyencanhtrung/asr-studio@sha256:{'a' * 64}"
    result = _run_start(tmp_path, COMMUNITY_1_FILES, args=("--pull",), image=image)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"pull {image}" in result.docker_log
    assert image in result.docker_log
    assert " compose -f compose.yaml build" not in result.docker_log


@pytest.mark.parametrize(
    "image",
    [
        "asr-studio:local",
        "ghcr.io/nguyencanhtrung/asr-studio:latest",
        "ghcr.io/nguyencanhtrung/asr-studio:edge",
        "ghcr.io/nguyencanhtrung/asr-studio:nightly",
    ],
)
def test_pull_mode_rejects_non_versioned_image_references(tmp_path, image):
    result = _run_start(tmp_path, COMMUNITY_1_FILES, args=("--pull",), image=image)

    assert result.returncode == 2
    assert "Pull mode" in result.stderr
    assert "pull " not in result.docker_log
    assert " version --json " not in result.docker_log


def test_pull_version_mismatch_stops_before_any_service_volume_is_mounted(tmp_path):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        args=("--pull",),
        image=image,
        version_matches=False,
    )

    assert result.returncode != 0
    assert "does not match source version 0.2.2 and schema 16" in result.stderr
    commands = result.docker_log.splitlines()
    assert f"pull {image}" in commands
    assert any("version --json --expect-version" in line for line in commands)
    assert not any(
        " compose " in f" {line} "
        and any(command in line for command in (" build", " run ", " up "))
        for line in commands
    )
    assert not any("asr_studio.cli model prepare" in line for line in commands)


@pytest.mark.parametrize(
    "args",
    [(), ("--source",), ("--pull",), ("--offline",)],
    ids=["default-source", "source", "pull", "offline"],
)
def test_image_revision_must_match_the_checkout_in_every_mode(tmp_path, args):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1" if args == ("--pull",) else None
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        args=args,
        image=image,
        image_revision="b" * 40,
        prepared_asr_model=True,
    )

    assert result.returncode != 0
    assert f"built from revision {'b' * 40}, but this checkout is {CHECKOUT_REVISION}" in (
        result.stderr
    )
    assert "Check out the matching tag" in result.stderr
    assert "asr_studio.cli model" not in result.docker_log
    assert " up " not in result.docker_log


def test_matching_image_revision_starts_without_a_warning(tmp_path):
    result = _run_start(tmp_path, COMMUNITY_1_FILES, args=("--offline",), prepared_asr_model=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cannot verify" not in result.stderr
    assert any(
        line.startswith("image inspect --format") and "org.opencontainers.image.revision" in line
        for line in result.docker_log.splitlines()
    )


@pytest.mark.parametrize(
    ("source_revision", "image_revision"),
    [(CHECKOUT_REVISION, "unknown"), (CHECKOUT_REVISION, ""), ("unknown", CHECKOUT_REVISION)],
    ids=["image-unknown", "image-unlabeled", "checkout-unknown"],
)
def test_unknown_revision_warns_with_guidance_and_continues(
    tmp_path, source_revision, image_revision
):
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        args=("--offline",),
        prepared_asr_model=True,
        source_revision=source_revision,
        image_revision=image_revision,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cannot verify that asr-studio:local was built from this checkout" in result.stderr
    assert f"Checkout revision: {source_revision}" in result.stderr
    assert "release tag" in result.stderr
    assert " up --pull never --no-build " in result.docker_log


def test_offline_start_leaves_the_only_service_that_can_download_stopped(tmp_path):
    offline = _run_start(
        tmp_path / "offline", COMMUNITY_1_FILES, args=("--offline",), prepared_asr_model=True
    )
    assert offline.returncode == 0, offline.stdout + offline.stderr
    assert " --scale model-installer=0 " in offline.docker_log
    source = _run_start(tmp_path / "source", COMMUNITY_1_FILES, prepared_asr_model=True)
    assert source.returncode == 0, source.stdout + source.stderr
    assert "--scale" not in source.docker_log


@pytest.mark.parametrize("through_symlink", [False, True], ids=["plain-path", "symlinked-path"])
def test_checkout_revision_comes_from_git_when_this_directory_is_the_repository_root(
    tmp_path, through_symlink
):
    checkout = _checkout_copy(tmp_path / "checkout")
    head = _commit_in_new_repository(checkout)
    if through_symlink:
        (tmp_path / "link").symlink_to(checkout)
        checkout = tmp_path / "link"

    result = _run_start(
        tmp_path / "run",
        COMMUNITY_1_FILES,
        args=("--offline",),
        prepared_asr_model=True,
        source_revision=None,
        image_revision=head,
        checkout=checkout,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cannot verify" not in result.stderr


def test_checkout_inside_another_repository_does_not_borrow_its_revision(tmp_path):
    outer_head = _commit_in_new_repository(tmp_path / "outer")
    checkout = _checkout_copy(tmp_path / "outer" / "asr-studio-archive")

    result = _run_start(
        tmp_path / "run",
        COMMUNITY_1_FILES,
        args=("--offline",),
        prepared_asr_model=True,
        source_revision=None,
        image_revision=CHECKOUT_REVISION,
        checkout=checkout,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Checkout revision: unknown" in result.stderr
    assert outer_head not in result.stderr


def test_offline_mode_uses_existing_image_and_models_without_preparation(tmp_path):
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        args=("--offline",),
        prepared_asr_model=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = result.docker_log.splitlines()
    assert "image inspect asr-studio:local" in commands
    assert any("version --json --expect-version" in line for line in commands)
    assert any("asr_studio.cli model check" in line for line in commands)
    assert any("asr_studio.cli runtime check" in line for line in commands)
    assert not any(line.startswith("pull ") for line in commands)
    assert not any(" compose " in f" {line} " and " build" in line for line in commands)
    assert not any("asr_studio.cli model prepare" in line for line in commands)
    assert any(" up --pull never --no-build " in f" {line} " for line in commands)
    assert all(
        "--pull never" in line
        for line in commands
        if " compose " in f" {line} " and " run " in line
    )


def test_offline_mode_fails_for_missing_image_or_unprepared_asr_without_fetching(tmp_path):
    missing_image = _run_start(
        tmp_path / "missing-image",
        COMMUNITY_1_FILES,
        args=("--offline",),
        image_present=False,
    )
    assert missing_image.returncode != 0
    assert "requires the image already present locally" in missing_image.stderr
    assert "image inspect asr-studio:local" in missing_image.docker_log
    assert " version --json " not in missing_image.docker_log
    assert not any(
        " compose " in f" {line} "
        and any(command in line for command in (" build", " run ", " up "))
        for line in missing_image.docker_log.splitlines()
    )

    missing_model = _run_start(
        tmp_path / "missing-model",
        COMMUNITY_1_FILES,
        args=("--offline",),
        model_check_ok=False,
    )
    assert missing_model.returncode != 0
    assert "asr_studio.cli model check" in missing_model.docker_log
    assert "asr_studio.cli model prepare" not in missing_model.docker_log
    assert "asr_studio.cli runtime check" not in missing_model.docker_log
    assert " up " not in missing_model.docker_log


def test_offline_mode_does_not_create_a_missing_model_directory(tmp_path):
    result = _run_start(tmp_path, args=("--offline",))

    assert result.returncode != 0
    assert (
        "startup_model_unavailable: offline mode requires prepared local models." in result.stderr
    )
    assert str(tmp_path) not in result.stderr
    assert not (tmp_path / "models").exists()
    assert result.docker_log


def test_nvidia_preflight_uses_selected_image_and_reports_actionable_failure(tmp_path):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    result = _run_start(
        tmp_path,
        COMMUNITY_1_FILES,
        args=("--pull",),
        image=image,
        device="cuda",
    )

    assert result.returncode != 0
    assert "Docker cannot expose it" in result.stderr
    assert "NVIDIA Container Toolkit" in result.stderr
    assert any(
        line.startswith(
            f"run --pull=never --rm --gpus all --network none --entrypoint python {image}"
        )
        for line in result.docker_log.splitlines()
    )
    assert "asr_studio.cli model prepare" not in result.docker_log
    assert " up " not in result.docker_log


def test_compose_image_resolution_must_be_shared_by_app_and_worker(tmp_path):
    result = _run_start(
        tmp_path,
        args=("--pull",),
        image="ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1",
        second_image="ghcr.io/nguyencanhtrung/asr-studio:v0.1.0",
    )

    assert result.returncode != 0
    assert "same single ASR_IMAGE" in result.stderr
    assert not any(line.startswith("pull ") for line in result.docker_log.splitlines())


def test_repository_checks_have_shell_syntax_and_pytest_timeout_guardrails():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    check_script = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    e2e_test = (ROOT / "tests" / "test_transcription_video_e2e.py").read_text(encoding="utf-8")
    pytest_config = project["tool"]["pytest"]["ini_options"]

    assert any(
        dependency.startswith("pytest-timeout")
        for dependency in project["dependency-groups"]["dev"]
    )
    assert pytest_config["timeout"] == 300
    assert pytest_config["timeout_method"] == "thread"
    assert "./scripts/check-shell-syntax.sh" in check_script
    assert "node --test 'tests/web/*.test.mjs'" in check_script
    # node --test passes when the glob matches nothing, so the gate needs real test files.
    assert sorted((ROOT / "tests" / "web").glob("*.test.mjs"))
    assert "pytest.mark.timeout(0)" in e2e_test


def test_shell_syntax_gate_propagates_a_script_failure(tmp_path):
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir()
    _write_executable(scripts_root / "valid.sh", "#!/bin/sh\necho valid\n")
    _write_executable(scripts_root / "broken.sh", "#!/bin/sh\necho 'unterminated\n")

    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "check-shell-syntax.sh"), str(scripts_root)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "syntax error" in result.stderr.casefold() or "unterminated" in result.stderr.casefold()


def test_platform_images_publish_only_on_version_tags_with_native_runners():
    jobs = load_yaml(PIPELINE_PATH)["jobs"]
    publish_platform = jobs["publish-platform"]
    expected_platforms = {
        "linux/amd64": {"runner": "ubuntu-24.04", "artifact": "amd64"},
        "linux/arm64": {"runner": "ubuntu-24.04-arm", "artifact": "arm64"},
    }

    assert publish_platform["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert publish_platform["needs"] == ["release-record"]
    assert jobs["release-record"]["needs"] == ["quality", "container"]
    assert publish_platform["timeout-minutes"] >= 60
    assert publish_platform["permissions"] == {"contents": "write", "packages": "write"}
    assert publish_platform["strategy"]["fail-fast"] is False
    matrix_entries = publish_platform["strategy"]["matrix"]["include"]
    assert {
        entry["platform"]: {"runner": entry["runner"], "artifact": entry["artifact"]}
        for entry in matrix_entries
    } == expected_platforms
    assert all("qemu" not in str(step).casefold() for step in publish_platform["steps"])

    build_step = next(
        step
        for step in publish_platform["steps"]
        if step["name"] == "Build and push platform image"
    )
    assert build_step["with"]["platforms"] == "${{ matrix.platform }}"
    assert build_step["with"]["build-args"] == "VCS_REF=${{ github.sha }}\n"
    assert "push-by-digest=true" in build_step["with"]["outputs"]
    assert "name-canonical=true" in build_step["with"]["outputs"]
    assert "push=true" in build_step["with"]["outputs"]
    assert "cache-to" not in build_step["with"]
    assert build_step["with"]["cache-from"] == "type=gha,scope=container"
    assert build_step["id"] == "publish"
    assert "metadata-file" not in build_step["with"]
    assert build_step["with"]["provenance"] == "mode=max"
    assert build_step["with"]["sbom"] is True

    candidate_step = next(
        step for step in publish_platform["steps"] if step["name"] == "Build platform candidate"
    )
    assert candidate_step["id"] == "candidate"
    assert "metadata-file" not in candidate_step["with"]

    source_step = next(
        step
        for step in publish_platform["steps"]
        if step["name"] == "Publish corresponding source before image"
    )
    assert "candidate_config_digest" in source_step["run"]
    assert source_step["env"]["CANDIDATE_METADATA"] == "${{ steps.candidate.outputs.metadata }}"
    assert '<<< "${CANDIDATE_METADATA}"' in source_step["run"]
    assert "--config-digest" in source_step["run"]
    assert "containerimage.config.digest" in source_step["run"]

    verify_step = next(
        step for step in publish_platform["steps"] if step["name"] == "Verify public platform image"
    )
    assert "scripts/resolve-image-config.py" in verify_step["run"]
    assert "published_config_digest" in verify_step["run"]
    assert "{{.Id}}" not in verify_step["run"]
    assert 'test "${published_config_digest}" = "${candidate_config_digest}"' in verify_step["run"]
    assert verify_step["env"]["CANDIDATE_METADATA"] == "${{ steps.candidate.outputs.metadata }}"
    assert verify_step["env"]["PUBLISHED_DIGEST"] == "${{ steps.publish.outputs.digest }}"
    assert verify_step["env"]["RELEASE_PLATFORM"] == "${{ matrix.artifact }}"
    assert "PUBLISHED_METADATA" not in verify_step["env"]
    assert "PUBLISHED_METADATA" not in verify_step["run"]
    assert '<<< "${CANDIDATE_METADATA}"' in verify_step["run"]

    export_digest = next(
        step for step in publish_platform["steps"] if step["name"] == "Export image digest"
    )
    assert '"${{ steps.publish.outputs.digest }}"' in export_digest["run"]
    assert 'touch "${RUNNER_TEMP}/digests/${digest#sha256:}"' in export_digest["run"]
    upload_digest = next(
        step for step in publish_platform["steps"] if step["name"] == "Upload image digest"
    )
    assert upload_digest["with"]["name"] == "digest-${{ matrix.artifact }}"
    assert upload_digest["with"]["if-no-files-found"] == "error"


def test_public_image_verification_run_block_is_valid_bash():
    steps = load_yaml(PIPELINE_PATH)["jobs"]["publish-platform"]["steps"]
    verify_step = next(step for step in steps if step["name"] == "Verify public platform image")
    result = subprocess.run(
        ["bash", "-n"],
        input=verify_step["run"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_manifest_publish_waits_for_platform_images_and_attests_manifest_digest():
    jobs = load_yaml(PIPELINE_PATH)["jobs"]
    publish_platform = jobs["publish-platform"]
    publish = jobs["publish"]
    version_tag_condition = "startsWith(github.ref, 'refs/tags/v')"

    assert publish_platform["if"] == version_tag_condition
    assert publish["if"] == version_tag_condition
    assert publish["needs"] == ["publish-platform"]
    assert publish["timeout-minutes"] >= 15
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "attestations": "write",
        "id-token": "write",
    }

    download_digests = next(
        step for step in publish["steps"] if step["name"] == "Download image digests"
    )
    assert download_digests["with"]["pattern"] == "digest-*"
    assert download_digests["with"]["merge-multiple"] is True

    manifest = next(
        step
        for step in publish["steps"]
        if step["name"] == "Create and publish multi-platform manifest"
    )
    assert manifest["id"] == "publish"
    assert manifest["working-directory"] == "${{ runner.temp }}/digests"
    assert manifest["env"]["IMAGE_TAGS_JSON"] == "${{ steps.metadata.outputs.json }}"
    assert "docker buildx imagetools create" in manifest["run"]
    assert '.tags | map("-t " + .)' in manifest["run"]
    assert "$(printf '${{ env.IMAGE_NAME }}@sha256:%s ' *)" in manifest["run"]
    assert "--metadata-file" in manifest["run"]
    assert 'echo "digest=${digest}" >> "${GITHUB_OUTPUT}"' in manifest["run"]

    attest = next(step for step in publish["steps"] if step["name"] == "Attest published container")
    assert attest["with"]["subject-name"] == "${{ env.IMAGE_NAME }}"
    assert attest["with"]["subject-digest"] == "${{ steps.publish.outputs.digest }}"
    assert attest["with"]["push-to-registry"] is True

    metadata = next(step for step in publish["steps"] if step.get("id") == "metadata")
    assert metadata["with"]["tags"].splitlines() == [
        "type=sha,format=long",
        "type=ref,event=tag",
        "type=semver,pattern={{version}}",
        "type=semver,pattern={{major}}.{{minor}}",
        "type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}",
    ]
    assert metadata["with"]["flavor"] == "latest=false"
    assert metadata["with"]["labels"] == "org.opencontainers.image.revision=${{ github.sha }}\n"


def test_every_action_is_pinned_to_an_immutable_commit():
    pipeline = load_yaml(PIPELINE_PATH)
    action_ref = re.compile(r"^[^@]+@[0-9a-f]{40}$")

    used_actions = [
        step["uses"] for job in pipeline["jobs"].values() for step in job["steps"] if "uses" in step
    ]

    assert used_actions
    assert all(action_ref.fullmatch(action) for action in used_actions)


def test_dependabot_pull_request_configuration_is_removed():
    assert not DEPENDABOT_PATH.exists()


LEGACY_VOLUMES = tuple(f"asr-en-vi_{v}" for v in ("data", "auth", "provider", "model-install"))


def _copied_volumes(log: str) -> list[str]:
    return [
        line.split(" -v ")[1].split(":")[0].removeprefix("asr-en-vi_")
        for line in log.splitlines()
        if "cp -a /from/. /to/" in line
    ]


def test_startup_copies_legacy_volumes_before_any_service_run(tmp_path):
    result = _run_start(tmp_path, COMMUNITY_1_FILES, existing_volumes=LEGACY_VOLUMES)

    assert result.returncode == 0, result.stdout + result.stderr
    commands = result.docker_log.splitlines()
    down = commands.index("compose -f compose.yaml -p asr-en-vi down")
    first_copy = next(i for i, line in enumerate(commands) if "cp -a /from/. /to/" in line)
    first_run = next(i for i, line in enumerate(commands) if " run --pull never " in line)
    assert down < first_copy < first_run
    assert _copied_volumes(result.docker_log) == ["auth", "provider", "model-install", "data"]
    assert all(
        f"-v asr-en-vi_{v}:/from:ro -v asr-studio_{v}:/to" in result.docker_log
        for v in ("data", "auth", "provider", "model-install")
    )
    assert "volume rm" not in result.docker_log
    assert "the old volumes are kept" in result.stdout


@pytest.mark.parametrize(
    ("project", "volumes"),
    [
        ("asr-studio", ()),
        ("asr-studio", (*LEGACY_VOLUMES, "asr-studio_data")),
        ("asr-en-vi", LEGACY_VOLUMES),
        ("custom", LEGACY_VOLUMES),
    ],
    ids=["fresh-install", "already-migrated", "legacy-project-name", "custom-project-name"],
)
def test_startup_skips_volume_migration_when_not_needed(tmp_path, project, volumes):
    result = _run_start(
        tmp_path, COMMUNITY_1_FILES, compose_project=project, existing_volumes=volumes
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert " down" not in result.docker_log
    assert "volume create" not in result.docker_log
    assert _copied_volumes(result.docker_log) == []


def test_failed_volume_copy_removes_new_volumes_and_stops(tmp_path):
    result = _run_start(
        tmp_path, COMMUNITY_1_FILES, existing_volumes=LEGACY_VOLUMES, copy_fail="asr-en-vi_provider"
    )

    assert result.returncode != 0
    assert "Could not copy volume asr-en-vi_provider" in result.stderr
    assert "volume rm asr-studio_auth asr-studio_provider" in result.docker_log
    assert not any(
        "volume create" in line and "asr-studio_data" in line
        for line in result.docker_log.splitlines()
    )
    assert " up " not in result.docker_log
