from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "offline-smoke.py"
SPEC = importlib.util.spec_from_file_location("offline_smoke_harness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
offline_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = offline_smoke
SPEC.loader.exec_module(offline_smoke)


def test_browser_check_is_recorded_as_soon_as_it_completes():
    harness = object.__new__(offline_smoke.SmokeHarness)
    harness.evidence = {"checks": {}}
    harness.log_handle = io.StringIO()
    harness.redactor = offline_smoke.Redactor()
    browser_checks = {}

    harness._record_browser_check(browser_checks, "history_read")

    assert browser_checks == {"history_read": True}
    assert harness.evidence["checks"] == {"history_read": True}
    assert "browser check history_read: pass" in harness.log_handle.getvalue()


def test_locator_value_wait_polls_until_the_predicate_matches():
    class Locator:
        def __init__(self):
            self.values = iter((0, 1))
            self.calls = []

        def evaluate(self, expression, argument, *, timeout):
            self.calls.append((expression, argument, timeout))
            return next(self.values)

    class Page:
        def __init__(self):
            self.waits = []

        def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    locator = Locator()
    page = Page()
    result = offline_smoke.wait_for_locator_value(
        page,
        locator,
        "element => element.readyState",
        predicate=lambda value: value >= 1,
        timeout_ms=1_000,
        failure_message="fixed timeout",
    )

    assert result == 1
    assert len(locator.calls) == 2
    assert all(0 < call[2] <= 1_000 for call in locator.calls)
    assert page.waits and all(0 < timeout <= 50 for timeout in page.waits)


def test_locator_value_wait_raises_a_fixed_failure_at_its_deadline(monkeypatch):
    monotonic_values = iter((0.0, 0.0, 0.002, 0.01))
    monkeypatch.setattr(offline_smoke.time, "monotonic", lambda: next(monotonic_values))

    class Locator:
        def evaluate(self, _expression, _argument, *, timeout):
            assert 0 < timeout <= 10
            return False

    class Page:
        def wait_for_timeout(self, timeout):
            assert timeout == 8

    with pytest.raises(offline_smoke.SmokeFailure, match="^fixed timeout$"):
        offline_smoke.wait_for_locator_value(
            Page(),
            Locator(),
            "element => element.readyState",
            predicate=bool,
            timeout_ms=10,
            failure_message="fixed timeout",
        )


def test_offline_smoke_harness_avoids_csp_sensitive_wait_for_function():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for_function"
    ]

    locations = ", ".join(f"line {node.lineno}" for node in calls)
    assert not calls, (
        "scripts/offline-smoke.py must avoid CSP-sensitive wait_for_function calls; "
        f"found {locations}"
    )


def test_project_names_are_unique_and_prefix_is_bounded():
    first = offline_smoke.unique_project_name("asr-offline-smoke")
    second = offline_smoke.unique_project_name("asr-offline-smoke")

    assert first.startswith("asr-offline-smoke-")
    assert second.startswith("asr-offline-smoke-")
    assert first != second
    assert len(first) <= 44
    with pytest.raises(offline_smoke.SmokeFailure):
        offline_smoke.unique_project_name("Unsafe/Project")
    with pytest.raises(offline_smoke.SmokeFailure):
        offline_smoke.unique_project_name("other-project")


def test_isolated_compose_removes_ports_build_and_pull_policy_without_changing_volumes():
    original = {
        "services": {
            "app": {
                "image": "example.invalid/asr:test",
                "build": {"context": "."},
                "pull_policy": "never",
                "ports": ["127.0.0.1:8765:8765"],
                "volumes": ["project_data:/var/lib/asr-studio/data"],
            },
            "worker": {
                "image": "example.invalid/asr:test",
                "build": {"context": "."},
                "volumes": ["project_data:/var/lib/asr-studio/data"],
            },
        },
        "volumes": {"data": {"name": "project_data"}},
    }

    isolated = offline_smoke.isolated_compose_document(original, "asr-offline-smoke-a1b2c3d4")

    assert original["services"]["app"]["ports"]
    assert isolated["services"]["app"]["ports"] == []
    assert "build" not in isolated["services"]["app"]
    assert "build" not in isolated["services"]["worker"]
    assert "pull_policy" not in isolated["services"]["app"]
    assert isolated["networks"]["default"]["internal"] is True
    assert isolated["volumes"] == original["volumes"]
    assert isolated["services"]["app"]["volumes"] == original["services"]["app"]["volumes"]
    assert isolated["name"] == "asr-offline-smoke-a1b2c3d4"


