from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_compose_separates_services_volumes_and_loopback_publish():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    app = compose["services"]["app"]
    worker = compose["services"]["worker"]
    notes = compose["services"]["notes-service"]

    assert app["ports"] == ["127.0.0.1:${ASR_PORT:-8765}:8765"]
    assert "ports" not in worker
    assert "data:/var/lib/asr-studio/data" in app["volumes"]
    assert "data:/var/lib/asr-studio/data" in worker["volumes"]
    assert "auth:/var/lib/asr-studio/auth" in app["volumes"]
    assert all("auth:" not in mount for mount in worker["volumes"])
    assert "provider:/var/lib/asr-studio/provider" in app["volumes"]
    assert "provider:/var/lib/asr-studio/provider" in notes["volumes"]
    assert all("provider:" not in mount for mount in worker["volumes"])
    assert app["environment"]["ASR_PROVIDER_DIR"] == "/var/lib/asr-studio/provider"
    assert "ASR_PROVIDER_DIR" not in worker["environment"]
    assert notes["environment"]["ASR_PROVIDER_DIR"] == "/var/lib/asr-studio/provider"
    assert "--no-access-log" in app["command"]
    assert app["image"] == "${ASR_IMAGE:-asr-studio:local}"
    assert app["image"] == worker["image"]
    assert app["build"]["args"]["VCS_REF"] == "${ASR_SOURCE_REVISION:-unknown}"
    assert app["environment"]["ASR_MAX_UPLOAD_BYTES"] == "${ASR_MAX_UPLOAD_BYTES:-4294967296}"
    assert app["environment"]["ASR_UPLOAD_CHUNK_BYTES"] == ("${ASR_UPLOAD_CHUNK_BYTES:-67108864}")
    assert app["environment"]["ASR_MAX_RECORDING_SECONDS"] == (
        "${ASR_MAX_RECORDING_SECONDS:-14400}"
    )
    assert "ASR_MODEL_PATH" not in app["environment"]
    assert worker["environment"]["ASR_DEVICE"] == "${ASR_DEVICE:-auto}"
    assert worker["environment"]["ASR_GPU_MODEL_PATH"].endswith("qwen3-asr-1.7b}")
    assert worker["environment"]["ASR_CPU_MODEL_PATH"].endswith("whisper-small}")
    assert worker["environment"]["ASR_DIARIZATION_MODEL_PATH"].endswith(
        "speaker-diarization-community-1}"
    )
    assert app["environment"]["ASR_DIARIZATION_MODEL_PATH"].endswith(
        "speaker-diarization-community-1}"
    )
    assert "HF_TOKEN" not in app["environment"] and "HF_TOKEN" not in worker["environment"]
    for service in (app, worker, notes):
        assert service["environment"]["DO_NOT_TRACK"] == "1"
        assert service["environment"]["HF_HUB_DISABLE_TELEMETRY"] == "1"
        assert service["environment"]["PYANNOTE_METRICS_ENABLED"] == "0"
        assert service["environment"]["HF_HUB_OFFLINE"] == "1"
        assert service["environment"]["TRANSFORMERS_OFFLINE"] == "1"
    assert "gpus" not in worker and "gpus" not in notes
    gpu_override = yaml.safe_load((ROOT / "compose.gpu.yaml").read_text(encoding="utf-8"))
    assert gpu_override["services"]["worker"]["gpus"] == "all"
    assert any(mount.endswith(":/models:ro") for mount in app["volumes"])
    assert any(mount.endswith(":/models:ro") for mount in worker["volumes"])
    assert not any(":/models" in mount for mount in notes["volumes"])
    # Compose keeps the service's :ro mount over `run -v`; only an override file widens it.
    prepare = yaml.safe_load((ROOT / "compose.model-prepare.yaml").read_text(encoding="utf-8"))
    model_mount = next(m for m in worker["volumes"] if m.endswith(":/models:ro"))
    assert prepare["services"]["worker"]["volumes"] == [model_mount.removesuffix(":ro") + ":rw"]
    assert prepare["services"]["worker"]["networks"] == ["default"]
    assert app["healthcheck"]
    assert worker["healthcheck"]
    assert notes["healthcheck"]
    assert compose["networks"]["internal"]["internal"] is True
    assert worker["networks"] == ["internal"]
    assert notes["networks"] == ["default"]