def test_resolved_compose_uses_gpu_overlay_and_offline_config_keeps_device_request(
    monkeypatch, tmp_path
):
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    device_request = {"count": -1, "capabilities": ["gpu"]}
    resolved = {
        "services": {
            "app": {
                "image": options.image,
                "build": {"context": "."},
                "pull_policy": "never",
                "ports": ["127.0.0.1:8765:8765"],
                "volumes": ["auth:/var/lib/asr-studio/auth"],
                "environment": {"ASR_PUBLIC_URL": harness.base_url},
            },
            "worker": {
                "image": options.image,
                "build": {"context": "."},
                "volumes": ["data:/var/lib/asr-studio/data"],
                "environment": {"ASR_DEVICE": "cuda"},
                "gpus": [device_request],
            },
        },
        "volumes": {"auth": {"name": "smoke_auth"}, "data": {"name": "smoke_data"}},
        "networks": {"default": {"driver": "bridge"}},
    }
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        return offline_smoke.subprocess.CompletedProcess(command, 0, json.dumps(resolved), "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    isolated_path = harness._write_isolated_compose()
    try:
        file_args = [
            commands[0][index + 1] for index, value in enumerate(commands[0][:-1]) if value == "-f"
        ]
        assert file_args == [str(offline_smoke.COMPOSE_FILE), str(offline_smoke.GPU_COMPOSE_FILE)]
        isolated = json.loads(isolated_path.read_text(encoding="utf-8"))
        assert isolated["services"]["worker"]["gpus"] == [device_request]
        assert isolated["services"]["app"]["volumes"] == resolved["services"]["app"]["volumes"]
        assert (
            isolated["services"]["worker"]["volumes"] == resolved["services"]["worker"]["volumes"]
        )
        assert (
            isolated["services"]["app"]["environment"] == resolved["services"]["app"]["environment"]
        )
        assert (
            isolated["services"]["worker"]["environment"]
            == resolved["services"]["worker"]["environment"]
        )
        assert isolated["volumes"] == resolved["volumes"]

        harness.container_python("app", "print('{}')", label="default config exec")
        default_exec_files = [
            commands[1][index + 1] for index, value in enumerate(commands[1][:-1]) if value == "-f"
        ]
        assert default_exec_files == [
            str(offline_smoke.COMPOSE_FILE),
            str(offline_smoke.GPU_COMPOSE_FILE),
        ]
        harness.container_python(
            "app", "print('{}')", label="isolated config exec", config_path=isolated_path
        )
        isolated_exec_files = [
            commands[2][index + 1] for index, value in enumerate(commands[2][:-1]) if value == "-f"
        ]
        assert isolated_exec_files == [str(isolated_path)]
    finally:
        isolated_path.unlink(missing_ok=True)


def test_cleanup_ownership_requires_exact_project_labels():
    project = "asr-offline-smoke-a1b2c3d4"
    container = {"Config": {"Labels": {"com.docker.compose.project": project}}}
    network = {"Labels": {"com.docker.compose.project": project}}
    foreign = {"Labels": {"com.docker.compose.project": "asr-studio"}}

    assert offline_smoke.owned_compose_resources(project, [container], [network]) is True
    assert offline_smoke.owned_compose_resources(project, [container, foreign], [network]) is False


def test_project_collision_preflight_uses_read_only_command_doubles(monkeypatch, tmp_path):
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        return offline_smoke.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    harness._assert_project_unused()

    assert [command[1:3] for command in commands] == [
        ["ps", "--all"],
        ["network", "ls"],
        ["volume", "ls"],
    ]
    assert all(
        f"label=com.docker.compose.project={harness.project}" in command for command in commands
    )


def test_project_collision_preflight_stops_before_any_mutation(monkeypatch, tmp_path):
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        output = "existing-volume-id\n" if command[1:3] == ["volume", "ls"] else ""
        return offline_smoke.subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    with pytest.raises(offline_smoke.SmokeFailure, match="already owns Docker volumes"):
        harness._assert_project_unused()

    assert len(commands) == 3
    assert all(command[1] in {"ps", "network", "volume"} for command in commands)


def test_source_or_pull_refuses_to_update_an_image_used_by_a_container(monkeypatch, tmp_path):
    options = offline_smoke.SmokeOptions(
        image="ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="pull",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        return offline_smoke.subprocess.CompletedProcess(command, 0, "container-id\n", "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    with pytest.raises(offline_smoke.SmokeFailure, match="referenced by an existing container"):
        harness._assert_image_not_in_use()

    assert len(commands) == 1
    assert commands[0][1:3] == ["ps", "--all"]
    assert "ancestor=ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1" in commands[0]


def _embedded_container_scripts():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    scripts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id in {"script", "probe"}
            for target in node.targets
        ):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            scripts.append(value.value)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_provider_script"
            and isinstance(value.args[0], ast.Constant)
        ):
            scripts.append(offline_smoke._provider_script(value.args[0].value))
    return scripts


def test_embedded_container_python_snippets_compile():
    snippets = _embedded_container_scripts()

    assert len(snippets) >= 4
    for snippet in snippets:
        compile(snippet, "<offline-smoke-container-script>", "exec")


def test_provider_scripts_share_one_bootstrap_with_the_provider_dir_guard():
    provider_scripts = [
        snippet for snippet in _embedded_container_scripts() if "provider.ensure()" in snippet
    ]

    assert len(provider_scripts) == 2
    for snippet in provider_scripts:
        assert snippet.startswith(offline_smoke._PROVIDER_PRELUDE)
        assert "if settings.provider_dir is None:" in snippet


def test_redactor_removes_known_credentials_and_access_fragments():
    redactor = offline_smoke.Redactor()
    redactor.add("synthetic-private-session-cookie")

    result = redactor.clean(
        "Cookie: synthetic-private-session-cookie "
        "/access#another-session-value-12345678901234567890"
    )

    assert "synthetic-private-session-cookie" not in result
    assert "another-session-value" not in result
    assert "[REDACTED]" in result


def test_failed_non_sensitive_command_logs_bounded_redacted_stderr_without_stdout(
    monkeypatch, tmp_path
):
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    harness.log_handle = io.StringIO()
    secret = "synthetic-private-diagnostic-token"
    harness.redactor.add(secret)
    stderr = f"Authorization: Bearer {secret}\ncompose failure: " + ("x" * 1800)

    def command_double(command, **_kwargs):
        return offline_smoke.subprocess.CompletedProcess(
            command, 1, "private auth transcript stdout", stderr
        )

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    with pytest.raises(offline_smoke.SmokeFailure, match="exited with status 1"):
        harness.run_command(["docker", "compose", "up"], label="force-recreate")

    log = harness.log_handle.getvalue()
    assert "compose failure" in log
    assert "[REDACTED]" in log
    assert secret not in log
    assert "private auth transcript stdout" not in log
    diagnostic = next(
        line.split("stderr: ", 1)[1] for line in log.splitlines() if "stderr: " in line
    )
    assert len(diagnostic) <= offline_smoke.MAX_STDERR_DIAGNOSTIC_CHARS
    assert diagnostic.endswith("… [truncated]")