def test_only_the_model_installer_has_the_network_and_a_writable_models_folder():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    installer = services["model-installer"]
    app, worker, notes = services["app"], services["worker"], services["notes-service"]

    # app and worker stay offline and read-only on /models.
    for service in (app, worker):
        assert service["environment"]["HF_HUB_OFFLINE"] == "1"
        assert service["environment"]["TRANSFORMERS_OFFLINE"] == "1"
        assert [m for m in service["volumes"] if ":/models" in m] == [
            "${ASR_MODEL_HOST_PATH:-./models}:/models:ro"
        ]
        assert "model-install:/var/lib/asr-studio/install" not in worker["volumes"]
    assert "model-install:/var/lib/asr-studio/install" in app["volumes"]
    assert "model-install:/var/lib/asr-studio/install" not in notes["volumes"]
    assert not any(":/models" in mount for mount in notes["volumes"])

    # The installer is the only writer of /models and sees none of the application's records.
    assert installer["image"] == app["image"]
    assert [m for m in installer["volumes"] if ":/models" in m] == [
        "${ASR_MODEL_HOST_PATH:-./models}:/models:rw"
    ]
    assert sorted(installer["volumes"]) == sorted(
        ["model-install:/var/lib/asr-studio/install", "${ASR_MODEL_HOST_PATH:-./models}:/models:rw"]
    )
    environment = installer["environment"]
    for name in ("ASR_DATA_DIR", "ASR_AUTH_DIR", "ASR_PROVIDER_DIR", "HF_TOKEN"):
        assert name not in environment
    assert environment["HF_HUB_OFFLINE"] == "0"
    assert environment["TRANSFORMERS_OFFLINE"] == "0"
    assert environment["HF_HOME"] == "/tmp/huggingface"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert name in environment
    assert installer["command"] == ["python", "-m", "asr_studio.model_installer"]
    assert installer["environment"]["HF_HUB_DISABLE_XET"] == "1"  # its cache would fill the tmpfs
    assert installer["user"] == "${ASR_MODEL_UID:-1000}:${ASR_MODEL_GID:-1000}"
    assert installer["read_only"] is True
    assert installer["cap_drop"] == ["ALL"]
    assert installer["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in installer and "gpus" not in installer
    assert "depends_on" not in installer
    assert set(compose["volumes"]) == {"data", "auth", "provider", "model-install"}
    # Nothing but the installer may name a writable /models.
    writers = [
        name
        for name, service in services.items()
        if any(m.endswith(":/models:rw") for m in service.get("volumes", []))
    ]
    assert writers == ["model-installer"]


def test_start_script_hands_the_installer_the_models_folder_owner():
    script = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")

    assert "ASR_MODEL_UID=${ASR_MODEL_UID:-$(id -u)}" in script
    assert "ASR_MODEL_GID=${ASR_MODEL_GID:-$(id -g)}" in script
    assert "export ASR_MODEL_UID ASR_MODEL_GID" in script
    assert script.index("export ASR_MODEL_UID") < script.index("compose_base config")


def test_install_volume_is_open_to_both_services_users():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "/var/lib/asr-studio/install" in dockerfile
    assert "chmod 0777 /var/lib/asr-studio/install" in dockerfile


def test_provider_volume_is_created_for_runtime_uid_and_default_server_suppresses_access_logs():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/var/lib/asr-studio/provider" in dockerfile
    assert (
        "chmod 0700 /var/lib/asr-studio/data /var/lib/asr-studio/auth /var/lib/asr-studio/provider"
        in dockerfile
    )
    assert '"--no-access-log"]' in dockerfile
    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "HF_HUB_DISABLE_TELEMETRY=1" in dockerfile
    assert "PYANNOTE_METRICS_ENABLED=0" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert 'LD_LIBRARY_PATH="/opt/asr-media/lib:${LD_LIBRARY_PATH}"' in dockerfile
    assert "ARG OMP_VERSION=18.5.0" in dockerfile
    assert "b8413e6e085a423c7a9b1d7bc025c2af794923b6b6c57330a309639b6176c8a1" in dockerfile
    assert "1244860c58bb68a3a45390d4932e5c7983183ec8c12cb6848c1afd709de38a07" in dockerfile
    assert "COPY --from=omp-bin /usr/local/bin/omp /usr/local/bin/omp" in dockerfile


def test_image_build_context_is_allowlisted_and_excludes_user_owned_files():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert dockerignore[0] == "**"
    assert "!pyproject.toml" in dockerignore
    assert "!README.md" in dockerignore
    assert "!uv.lock" in dockerignore
    assert "!src/asr_studio/**" in dockerignore
    assert "COPY ." not in dockerfile
    assert "COPY pyproject.toml README.md LICENSE uv.lock ./" in dockerfile
    assert {"!LICENSE"} <= set(dockerignore)
    assert "COPY LICENSE /usr/local/share/licenses/asr-studio/" in dockerfile
    assert 'org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"' in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04" in dockerfile
    assert "libpython3.12" in dockerfile
    assert "faster-whisper==1.2.1" in pyproject
    assert '"av>=11,<19"' in pyproject
    assert not (ROOT / "requirements-asr.txt").exists()
    assert "COPY src/ ./src/" in dockerfile
    assert "prototype" not in dockerfile
    assert "docs/" not in dockerfile
    assert "/models/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_torchcodec_uses_cuda128_wheels_only_on_linux_aarch64():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = project["tool"]["uv"]["sources"]

    assert sources["torch"][0] == {
        "index": "pytorch-cu128",
        "marker": "sys_platform == 'linux'",
    }
    assert sources["torchaudio"][0] == {
        "index": "pytorch-cu128",
        "marker": "sys_platform == 'linux'",
    }
    assert sources["torchcodec"][0] == {
        "index": "pytorch-cu128",
        "marker": "sys_platform == 'linux' and platform_machine == 'aarch64'",
    }

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    torchcodec_packages = [
        package for package in lock["package"] if package["name"] == "torchcodec"
    ]
    assert {package["source"]["registry"] for package in torchcodec_packages} == {
        "https://pypi.org/simple",
        "https://download.pytorch.org/whl/cu128",
    }
    cuda_package = next(
        package
        for package in torchcodec_packages
        if package["source"]["registry"] == "https://download.pytorch.org/whl/cu128"
    )
    pypi_package = next(
        package
        for package in torchcodec_packages
        if package["source"]["registry"] == "https://pypi.org/simple"
    )
    assert cuda_package["version"] == "0.11.1+cu128"
    assert cuda_package["resolution-markers"] == [
        "platform_machine == 'aarch64' and sys_platform == 'linux'"
    ]
    assert (
        "platform_machine != 'aarch64' and sys_platform == 'linux'"
        in pypi_package["resolution-markers"]
    )
    assert "cu13" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "cu13" not in (ROOT / "uv.lock").read_text(encoding="utf-8").lower()


def test_example_environment_selects_hardware_profiles_automatically():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "ASR_DEVICE=auto" in example.splitlines()
    assert "ASR_GPU_MODEL_PATH=/models/qwen3-asr-1.7b" in example.splitlines()
    assert "ASR_CPU_MODEL_PATH=/models/whisper-small" in example.splitlines()
    assert "ASR_GPU_COMPUTE_TYPE=float16" in example.splitlines()
    assert "ASR_CPU_COMPUTE_TYPE=int8" in example.splitlines()
    assert (
        "ASR_DIARIZATION_MODEL_PATH=/models/speaker-diarization-community-1" in example.splitlines()
    )


def test_status_strip_item_sizes_only_its_dot_and_runtime_details_have_ui_targets():
    stylesheet = (ROOT / "src/asr_studio/web/shell.css").read_text(encoding="utf-8")
    markup = (ROOT / "src/asr_studio/web/shell.html").read_text(encoding="utf-8")
    script = (ROOT / "src/asr_studio/web/history.js").read_text(encoding="utf-8")

    assert ".meeting-status-item > span:first-child" in stylesheet
    assert ".meeting-status-item > span," not in stylesheet
    assert 'id="runtime-backend"' in markup
    assert 'id="runtime-model"' in markup
    assert "#runtime-backend" in script
    assert "#runtime-model" in script


def test_language_choice_is_presented_as_a_hint_not_an_output_constraint():
    markup = (ROOT / "src/asr_studio/web/shell.html").read_text(encoding="utf-8")
    script = (ROOT / "src/asr_studio/web/history.js").read_text(encoding="utf-8")

    assert markup.count("Gợi ý ngôn ngữ") >= 3
    assert "Ngôn ngữ chính" not in markup
    assert 't("Gợi ý: {language}"' in script


def test_worker_grace_period_exceeds_runner_worst_case_stop_time():
    from asr_studio import worker as worker_module

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    grace = compose["services"]["worker"]["stop_grace_period"]
    worst_case = worker_module.RUNNER_TERM_GRACE_SECONDS + worker_module.RUNNER_KILL_GRACE_SECONDS

    assert grace.endswith("s")
    assert float(grace.removesuffix("s")) >= worst_case + 5


def test_start_preflight_maps_the_configured_container_diarization_path_to_the_host_mount():
    script = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")

    assert "ASR_DIARIZATION_MODEL_PATH=" in script
    assert "${diarization_container_path#/models/}" in script
    assert "ASR_DIARIZATION_MODEL_PATH must be below /models" in script
    assert "embedding/pytorch_model.bin" in script
    assert "plda/xvec_transform.npz" in script
    assert "segmentation/pytorch_model.bin" in script


def test_claimed_task_starting_stage_has_a_vietnamese_label():
    script = (ROOT / "src/asr_studio/web/history.js").read_text(encoding="utf-8")
    labels = script.split("const TASK_STAGES = {", 1)[1].split("};", 1)[0]

    assert '  starting: t("Đang bắt đầu"),' in labels