def test_failed_sensitive_command_keeps_output_suppressed(monkeypatch, tmp_path):
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    harness.log_handle = io.StringIO()
    sensitive = "secret fixture and transcript data"

    def command_double(command, **_kwargs):
        return offline_smoke.subprocess.CompletedProcess(command, 1, sensitive, sensitive)

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    with pytest.raises(offline_smoke.SmokeFailure, match="exited with status 1"):
        harness.run_command(
            ["docker", "compose", "exec"], label="sensitive fixture read", sensitive_output=True
        )

    log = harness.log_handle.getvalue()
    assert "output suppressed" in log
    assert "stderr:" not in log
    assert sensitive not in log


def _valid_connected_provider_fixture():
    pin = {
        "provider": "chatgpt",
        "connection_version": 17,
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
    }
    return {
        "connection_status": "connected",
        "provider_view_status": "connected",
        "fixture_session_status": "connected",
        "client_id_valid": True,
        "direct_scope_present": True,
        "token_expiry_valid": True,
        "access_token_sha256": "a" * 64,
        "refresh_token_sha256": "b" * 64,
        "state_sha256": "c" * 64,
        "state_bytes": 400,
        "connection_version": 17,
        "selected_model": "gpt-6.1-sol",
        "selected_reasoning_effort": "high",
        "notes_task_pin": pin,
    }


def test_connected_provider_evidence_accepts_complete_local_fixture():
    fixture = _valid_connected_provider_fixture()
    assert offline_smoke.validate_connected_provider_fixture(fixture) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("connection_status", "reconnect_required"), ("direct_scope_present", False)],
)
def test_connected_provider_evidence_rejects_reconnect_required_or_missing_scope(field, value):
    fixture = _valid_connected_provider_fixture()
    fixture[field] = value

    with pytest.raises(
        offline_smoke.SmokeFailure,
        match="does not prove a valid connected provider session",
    ):
        offline_smoke.validate_connected_provider_fixture(fixture)


def test_seed_provider_fixture_executes_seed_and_reopen_scripts_locally(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    auth_dir = tmp_path / "auth"
    provider_dir = tmp_path / "provider"
    for directory in (data_dir, auth_dir, provider_dir):
        directory.mkdir()

    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )
    harness = offline_smoke.SmokeHarness(options)
    script_runs = []

    def run_locally(service, script, *, label, env_values=None, timeout=60, **_kwargs):
        assert service == "app"
        script_runs.append(label)
        environment = {
            "PATH": offline_smoke.os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT / "src"),
            "ASR_DATA_DIR": str(data_dir),
            "ASR_AUTH_DIR": str(auth_dir),
            "ASR_PROVIDER_DIR": str(provider_dir),
            "ASR_PUBLIC_URL": "http://127.0.0.1:8765",
        }
        environment.update(env_values or {})
        result = offline_smoke.subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert isinstance(value, dict)
        return value

    monkeypatch.setattr(harness, "container_python", run_locally)

    fixture = harness._seed_provider_fixture()

    assert script_runs == [
        "seed valid synthetic connected provider session",
        "verify local connected provider session and credentials",
    ]
    assert fixture["connection_status"] == "connected"
    assert fixture["provider_view_status"] == "connected"
    assert fixture["fixture_session_status"] == "connected"
    assert fixture["notes_task_pin"] == {
        "provider": "chatgpt",
        "connection_version": 17,
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
    }


def _write_prepared_models(root: Path) -> None:
    asr = root / offline_smoke.ASR_MODEL_NAME
    diarization = root / offline_smoke.DIARIZATION_MODEL_NAME
    for relative in offline_smoke.ASR_REQUIRED_FILES:
        path = asr / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "config.json":
            path.write_text(json.dumps({"model_type": "qwen3_asr"}), encoding="utf-8")
        elif relative == "processor_config.json":
            path.write_text(
                json.dumps({"feature_extractor": {"sampling_rate": 16000}}),
                encoding="utf-8",
            )
        else:
            path.write_bytes(b"prepared-asr-model-artifact")
    for relative in offline_smoke.DIARIZATION_REQUIRED_FILES:
        path = diarization / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"prepared-community-model-artifact")


def _write_short_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 48_000)


def test_input_validation_requires_prepared_models_and_short_local_wav(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    _write_prepared_models(models)
    audio = tmp_path / "synthetic.wav"
    _write_short_wav(audio)
    options = offline_smoke.SmokeOptions(
        image="asr-studio:local",
        models=models,
        audio=audio,
        output=tmp_path / "output",
        mode="offline",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )

    asr_dir, diarization_dir, duration_ms, audio_digest = offline_smoke.validate_inputs(options)

    assert asr_dir == models / offline_smoke.ASR_MODEL_NAME
    assert diarization_dir == models / offline_smoke.DIARIZATION_MODEL_NAME
    assert duration_ms == 3000
    assert len(audio_digest) == 64
    assert offline_smoke._artifact_identity(asr_dir)["file_count"] == len(
        offline_smoke.ASR_REQUIRED_FILES
    )


def test_pull_mode_rejects_nonpublic_registry_reference(tmp_path):
    options = offline_smoke.SmokeOptions(
        image="registry.example.invalid/asr-studio:v0.1.0-rc.1",
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode="pull",
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )

    with pytest.raises(offline_smoke.SmokeFailure, match="public ghcr.io"):
        offline_smoke.validate_inputs(options)


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/nguyencanhtrung/asr-studiofoobar:v0.1.0-rc.1",
        "ghcr.io/nguyencanhtrung/asr-studio:latest",
        "ghcr.io/nguyencanhtrung/asr-studio@sha256:abcd",
    ],
)
def test_pull_mode_rejects_noncanonical_public_refs(image):
    with pytest.raises(offline_smoke.SmokeFailure):
        offline_smoke.validate_pull_image(image)


def test_pull_mode_accepts_versioned_public_tag_and_sha256_digest():
    offline_smoke.validate_pull_image("ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1")
    offline_smoke.validate_pull_image("ghcr.io/nguyencanhtrung/asr-studio@sha256:" + "a" * 64)


def _smoke_options(tmp_path, mode="offline", image="asr-studio:local"):
    return offline_smoke.SmokeOptions(
        image=image,
        models=tmp_path / "models",
        audio=tmp_path / "synthetic.wav",
        output=tmp_path / "output",
        mode=mode,
        project_prefix="asr-offline-smoke",
        task_timeout=60,
        startup_timeout=30,
    )


def test_host_environment_does_not_carry_ineffective_telemetry_keys(tmp_path):
    environment = offline_smoke.SmokeHarness(_smoke_options(tmp_path))._environment()

    for key in (
        "PYANNOTE_METRICS_ENABLED",
        "DO_NOT_TRACK",
        "HF_HUB_DISABLE_TELEMETRY",
        "HF_HUB_DISABLE_PROGRESS_BARS",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    ):
        assert environment.get(key) == os.environ.get(key)
    assert environment["ASR_DEVICE"] == "cuda"


def test_shipped_smoke_uses_stable_tool_names_not_pipeline_task_names(tmp_path):
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path))
    harness.run_dir = tmp_path / "run"
    harness.run_dir.mkdir()
    harness.write_runtime_report()

    assert harness.evidence["tool"] == "asr-studio offline smoke"
    assert "task" not in harness.evidence
    assert (harness.run_dir / "smoke-report.md").is_file()
    assert not (harness.run_dir / "report-smoke-harness.md").exists()


def test_revision_parity_distinguishes_match_mismatch_and_unverified():
    commit = "a" * 40

    assert offline_smoke.revision_parity(commit, commit) == "match"
    assert offline_smoke.revision_parity(commit, "b" * 40) == "mismatch"
    assert offline_smoke.revision_parity(commit, "unknown") == "unverified"
    assert offline_smoke.revision_parity(commit, None) == "unverified"
    assert offline_smoke.revision_parity(commit, "") == "unverified"


def test_image_identity_records_the_revision_label(monkeypatch, tmp_path):
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path))
    inspected = [
        {
            "Id": "sha256:image",
            "RepoDigests": [],
            "Config": {"Labels": {offline_smoke.IMAGE_REVISION_LABEL: "c" * 40}},
        }
    ]
    monkeypatch.setattr(
        offline_smoke.subprocess,
        "run",
        lambda command, **_kwargs: offline_smoke.subprocess.CompletedProcess(
            command, 0, json.dumps(inspected), ""
        ),
    )

    assert harness._image_identity()["revision"] == "c" * 40


# Docker CLI facts behind these doubles: `docker context inspect` without a name inspects the
# current context (docker/cli v27, cli/command/context/inspect.go), and the JSON it prints carries
# Endpoints.docker.Host (https://docs.docker.com/reference/cli/docker/context/inspect/).
CONTEXT_INSPECT = ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
CONTEXT_ENDPOINT = "unix:///run/user/1000/docker.sock"


def test_pull_mode_pulls_anonymously_with_an_empty_docker_config_before_the_installer(
    monkeypatch, tmp_path
):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    ambient = tmp_path / "ambient-docker-config"
    monkeypatch.setenv("DOCKER_CONFIG", str(ambient))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path, "pull", image))
    calls = []

    def command_double(command, **kwargs):
        env = kwargs.get("env") or os.environ
        entry = {
            "command": command,
            "docker_config": env.get("DOCKER_CONFIG"),
            "docker_host": env.get("DOCKER_HOST"),
        }
        if command[:2] == ["docker", "pull"]:
            entry["config_entries"] = os.listdir(env["DOCKER_CONFIG"])
        calls.append(entry)
        stdout = f"{CONTEXT_ENDPOINT}\n" if command == CONTEXT_INSPECT else ""
        return offline_smoke.subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    harness._start_installation()

    context = next(call for call in calls if call["command"] == CONTEXT_INSPECT)
    pull = next(call for call in calls if call["command"][:2] == ["docker", "pull"])
    installer = next(call for call in calls if call["command"][0].endswith("start.sh"))
    assert calls.index(context) < calls.index(pull) < calls.index(installer)
    # The endpoint is read with the ambient config and carried to the pull alone.
    assert context["docker_config"] == str(ambient)
    assert context["docker_host"] is None
    assert pull["command"] == ["docker", "pull", image]
    assert pull["docker_config"] != str(ambient)
    assert pull["config_entries"] == []
    assert not Path(pull["docker_config"]).exists()
    assert pull["docker_host"] == CONTEXT_ENDPOINT
    # The installer and Compose keep the ambient config, so contexts and CLI plugins still resolve.
    assert installer["docker_config"] == str(ambient)
    assert installer["docker_host"] is None
    assert harness.evidence["anonymous_pull"]["docker_config"] == "fresh empty directory"
    assert harness.evidence["checks"]["anonymous_pull"] == "pass"


@pytest.mark.parametrize(
    ("explicit_host", "inspect_result", "expected_host", "reads_context"),
    [
        (
            "tcp://docker.example.invalid:2375",
            (0, f"{CONTEXT_ENDPOINT}\n"),
            "tcp://docker.example.invalid:2375",
            False,
        ),
        (None, (1, ""), None, True),
        (None, (0, "\n"), None, True),
    ],
    ids=["explicit-host-kept", "context-unreadable", "context-without-endpoint"],
)
def test_anonymous_pull_sets_docker_host_only_from_a_readable_context_endpoint(
    monkeypatch, tmp_path, explicit_host, inspect_result, expected_host, reads_context
):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    if explicit_host is None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
    else:
        monkeypatch.setenv("DOCKER_HOST", explicit_host)
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path, "pull", image))
    calls = []

    def command_double(command, **kwargs):
        calls.append((command, kwargs["env"].get("DOCKER_HOST")))
        if command == CONTEXT_INSPECT:
            return offline_smoke.subprocess.CompletedProcess(
                command, inspect_result[0], inspect_result[1], ""
            )
        return offline_smoke.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    harness._anonymous_pull()

    assert [command for command, _host in calls].count(CONTEXT_INSPECT) == int(reads_context)
    assert calls[-1] == (["docker", "pull", image], expected_host)


def test_anonymous_pull_failure_names_docker_host_logs_redacted_stderr_and_stops(
    monkeypatch, tmp_path
):
    image = "ghcr.io/nguyencanhtrung/asr-studio:v0.1.0-rc.1"
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path, "pull", image))
    harness.log_handle = io.StringIO()
    secret = "synthetic-daemon-credential"
    harness.redactor.add(secret)
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["docker", "pull"]:
            stderr = f"Cannot connect to the Docker daemon ({secret})"
            return offline_smoke.subprocess.CompletedProcess(command, 1, "", stderr)
        return offline_smoke.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    with pytest.raises(offline_smoke.SmokeFailure) as failure:
        harness._start_installation()

    message = str(failure.value)
    assert "set DOCKER_HOST" in message
    assert "smoke.log" in message
    assert "must be public" not in message
    log = harness.log_handle.getvalue()
    assert "Cannot connect to the Docker daemon" in log
    assert secret not in log
    assert not any(command[0].endswith("start.sh") for command in commands)
    assert "anonymous_pull" not in harness.evidence


@pytest.mark.parametrize("mode", ["source", "offline"])
def test_source_and_offline_modes_do_not_run_the_anonymous_pull(monkeypatch, tmp_path, mode):
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path, mode))
    commands = []

    def command_double(command, **_kwargs):
        commands.append(command)
        return offline_smoke.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(offline_smoke.subprocess, "run", command_double)
    harness._start_installation()

    assert not any(command[:2] == ["docker", "pull"] for command in commands)
    assert "anonymous_pull" not in harness.evidence


class _FakeNotesApi:
    def __init__(self, after, providers=None):
        self.after = after
        self.providers = providers or {"providers": [{"id": "chatgpt", "status": "connected"}]}

    def json(self, _method, path, **_kwargs):
        if path == "/api/providers":
            return self.providers
        return self.after


def _notes_state(task_id, status="succeeded", error_code=None, revisions=("note-1",)):
    return {
        "revisions": [{"id": item, "markdown": "saved", "edit_version": 1} for item in revisions],
        "task": {"id": task_id, "status": status, "error_code": error_code},
        "source_locked": False,
    }


def _offline_outcome(tmp_path, after, providers=None):
    harness = offline_smoke.SmokeHarness(_smoke_options(tmp_path))
    before = _notes_state("seed-task")
    harness.api = _FakeNotesApi(after, providers)
    harness._assert_offline_notes_outcome("meeting", before)
    return harness


def test_offline_notes_outcome_accepts_a_failed_provider_unavailable_task(tmp_path):
    harness = _offline_outcome(tmp_path, _notes_state("new", "failed", "provider_unavailable"))

    assert harness.evidence["offline_notes_attempt"] == {
        "provider_status": "connected",
        "run_accepted_status": 202,
        "task_status": "failed",
        "task_error_code": "provider_unavailable",
        "saved_notes_unchanged": True,
        "source_locked": False,
        "external_call": "none; container egress denied",
    }


@pytest.mark.parametrize(
    ("after", "providers", "message"),
    [
        (_notes_state("seed-task"), None, "did not create a task"),
        (_notes_state("new", "running"), None, "ended running"),
        (_notes_state("new", "failed", "provider_timeout"), None, "provider_timeout"),
        (_notes_state("new", "succeeded"), None, "ended succeeded"),
        (
            _notes_state("new", "failed", "provider_unavailable", ("note-1", "note-2")),
            None,
            "changed the saved notes",
        ),
        (
            _notes_state("new", "failed", "provider_unavailable"),
            {"providers": [{"id": "chatgpt", "status": "reconnect_required"}]},
            "disconnected the persisted provider",
        ),
        (
            {**_notes_state("new", "failed", "provider_unavailable"), "source_locked": True},
            None,
            "still holds the notes source",
        ),
    ],
)
def test_offline_notes_outcome_rejects_any_other_result(tmp_path, after, providers, message):
    with pytest.raises(offline_smoke.SmokeFailure, match=message):
        _offline_outcome(tmp_path, after, providers)
