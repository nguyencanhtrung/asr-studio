#!/usr/bin/env python3
# ruff: noqa: E501 -- embedded Python, JavaScript and Markdown snippets keep their local formatting.
"""Run an opt-in, isolated Linux/NVIDIA acceptance smoke for a local ASR installation.

This command owns only a uniquely named Compose project. It requires prepared local models and a
short authorized WAV; it never downloads models, installs host packages, configures the Docker
daemon, or touches another Compose project. Real source/pull/offline acceptance requires running
this command on the target Linux/NVIDIA host.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"
GPU_COMPOSE_FILE = ROOT / "compose.gpu.yaml"
COMPOSE_FILES = (COMPOSE_FILE, GPU_COMPOSE_FILE)
DEFAULT_PROJECT_PREFIX = "asr-offline-smoke"
PUBLIC_IMAGE = "ghcr.io/nguyencanhtrung/asr-studio"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_STDERR_DIAGNOSTIC_CHARS = 1200
OFFLINE_NOTES_STATUS_TEXT = "tạm thời không khả dụng"
OFFLINE_NOTES_TIMEOUT_MS = 120_000
MIN_AUDIO_SECONDS = 2
ASR_MODEL_NAME = "qwen3-asr-1.7b"
DIARIZATION_MODEL_NAME = "speaker-diarization-community-1"
ASR_REQUIRED_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "processor_config.json",
    "chat_template.jinja",
)
DIARIZATION_REQUIRED_FILES = (
    "config.yaml",
    "embedding/pytorch_model.bin",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
    "segmentation/pytorch_model.bin",
)
IMAGE_REVISION_LABEL = "org.opencontainers.image.revision"
ACCESS_LINK_RE = re.compile(r"/access#([A-Za-z0-9_-]{40,512})")
SAFE_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,34}\Z")


# One bootstrap for every container script that opens the provider store without network access.
_PROVIDER_PRELUDE = r"""import json, time
from asr_studio.chatgpt import ChatGPTProvider, DIRECT_SCOPE
from asr_studio.config import Settings

class FailOnNetworkAdapter:
    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise RuntimeError("unexpected provider adapter operation: " + name)
        return fail

settings = Settings.from_env()
if settings.provider_dir is None:
    raise RuntimeError("provider directory unavailable")
provider = ChatGPTProvider(settings.provider_dir,
    callback_url=settings.chatgpt_callback_url or "", adapter=FailOnNetworkAdapter())
provider.ensure()
"""


def _provider_script(body: str) -> str:
    return _PROVIDER_PRELUDE + body


class SmokeFailure(RuntimeError):
    """A bounded, user-actionable smoke failure without private command output."""


@dataclass(frozen=True, slots=True)
class SmokeOptions:
    image: str
    models: Path
    audio: Path
    output: Path
    mode: str
    project_prefix: str
    task_timeout: int
    startup_timeout: int
    state_ref: str | None = None


class Redactor:
    """Remove known secrets and local paths before any subprocess text reaches a log."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self._values.add(value)

    def clean(self, value: str) -> str:
        result = value
        for secret in sorted(self._values, key=len, reverse=True):
            result = result.replace(secret, "[REDACTED]")
        result = re.sub(
            r"(?i)(authorization|cookie|access[_-]?token|refresh[_-]?token|token)"
            r"(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            result,
        )
        result = re.sub(r"(?<=/access#)[^\s\"']+", "[REDACTED]", result)
        return result


def validate_connected_provider_fixture(fixture: dict[str, Any]) -> None:
    """Reject synthetic provider state that cannot prove a valid local connection."""
    pin = fixture.get("notes_task_pin")
    connection_version = fixture.get("connection_version")
    sha256_fields = (
        "access_token_sha256",
        "refresh_token_sha256",
        "state_sha256",
    )
    if (
        fixture.get("connection_status") != "connected"
        or fixture.get("provider_view_status") != "connected"
        or fixture.get("fixture_session_status") != "connected"
        or fixture.get("client_id_valid") is not True
        or fixture.get("direct_scope_present") is not True
        or fixture.get("token_expiry_valid") is not True
        or isinstance(connection_version, bool)
        or not isinstance(connection_version, int)
        or connection_version < 0
        or isinstance(fixture.get("state_bytes"), bool)
        or not isinstance(fixture.get("state_bytes"), int)
        or fixture["state_bytes"] <= 0
        or not isinstance(pin, dict)
        or pin.get("provider") != "chatgpt"
        or pin.get("connection_version") != connection_version
        or pin.get("model") != fixture.get("selected_model")
        or pin.get("reasoning_effort") != fixture.get("selected_reasoning_effort")
        or not isinstance(fixture.get("selected_model"), str)
        or not fixture["selected_model"]
        or not isinstance(fixture.get("selected_reasoning_effort"), str)
        or not fixture["selected_reasoning_effort"]
        or any(
            not isinstance(fixture.get(field), str)
            or not re.fullmatch(r"[0-9a-f]{64}", fixture[field])
            for field in sha256_fields
        )
    ):
        raise SmokeFailure(
            "Synthetic ChatGPT fixture does not prove a valid connected provider session."
        )


def _compose_file_arguments(config_path: Path | None) -> list[str]:
    files = (config_path,) if config_path is not None else COMPOSE_FILES
    return [argument for path in files for argument in ("-f", str(path))]


def unique_project_name(prefix: str = DEFAULT_PROJECT_PREFIX) -> str:
    if not SAFE_PROJECT_RE.fullmatch(prefix) or not prefix.startswith(DEFAULT_PROJECT_PREFIX):
        raise SmokeFailure(
            "Project prefix must start with 'asr-offline-smoke' and use safe lowercase characters."
        )
    return f"{prefix}-{secrets.token_hex(4)}"


def isolated_compose_document(document: dict[str, Any], project: str) -> dict[str, Any]:
    """Return a resolved Compose document isolated from egress and published ports."""
    result = json.loads(json.dumps(document))
    services = result.get("services")
    if not isinstance(services, dict) or not {"app", "worker"} <= set(services):
        raise SmokeFailure("Resolved Compose config must contain app and worker services.")
    for service_name in ("app", "worker"):
        service = services[service_name]
        if not isinstance(service, dict) or not service.get("image"):
            raise SmokeFailure("Resolved app and worker services must use the selected image.")
        service.pop("build", None)
        service.pop("pull_policy", None)
    services["app"]["ports"] = []
    networks = result.setdefault("networks", {})
    default_network = networks.get("default", {})
    if not isinstance(default_network, dict) or default_network.get("external"):
        raise SmokeFailure("The resolved default network cannot be safely isolated.")
    default_network["name"] = f"{project}_default"
    default_network["internal"] = True
    networks["default"] = default_network
    result["name"] = project
    return result


def owned_compose_resources(
    project: str, containers: list[dict[str, Any]], networks: list[dict[str, Any]]
) -> bool:
    """Accept cleanup only when every discovered resource carries our exact project label."""
    for resource in [*containers, *networks]:
        labels = resource.get("Config", {}).get("Labels") or resource.get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            return False
    return True


def revision_parity(source_commit: str, image_revision: str | None) -> str:
    """Compare the checkout revision with the image revision label."""
    if not image_revision or image_revision == "unknown":
        return "unverified"
    return "match" if image_revision == source_commit else "mismatch"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_identity(directory: Path) -> dict[str, Any]:
    paths = sorted(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise SmokeFailure("Model directories must not contain symlinks.")
    files = [path for path in paths if path.is_file()]
    manifest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        size = path.stat().st_size
        total_bytes += size
        relative = path.relative_to(directory).as_posix()
        manifest.update(f"{relative}\0{size}\0{_sha256_file(path)}\n".encode())
    if not files:
        raise SmokeFailure("A prepared model directory is empty.")
    return {"file_count": len(files), "bytes": total_bytes, "sha256": manifest.hexdigest()}


def _check_model_files(directory: Path, required: tuple[str, ...], label: str) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise SmokeFailure(f"Prepared {label} model directory must be a regular directory.")
    for relative in required:
        path = directory / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise SmokeFailure(f"Prepared {label} model is missing a required local artifact.")
        with path.open("rb") as source:
            prefix = source.read(48)
        if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise SmokeFailure(f"Prepared {label} model contains an unresolved Git LFS pointer.")


def _check_asr_model_configuration(directory: Path) -> None:
    try:
        model = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        processor = json.loads((directory / "processor_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeFailure(
            "Prepared ASR model configuration is invalid; no download was attempted."
        ) from None
    if not isinstance(model, dict) or model.get("model_type") != "qwen3_asr":
        raise SmokeFailure("Prepared ASR model is not Qwen3-ASR; no download was attempted.")
    feature_extractor = processor.get("feature_extractor") if isinstance(processor, dict) else None
    if not isinstance(feature_extractor, dict):
        raise SmokeFailure(
            "Prepared ASR processor config is incomplete; no download was attempted."
        )
    if feature_extractor.get("sampling_rate") != 16000:
        raise SmokeFailure("Prepared ASR model must use 16 kHz audio; no download was attempted.")


def _is_public_pull_image(image: str) -> bool:
    if image.startswith(f"{PUBLIC_IMAGE}:v"):
        tag = image[len(PUBLIC_IMAGE) + 1 :]
        return re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", tag) is not None
    digest_prefix = f"{PUBLIC_IMAGE}@sha256:"
    if image.startswith(digest_prefix):
        return re.fullmatch(r"[0-9a-f]{64}", image[len(digest_prefix) :]) is not None
    return False


def validate_pull_image(image: str) -> None:
    if not _is_public_pull_image(image):
        raise SmokeFailure(
            "Pull mode accepts only a versioned or sha256-pinned public "
            "ghcr.io/nguyencanhtrung/asr-studio image."
        )


def validate_inputs(options: SmokeOptions) -> tuple[Path, Path, int, str]:
    if options.mode not in {"source", "pull", "offline"}:
        raise SmokeFailure("Choose one startup mode: source, pull or offline.")
    if not options.image or any(character.isspace() for character in options.image):
        raise SmokeFailure("Provide an explicit Docker image reference without whitespace.")
    if "://" in options.image or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/@-]{1,254}", options.image
    ):
        raise SmokeFailure("Image reference must be a Docker image name, tag or digest.")
    if options.mode == "pull":
        validate_pull_image(options.image)
    try:
        provided_models = options.models.expanduser()
        provided_audio = options.audio.expanduser()
        if provided_models.is_symlink() or provided_audio.is_symlink():
            raise SmokeFailure("Model and audio input paths must not be symlinks.")
        models = provided_models.resolve(strict=True)
        audio = provided_audio.resolve(strict=True)
    except OSError:
        raise SmokeFailure("The requested local model or WAV path does not exist.") from None
    if (
        not models.is_dir()
        or audio.is_symlink()
        or not audio.is_file()
        or audio.suffix.lower() != ".wav"
    ):
        raise SmokeFailure("Provide an existing model directory and a regular local .wav file.")
    asr_dir = models / ASR_MODEL_NAME
    diarization_dir = models / DIARIZATION_MODEL_NAME
    if not asr_dir.is_dir() or not diarization_dir.is_dir():
        raise SmokeFailure("Stage both qwen3-asr-1.7b and Community-1 under --models first.")
    _check_model_files(asr_dir, ASR_REQUIRED_FILES, "ASR")
    _check_model_files(diarization_dir, DIARIZATION_REQUIRED_FILES, "Community-1")
    _check_asr_model_configuration(asr_dir)
    if audio.stat().st_size > MAX_AUDIO_BYTES:
        raise SmokeFailure("The smoke WAV exceeds the 25 MiB input limit.")
    try:
        with wave.open(str(audio), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if wav.getnframes() < 1 or not MIN_AUDIO_SECONDS <= duration <= 90:
                raise SmokeFailure("The smoke WAV must be between 2 and 90 seconds long.")
    except (wave.Error, OSError, ZeroDivisionError):
        raise SmokeFailure(
            "The requested WAV is unreadable or has invalid audio metadata."
        ) from None
    audio_digest = _sha256_file(audio)
    return asr_dir, diarization_dir, round(duration * 1000), audio_digest


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise SmokeFailure("An evidence path must not be a symlink.")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        os.chmod(output.name, 0o600)
        json.dump(document, output, indent=2, ensure_ascii=False, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    os.replace(temporary, path)


class ApiClient:
    def __init__(self, base_url: str, log, redactor: Redactor) -> None:
        self.base_url = base_url
        self.origin = base_url
        self.log = log
        self.redactor = redactor
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 30,
    ) -> tuple[int, bytes, dict[str, str]]:
        headers = {"Origin": self.origin, "Accept": "application/json"}
        data = body
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, response_body = response.status, response.read()
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            response_body = error.read()
            code = None
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                payload = json.loads(response_body.decode("utf-8"))
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(detail, dict):
                    code = detail.get("code")
            suffix = f" (code {code})" if isinstance(code, str) else ""
            raise SmokeFailure(f"HTTP {error.code} for {method} {path}{suffix}.") from None
        except (URLError, TimeoutError, OSError):
            raise SmokeFailure(f"The local app request failed for {method} {path}.") from None
        self.log(f"api {method} {path} -> {status}")
        if status not in expected:
            raise SmokeFailure(f"Unexpected HTTP {status} for {method} {path}.")
        return status, response_body, response_headers

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        _status, body, _headers = self.request(method, path, **kwargs)
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SmokeFailure(f"The app returned invalid JSON for {method} {path}.") from None
        if not isinstance(value, dict):
            raise SmokeFailure(f"The app returned an unexpected response for {method} {path}.")
        return value

    def exchange_access_token(self, token: str) -> None:
        self.request(
            "POST",
            "/api/access/session",
            payload={"token": token},
            expected=(204,),
        )
        self.redactor.add(token)
        for cookie in self.cookies:
            self.redactor.add(cookie.value)


class LoopbackRelay:
    """A byte-transparent loopback TCP relay for a host browser and internal app network."""

    def __init__(self, port: int, target_host: str, target_port: int = 8765) -> None:
        self.port = port
        self.target_host = target_host
        self.target_port = target_port
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                upstream = None
                deadline = time.monotonic() + 10
                while upstream is None and time.monotonic() < deadline:
                    try:
                        upstream = socket.create_connection(
                            (owner.target_host, owner.target_port), timeout=1
                        )
                    except OSError:
                        time.sleep(0.1)
                if upstream is None:
                    return
                try:
                    self.request.settimeout(30)
                    upstream.settimeout(30)
                    sockets = (self.request, upstream)
                    while True:
                        readable, _, _ = select.select(sockets, (), (), 30)
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            destination = upstream if source is self.request else self.request
                            destination.sendall(data)
                except OSError:
                    return
                finally:
                    upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            block_on_close = False

            def handle_error(self, _request, _client_address) -> None:
                return

        self.server = Server(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def open_undated_history_meeting(
    page: Any, meeting_id: str, *, on_stage: Callable[[str], None]
) -> None:
    """Open an undated meeting through the browser's calendar history path."""
    on_stage("select_undated_calendar_group")
    undated_group = page.locator("#mini-calendar .cal-undated")
    undated_group.wait_for(state="visible", timeout=10_000)
    undated_group.click(timeout=10_000)

    on_stage("open_matching_day_panel_link")
    meeting_link = page.locator(f'#day-panel-list a[href="/meetings/{meeting_id}"]')
    meeting_link.wait_for(state="visible", timeout=10_000)
    meeting_link.click(timeout=10_000)

    on_stage("wait_for_meeting_view")
    page.wait_for_url(f"**/meetings/{meeting_id}", timeout=10_000)
    page.locator("#meeting-title").wait_for(state="visible", timeout=10_000)


def edit_meeting_title(page: Any, title: str, *, timeout_ms: int = 15_000) -> None:
    """Edit a meeting title and wait for the rendered value through Playwright locators."""
    from playwright.sync_api import expect

    page.locator("#meeting-actions").click()
    page.locator("#edit-meeting").click()
    page.locator("#edit-title").fill(title)
    page.locator("#edit-save").click()
    expect(page.locator("#meeting-title")).to_have_text(title, timeout=timeout_ms)


def wait_for_locator_value(
    page: Any,
    locator: Any,
    expression: str,
    *,
    predicate: Callable[[Any], bool],
    timeout_ms: int,
    failure_message: str,
    argument: Any = None,
) -> Any:
    """Poll a locator evaluation without using CSP-sensitive page wait functions."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise SmokeFailure(failure_message)
        remaining_ms = max(1, int(remaining_seconds * 1000))
        value = locator.evaluate(expression, argument, timeout=remaining_ms)
        if predicate(value):
            return value

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise SmokeFailure(failure_message)
        page.wait_for_timeout(min(50, max(1, int(remaining_seconds * 1000))))


class SmokeHarness:
    def __init__(self, options: SmokeOptions) -> None:
        self.options = options
        self.project = unique_project_name(options.project_prefix)
        self.port = _available_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.asr_dir: Path | None = None
        self.diarization_dir: Path | None = None
        self.audio_duration_ms = 0
        self.audio_digest = ""
        self.output_root = options.output.expanduser()
        self.run_dir = self.output_root / self.project
        self.log_path = self.run_dir / "smoke.log"
        self.log_handle = None
        self.redactor = Redactor()
        self.redactor.add(str(ROOT))
        self.redactor.add(str(self.output_root.resolve()))
        self.redactor.add(str(options.models.expanduser()))
        self.redactor.add(str(options.audio.expanduser()))
        self.evidence: dict[str, Any] = {
            "tool": "asr-studio offline smoke",
            "status": "running",
            "project": self.project,
            "mode": options.mode,
            "checks": {},
            "provider_fixture": {
                "type": "synthetic ChatGPT provider registration and session fixture",
                "external_account_validity": "not assessed",
            },
            "cleanup": {"volumes_preserved": True, "errors": []},
        }
        self.api: ApiClient | None = None
        self.access_token: str | None = None
        self.isolated_config_path: Path | None = None
        self.relay: LoopbackRelay | None = None
        self.owned_project_started = False

    def log(self, message: str) -> None:
        if self.log_handle is None:
            return
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        safe = self.redactor.clean(message).replace("\r", " ")
        self.log_handle.write(f"{stamp} {safe}\n")
        self.log_handle.flush()

    def _record_browser_check(self, browser_checks: dict[str, Any], name: str) -> None:
        browser_checks[name] = True
        self.evidence.setdefault("checks", {})[name] = True
        self.log(f"browser check {name}: pass")

    def _log_command_stderr(self, label: str, stderr: str | bytes | None) -> None:
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if not isinstance(stderr, str) or not stderr:
            return
        diagnostic = " ".join(self.redactor.clean(stderr).split())
        if not diagnostic:
            return
        suffix = " … [truncated]"
        if len(diagnostic) > MAX_STDERR_DIAGNOSTIC_CHARS:
            diagnostic = diagnostic[: MAX_STDERR_DIAGNOSTIC_CHARS - len(suffix)].rstrip() + suffix
        self.log(f"command {label}: stderr: {diagnostic}")

    def run_command(
        self,
        command: list[str],
        *,
        label: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        sensitive_output: bool = False,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.log(f"command {label}: started")
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            self.log(f"command {label}: timed out after {timeout}s")
            if not sensitive_output:
                self._log_command_stderr(label, error.stderr)
            if allow_failure:
                return subprocess.CompletedProcess(command, 124, "", "")
            raise SmokeFailure(f"Command {label} exceeded its {timeout}-second deadline.") from None
        except OSError:
            self.log(f"command {label}: could not start")
            if allow_failure:
                return subprocess.CompletedProcess(command, 127, "", "")
            raise SmokeFailure(f"Command {label} could not start.") from None
        if sensitive_output:
            self.log(f"command {label}: exit={result.returncode}; output suppressed")
        else:
            self.log(f"command {label}: exit={result.returncode}")
            if result.returncode != 0:
                self._log_command_stderr(label, result.stderr)
        if result.returncode != 0 and not allow_failure:
            raise SmokeFailure(f"Command {label} exited with status {result.returncode}.")
        return result

    def compose(self, *arguments: str, config_path: Path | None = None, **kwargs: Any):
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
        ]
        command.extend(_compose_file_arguments(config_path))
        command.extend(arguments)
        kwargs.setdefault("env", self._environment())
        return self.run_command(command, **kwargs)

    def container_python(
        self,
        service: str,
        script: str,
        *,
        label: str,
        env_values: dict[str, str] | None = None,
        timeout: int = 60,
        config_path: Path | None = None,
    ) -> dict[str, Any]:
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
        ]
        command.extend(_compose_file_arguments(config_path))
        command.extend(("exec", "-T"))
        for key, value in (env_values or {}).items():
            command.extend(("-e", f"{key}={value}"))
        command.extend((service, "python", "-"))
        result = self.run_command(
            command,
            label=label,
            timeout=timeout,
            env=self._environment(),
            input_text=script,
            sensitive_output=False,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SmokeFailure(f"Container check {label} returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise SmokeFailure(f"Container check {label} returned an unexpected result.")
        return value

    def prepare(self) -> None:
        if sys.platform != "linux":
            raise SmokeFailure("The acceptance smoke requires a Linux Docker host.")
        if shutil.which("docker") is None:
            raise SmokeFailure("Docker CLI is required for the acceptance smoke.")
        if self.output_root.is_symlink():
            raise SmokeFailure("The output path must not be a symlink.")
        self.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.output_root.is_symlink() or not self.output_root.is_dir():
            raise SmokeFailure("The output path must be a regular directory, not a symlink.")
        self.output_root = self.output_root.resolve(strict=True)
        os.chmod(self.output_root, 0o700)
        self.run_dir = self.output_root / self.project
        self.log_path = self.run_dir / "smoke.log"
        self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.log_handle = self.log_path.open("x", encoding="utf-8")
        os.chmod(self.log_path, 0o600)
        self.asr_dir, self.diarization_dir, self.audio_duration_ms, self.audio_digest = (
            validate_inputs(self.options)
        )
        self.redactor.add(str(self.asr_dir))
        self.redactor.add(str(self.diarization_dir))
        self.redactor.add(str(self.options.audio.expanduser().resolve()))
        self.evidence["input_audio"] = {
            "duration_ms": self.audio_duration_ms,
            "bytes": self.options.audio.stat().st_size,
            "sha256": self.audio_digest,
        }
        self.evidence["model_artifacts"] = {
            "asr": _artifact_identity(self.asr_dir),
            "diarization": _artifact_identity(self.diarization_dir),
        }
        self.evidence["startup_timeout_seconds"] = self.options.startup_timeout
        self.evidence["task_timeout_seconds"] = self.options.task_timeout
        self.log("preflight input paths, model artifacts and short WAV: pass")

    def _assert_project_unused(self) -> None:
        project_filter = f"label=com.docker.compose.project={self.project}"
        checks = (
            (["docker", "ps", "--all", "--quiet", "--filter", project_filter], "containers"),
            (["docker", "network", "ls", "--quiet", "--filter", project_filter], "networks"),
            (["docker", "volume", "ls", "--quiet", "--filter", project_filter], "volumes"),
        )
        for command, resource_type in checks:
            result = self.run_command(
                command,
                label=f"verify unique project has no existing {resource_type}",
                timeout=30,
            )
            if result.stdout.strip():
                raise SmokeFailure(
                    f"Generated project name already owns Docker {resource_type}; "
                    "no mutation was attempted."
                )

    def _assert_image_not_in_use(self) -> None:
        if self.options.mode == "offline":
            return
        result = self.run_command(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"ancestor={self.options.image}",
            ],
            label="check selected image is not used by another container",
            timeout=30,
        )
        if result.stdout.strip():
            raise SmokeFailure(
                "The selected source/pull image is referenced by an existing container; "
                "no image update was attempted."
            )

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "ASR_IMAGE": self.options.image,
                "ASR_MODEL_HOST_PATH": str(self.options.models.expanduser().resolve()),
                "ASR_DEVICE": "cuda",
                "ASR_PORT": str(self.port),
                "ASR_PUBLIC_URL": self.base_url,
                "ASR_CHATGPT_CALLBACK_URL": "",
                "ASR_COOKIE_SECURE": "false",
                "ASR_MODEL_PATH": "",
                "ASR_COMPUTE_TYPE": "",
                "ASR_GPU_MODEL_PATH": f"/models/{ASR_MODEL_NAME}",
                "ASR_DIARIZATION_MODEL_PATH": f"/models/{DIARIZATION_MODEL_NAME}",
                "ASR_GPU_COMPUTE_TYPE": "float16",
            }
        )
        return env

    def _selected_runtime(self, *, from_image: bool) -> dict[str, Any]:
        if from_image:
            command = [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                self.options.image,
                "-m",
                "asr_studio.cli",
                "version",
                "--json",
            ]
            result = self.run_command(command, label="selected image version/schema", timeout=90)
        else:
            result = self.compose(
                "exec",
                "-T",
                "app",
                "python",
                "-m",
                "asr_studio.cli",
                "version",
                "--json",
                label="running image version/schema",
                timeout=60,
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SmokeFailure(
                "Selected image did not return a JSON version/schema report."
            ) from None
        if not isinstance(value, dict) or not isinstance(value.get("version"), str):
            raise SmokeFailure("Selected image returned an incomplete version/schema report.")
        return value

    def _anonymous_pull(self) -> None:
        """Pull the image with an empty Docker config so ambient registry logins cannot count."""
        # ponytail: the empty config drops the context store, so only the active context's
        # endpoint carries over as DOCKER_HOST; a TLS context also needs DOCKER_CERT_PATH and
        # DOCKER_TLS_VERIFY set by hand.
        env = self._environment()
        if not env.get("DOCKER_HOST"):
            context = self.run_command(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                label="read the ambient Docker context endpoint",
                timeout=30,
                env=env,
                allow_failure=True,
            )
            endpoint = context.stdout.strip()
            if endpoint:
                env["DOCKER_HOST"] = endpoint
        with tempfile.TemporaryDirectory(prefix="asr-smoke-docker-config-") as config_dir:
            env["DOCKER_CONFIG"] = config_dir
            result = self.run_command(
                ["docker", "pull", self.options.image],
                label="anonymous image pull with an empty Docker config",
                timeout=max(self.options.startup_timeout * 3, 900),
                env=env,
                allow_failure=True,
            )
        if result.returncode != 0:
            raise SmokeFailure(
                "Anonymous pull failed (image not public, registry unreachable, or Docker daemon "
                "unreachable without the Docker config; set DOCKER_HOST). See smoke.log."
            )
        self.evidence["anonymous_pull"] = {
            "performed": True,
            "docker_config": "fresh empty directory",
            "image": self.options.image,
        }
        self.evidence["checks"]["anonymous_pull"] = "pass"

    def _start_installation(self) -> None:
        self._assert_project_unused()
        self._assert_image_not_in_use()
        self.owned_project_started = True
        if self.options.mode == "pull":
            self._anonymous_pull()
        mode_argument = f"--{self.options.mode}"
        result = self.run_command(
            [str(ROOT / "scripts" / "start.sh"), mode_argument],
            label=f"installer {self.options.mode} route",
            timeout=max(self.options.startup_timeout * 3, 900),
            env=self._environment(),
            sensitive_output=True,
        )
        if result.returncode != 0:
            raise SmokeFailure(f"Installer {self.options.mode} route failed.")
        self.evidence["checks"]["installer_route"] = "pass"
        self.log(f"installer {self.options.mode} route: pass; startup output suppressed")

    def _source_commit(self) -> str:
        result = self.run_command(
            ["git", "rev-parse", "--verify", "HEAD"], label="source commit", timeout=15
        )
        commit = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise SmokeFailure("Could not identify the source commit.")
        return commit

    def _image_identity(self) -> dict[str, Any]:
        result = self.run_command(
            ["docker", "image", "inspect", self.options.image],
            label="selected image identity",
            timeout=30,
        )
        try:
            images = json.loads(result.stdout)
            image = images[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            raise SmokeFailure("Could not inspect the selected local image.") from None
        labels = (image.get("Config") or {}).get("Labels") or {}
        return {
            "id": image.get("Id"),
            "repo_digests": sorted(image.get("RepoDigests") or []),
            "revision": labels.get(IMAGE_REVISION_LABEL),
        }

    def _read_access_token(self) -> str:
        result = self.compose(
            "exec",
            "-T",
            "app",
            "python",
            "-m",
            "asr_studio.cli",
            "access",
            "show",
            label="read test-project access material",
            timeout=30,
            sensitive_output=True,
        )
        match = ACCESS_LINK_RE.search(result.stdout)
        if match is None:
            raise SmokeFailure("Could not obtain the new test-project access token in memory.")
        self.access_token = match.group(1)
        self.redactor.add(self.access_token)
        return self.access_token

    def _wait_ready(
        self, *, client: ApiClient | None = None, config_path: Path | None = None
    ) -> None:
        deadline = time.monotonic() + self.options.startup_timeout
        while time.monotonic() < deadline:
            try:
                reachable = False
                if client is None:
                    request = Request(f"{self.base_url}/healthz", method="GET")
                    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                        if response.status == 200:
                            reachable = True
                else:
                    status, _body, _headers = client.request(
                        "GET", "/api/meetings", timeout=3, expected=(200,)
                    )
                    if status == 200:
                        reachable = True
                if reachable:
                    check = self.compose(
                        "exec",
                        "-T",
                        "app",
                        "python",
                        "-m",
                        "asr_studio.cli",
                        "installation",
                        "check",
                        config_path=config_path,
                        label="verify app and worker readiness",
                        timeout=15,
                        allow_failure=True,
                    )
                    if check.returncode == 0:
                        self.log("app and worker readiness: ready")
                        return
            except (URLError, TimeoutError, OSError, SmokeFailure):
                time.sleep(1)
        raise SmokeFailure("The app did not become ready before the bounded startup deadline.")

    def _create_meeting(self, client: ApiClient, title: str) -> str:
        audio = self.options.audio.expanduser().resolve().read_bytes()
        upload = client.json(
            "POST",
            "/api/uploads",
            payload={"filename": "offline-smoke.wav", "size_bytes": len(audio)},
            expected=(201,),
        )
        upload_id = upload.get("id")
        if not isinstance(upload_id, str):
            raise SmokeFailure("The local upload API did not return an upload id.")
        client.json(
            "PUT",
            f"/api/uploads/{upload_id}/content",
            body=audio,
            content_type="application/octet-stream",
            timeout=120,
        )
        client.json("POST", f"/api/uploads/{upload_id}/complete", expected=(200,))
        meeting = client.json(
            "POST",
            "/api/meetings",
            payload={"upload_id": upload_id, "title": title, "language_hint": "en"},
            expected=(201,),
        )
        meeting_id = meeting.get("id")
        if not isinstance(meeting_id, str):
            raise SmokeFailure("The local meeting API did not return a meeting id.")
        self.log("synthetic WAV imported through authenticated HTTP: pass")
        return meeting_id

    def _start_transcription(
        self, client: ApiClient, meeting_id: str, request_id: str
    ) -> dict[str, Any]:
        accepted = client.json(
            "POST",
            "/api/tasks",
            payload={"meeting_id": meeting_id, "request_id": request_id, "language_hint": "en"},
            expected=(202,),
        )
        task_id = accepted.get("id")
        if not isinstance(task_id, str):
            raise SmokeFailure("The transcription API did not return a task id.")
        deadline = time.monotonic() + self.options.task_timeout
        last_status = "accepted"
        while time.monotonic() < deadline:
            task = client.json("GET", f"/api/tasks/{task_id}", timeout=20)
            last_status = str(task.get("status", "unknown"))
            if last_status == "succeeded":
                meeting = client.json("GET", f"/api/meetings/{meeting_id}")
                transcription = meeting.get("transcription")
                generation = (
                    transcription.get("generation") if isinstance(transcription, dict) else None
                )
                if not isinstance(generation, dict) or not generation.get("segments"):
                    raise SmokeFailure("ASR completed without a nonempty transcript generation.")
                speakers = generation.get("speakers")
                if not isinstance(speakers, list) or not speakers:
                    raise SmokeFailure("Diarization completed without real speaker output.")
                runtime = transcription.get("runtime")
                if not isinstance(runtime, dict) or runtime.get("device") != "cuda":
                    raise SmokeFailure("The app did not report the CUDA runtime profile.")
                if task.get("device") != "cuda":
                    raise SmokeFailure(
                        "The completed ASR and diarization task did not use the CUDA device."
                    )
                self.log(
                    f"task {request_id}: succeeded; segments={len(generation['segments'])}; "
                    f"speakers={len(speakers)}; runtime=cuda"
                )
                return {"task": task, "meeting": meeting, "generation": generation}
            if last_status in {"failed", "canceled", "interrupted"}:
                code = task.get("error_code")
                suffix = f" (code {code})" if isinstance(code, str) else ""
                raise SmokeFailure(f"Transcription task ended with {last_status}{suffix}.")
            time.sleep(2)
        raise SmokeFailure(
            f"Transcription task exceeded the {self.options.task_timeout}-second deadline "
            f"(last status {last_status})."
        )

    def _edit_generation(
        self, client: ApiClient, meeting_id: str, generation: dict[str, Any]
    ) -> None:
        segments = generation.get("segments")
        speakers = generation.get("speakers")
        if (
            not isinstance(segments, list)
            or not segments
            or not isinstance(speakers, list)
            or not speakers
        ):
            raise SmokeFailure(
                "Cannot verify local transcript edits without segments and speakers."
            )
        source_version = generation.get("source_version")
        generation_id = generation.get("id")
        segment = segments[0]
        if not isinstance(source_version, int) or not isinstance(generation_id, str):
            raise SmokeFailure("The app returned incomplete transcript edit versions.")
        edited = str(segment.get("text", "")).strip() + " [smoke edit]"
        text_result = client.json(
            "PATCH",
            f"/api/meetings/{meeting_id}/transcript-segments/{segment['id']}/text",
            payload={
                "expected_generation_id": generation_id,
                "expected_source_version": source_version,
                "text": edited,
            },
        )
        current_version = text_result.get("source_version", source_version)
        speaker = speakers[0]
        old_name = str(speaker.get("display_name", "Speaker"))
        new_name = (
            f"{old_name[:58]} smoke"
            if "smoke" not in old_name.lower()
            else f"{old_name[:55]} local"
        )
        client.json(
            "PATCH",
            f"/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers/"
            f"{speaker['speaker_id']}",
            payload={
                "expected_source_version": current_version,
                "display_name": new_name,
            },
        )
        self.log("local transcript text and speaker edits through the public API: pass")

    def _seed_provider_fixture(self) -> dict[str, Any]:
        script = _provider_script(r"""import uuid
now = time.time()
client_id = "oaiapp_smoke-" + uuid.uuid4().hex[:12]
session_id = uuid.uuid4().hex
access_token = "synthetic-smoke-access-" + uuid.uuid4().hex
refresh_token = "synthetic-smoke-refresh-" + uuid.uuid4().hex
with provider.store.locked(write=True) as state:
    state["connection_status"] = "connected"
    state["connection_version"] = 17
    state["active_client_id"] = client_id
    state["registrations"] = {client_id: {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": now + 3600,
        "scopes": [DIRECT_SCOPE],
        "subject": "synthetic-smoke-subject",
        "label": "Synthetic smoke session",
    }}
    state["sessions"] = {session_id: {
        "id": session_id,
        "status": "connected",
        "created_at": now - 1,
        "expires_at": now + 3600,
        "expected_client_id": client_id,
        "claimed": False,
        "error": None,
    }}
    state["selected_model"] = "gpt-6.1-sol"
    state["selected_reasoning_effort"] = "high"
    state["verification"] = None
    state.pop("last_provider_error", None)
    state.pop("last_diagnostic", None)
if not provider._issued_client_id(client_id):
    raise RuntimeError("synthetic client id is not in the issued-client format")
session_id_present = session_id in state["sessions"]
print(json.dumps({"seeded": True, "client_id_valid": True,
                  "session_id_present": session_id_present}))
""")
        result = self.container_python(
            "app", script, label="seed valid synthetic connected provider session"
        )
        if (
            result.get("seeded") is not True
            or result.get("client_id_valid") is not True
            or result.get("session_id_present") is not True
        ):
            raise SmokeFailure("Could not seed a valid synthetic connected provider session.")
        return self._provider_fixture_state()

    def _seed_saved_notes(self, meeting_id: str) -> dict[str, Any]:
        script = r"""import contextlib, hashlib, json, sqlite3, uuid
from asr_studio.config import Settings
from asr_studio.notes import accept_notes_task, get_notes_source, publish_notes_revision
from asr_studio.notes_document import build_citations, render_markdown
from asr_studio.storage import database_path
from asr_studio.tasks import claim_next_task

class FixtureStore:
    @contextlib.contextmanager
    def operation(self):
        yield

class FixtureProvider:
    store = FixtureStore()
    def assert_notes_acceptable(self):
        return None
    def notes_task_pin(self):
        return {"provider": "chatgpt", "connection_version": 7,
                "model": "gpt-6.1-sol", "reasoning_effort": "high"}

settings = Settings.from_env()
meeting_id = __import__("os").environ["SMOKE_MEETING_ID"]
with sqlite3.connect(database_path(settings.data_dir)) as db:
    meeting = db.execute("SELECT metadata_version FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    current = db.execute("SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
if meeting is None or current is None or current[0] is None:
    raise RuntimeError("completed generation missing")
generation_id = current[0]
with sqlite3.connect(database_path(settings.data_dir)) as db:
    generation = db.execute("SELECT source_version FROM transcript_generations WHERE id = ?", (generation_id,)).fetchone()
accepted = accept_notes_task(
    settings.data_dir, provider=FixtureProvider(), provider_id="chatgpt", meeting_id=meeting_id,
    request_id="smoke-notes-" + uuid.uuid4().hex,
    expected_generation_id=generation_id, expected_source_version=generation[0],
    expected_metadata_version=meeting[0], worker_ready=True, output_language="en",
    expected_model="gpt-6.1-sol", expected_reasoning_effort="high")
owner = "smoke-fixture-" + accepted["id"]
claimed = claim_next_task(settings.data_dir, owner_id=owner)
if claimed is None or claimed["id"] != accepted["id"]:
    raise RuntimeError("could not claim notes fixture task")
with sqlite3.connect(database_path(settings.data_dir)) as db:
    source_id = db.execute("SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)).fetchone()[0]
snapshot = get_notes_source(settings.data_dir, source_id)
segments = snapshot["segments"]
if not segments:
    raise RuntimeError("notes source snapshot has no transcript segments")
source_ids = [item["id"] for item in segments[:2]]
document = {
    "schema_version": 1,
    "overview": [{"text": "Synthetic offline smoke notes fixture.", "source_ids": source_ids[:1]}],
    "key_topics": [],
    "decisions": [{"text": "Keep local artifacts available.", "source_ids": source_ids[:1]}],
    "open_questions": [{"text": "Which local step needs review?", "source_ids": source_ids[-1:]}],
    "action_items": [{"text": "Review the synthetic smoke output.", "source_ids": source_ids[-1:],
                      "owner": "Smoke operator", "deadline_text": None, "deadline_date": None}],
    "confirmation_items": [{"text": "Confirm the local model path.", "source_ids": source_ids[-1:]}],
}
markdown = render_markdown(document, snapshot, output_language="en")
revision = publish_notes_revision(
    settings.data_dir, accepted["id"], owner_id=owner, markdown=markdown,
    elapsed_seconds=0.1, citations=build_citations(document, snapshot, output_language="en"))
if revision is None:
    raise RuntimeError("notes fixture revision was not published")
print(json.dumps({"note_id": revision["id"], "source_snapshot_id": source_id,
                  "task_id": accepted["id"], "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest()}))
"""
        env = {"SMOKE_MEETING_ID": meeting_id}
        return self.container_python(
            "app", script, label="seed saved notes through domain functions", env_values=env
        )

    def _seed_notes_preference(self) -> None:
        assert self.api is not None
        self.api.json(
            "PUT",
            "/api/notes-preferences",
            payload={
                "provider": "chatgpt",
                "model": "gpt-6.1-sol",
                "reasoning_effort": "high",
            },
        )
        self.log("saved ChatGPT notes preference without a provider request")

    def _provider_fixture_state(self) -> dict[str, Any]:
        script = _provider_script(r"""import hashlib
with provider.store.locked() as state:
    client_id = state.get("active_client_id")
    registration = state.get("registrations", {}).get(client_id) if client_id else None
    fixture_sessions = [item for item in state.get("sessions", {}).values()
                        if item.get("expected_client_id") == client_id]
    if not isinstance(registration, dict) or len(fixture_sessions) != 1:
        raise RuntimeError("synthetic provider session fixture is missing")
    session_id = fixture_sessions[0]["id"]
    connection_status = state.get("connection_status")
    connection_version = state.get("connection_version")
    selected_model = state.get("selected_model")
    selected_reasoning_effort = state.get("selected_reasoning_effort")
    access_token = registration.get("access_token")
    refresh_token = registration.get("refresh_token")
    expires_at = registration.get("expires_at")
    direct_scope_present = DIRECT_SCOPE in registration.get("scopes", [])
    token_expiry_valid = (isinstance(expires_at, (int, float))
                          and not isinstance(expires_at, bool) and expires_at > time.time())
    client_id_valid = provider._issued_client_id(client_id)
provider_views = provider.get_providers().get("providers", [])
provider_view = next((item for item in provider_views
                      if isinstance(item, dict) and item.get("id") == "chatgpt"), {})
reopened_session = provider.login_session(session_id)
notes_task_pin = provider.notes_task_pin()
raw = provider.store.state_path.read_bytes()
print(json.dumps({
    "connection_status": connection_status,
    "provider_view_status": provider_view.get("status"),
    "fixture_session_status": reopened_session.get("status"),
    "client_id_valid": client_id_valid,
    "direct_scope_present": direct_scope_present,
    "token_expiry_valid": token_expiry_valid,
    "access_token_sha256": hashlib.sha256(access_token.encode()).hexdigest()
        if isinstance(access_token, str) and access_token else None,
    "refresh_token_sha256": hashlib.sha256(refresh_token.encode()).hexdigest()
        if isinstance(refresh_token, str) and refresh_token else None,
    "state_sha256": hashlib.sha256(raw).hexdigest(),
    "state_bytes": len(raw),
    "connection_version": connection_version,
    "selected_model": selected_model,
    "selected_reasoning_effort": selected_reasoning_effort,
    "notes_task_pin": notes_task_pin,
}))
""")
        fixture = self.container_python(
            "app", script, label="verify local connected provider session and credentials"
        )
        validate_connected_provider_fixture(fixture)
        return fixture

    def _semantic_snapshot(self, client: ApiClient, meeting_id: str) -> dict[str, Any]:
        meeting = client.json("GET", f"/api/meetings/{meeting_id}")
        auth_status, _auth_body, _auth_headers = client.request(
            "GET", "/api/meetings", expected=(200,)
        )
        transcription = meeting.get("transcription")
        runtime = transcription.get("runtime") if isinstance(transcription, dict) else None
        env = {"SMOKE_MEETING_ID": meeting_id}
        script = r"""import hashlib, json, sqlite3
from pathlib import Path
from asr_studio.config import Settings
from asr_studio.recordings import recording_path
from asr_studio.storage import database_path

settings = Settings.from_env()
meeting_id = __import__("os").environ["SMOKE_MEETING_ID"]
db_path = database_path(settings.data_dir)
volatile = {"created_at", "updated_at", "accepted_at", "started_at", "heartbeat_at",
            "cancel_requested_at", "completed_at", "applied_at", "elapsed_seconds"}
never = {"owner_id", "request_hash", "error_message", "raw_key", "managed_key"}
hashed = {"text", "markdown", "snapshot_json", "coverage_json", "citations_json", "review_reasons"}

def normalize(row):
    value = {}
    for key in row.keys():
        if key in volatile or key in never:
            continue
        item = row[key]
        if key in hashed and item is not None:
            raw = item if isinstance(item, bytes) else str(item).encode("utf-8")
            value[key + "_sha256"] = hashlib.sha256(raw).hexdigest()
        else:
            value[key] = item
    return value

queries = {
    "meeting": ("SELECT * FROM meetings WHERE id = ?", (meeting_id,)),
    "recording": ("SELECT * FROM recordings WHERE meeting_id = ?", (meeting_id,)),
    "tasks": ("SELECT * FROM tasks WHERE meeting_id = ? ORDER BY kind, id", (meeting_id,)),
    "generations": ("SELECT * FROM transcript_generations WHERE meeting_id = ? ORDER BY generation_number", (meeting_id,)),
    "speakers": ("SELECT * FROM generation_speakers WHERE generation_id IN (SELECT id FROM transcript_generations WHERE meeting_id = ?) ORDER BY generation_id, speaker_id", (meeting_id,)),
    "segments": ("SELECT * FROM transcript_segments WHERE generation_id IN (SELECT id FROM transcript_generations WHERE meeting_id = ?) ORDER BY generation_id, sequence", (meeting_id,)),
    "notes_sources": ("SELECT * FROM notes_sources WHERE meeting_id = ? ORDER BY id", (meeting_id,)),
    "notes_revisions": ("SELECT * FROM notes_revisions WHERE meeting_id = ? ORDER BY id", (meeting_id,)),
}
with sqlite3.connect(db_path) as db:
    db.row_factory = sqlite3.Row
    observed_schema = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    tables = {}
    for name, (query, params) in queries.items():
        tables[name] = [normalize(row) for row in db.execute(query, params).fetchall()]
audio = recording_path(settings.data_dir, meeting_id)
audio_hash = hashlib.sha256()
with audio.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        audio_hash.update(chunk)
print(json.dumps({"observed_schema": observed_schema, "tables": tables,
                  "audio": {"bytes": audio.stat().st_size, "sha256": audio_hash.hexdigest()}}))
"""
        database = self.container_python(
            "app", script, label="semantic persisted-data snapshot", env_values=env
        )
        provider = self._provider_fixture_state()
        providers = client.json("GET", "/api/providers").get("providers", [])
        chatgpt = next(
            (view for view in providers if isinstance(view, dict) and view.get("id") == "chatgpt"),
            {},
        )
        reopened_status = chatgpt.get("status")
        if reopened_status != "connected":
            raise SmokeFailure("The synthetic ChatGPT provider did not reopen as connected.")
        return {
            "meeting": {
                "id": meeting.get("id"),
                "title": meeting.get("title"),
                "metadata_version": meeting.get("metadata_version"),
                "notes_source_version": meeting.get("notes_source_version"),
                "current_generation_id": (
                    transcription.get("generation", {}).get("id")
                    if isinstance(transcription, dict)
                    and isinstance(transcription.get("generation"), dict)
                    else None
                ),
                "runtime": runtime,
            },
            "database": database,
            "session_valid": auth_status == 200,
            "provider_fixture": {
                **provider,
                "reopened_status": reopened_status,
                "external_account_validity": "not assessed",
            },
        }

    def _compare_snapshot(
        self, expected: dict[str, Any], actual: dict[str, Any], stage: str
    ) -> None:
        if actual != expected:
            raise SmokeFailure(f"Semantic persistence snapshot changed after {stage}.")
        if actual.get("session_valid") is not True:
            raise SmokeFailure(f"The test-project session did not remain valid after {stage}.")
        self.log(f"semantic state, audio digest, provider fixture and session after {stage}: match")

    def _restart_lifecycle(self, client: ApiClient, meeting_id: str) -> dict[str, Any]:
        before = self._semantic_snapshot(client, meeting_id)
        self.compose("restart", "app", "worker", label="restart owned app and worker", timeout=120)
        self._wait_ready(client=client)
        after_restart = self._semantic_snapshot(client, meeting_id)
        self._compare_snapshot(before, after_restart, "service restart")
        self.compose(
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            str(self.options.startup_timeout),
            "app",
            "worker",
            label="force-recreate owned app and worker with named volumes",
            timeout=max(self.options.startup_timeout + 60, 180),
        )
        self._wait_ready(client=client)
        after_recreate = self._semantic_snapshot(client, meeting_id)
        self._compare_snapshot(before, after_recreate, "force-recreate")
        before_provider = before["provider_fixture"]
        after_restart_provider = after_restart["provider_fixture"]
        after_recreate_provider = after_recreate["provider_fixture"]
        self.evidence["persistence"] = {
            "restart_equal": True,
            "force_recreate_equal": True,
            "synthetic_connected_session_valid": True,
            "provider_fixture_bytes_preserved": True,
            "provider_access_token_digest_preserved": True,
            "provider_refresh_token_digest_preserved": True,
            "provider_scope_and_expiry_valid": True,
            "notes_task_pin_preserved": True,
            "provider_session_continuity": {
                "baseline": before_provider,
                "after_service_restart": after_restart_provider,
                "after_force_recreate": after_recreate_provider,
            },
            "provider_external_validity": "not assessed",
            "snapshot_sha256": hashlib.sha256(
                json.dumps(before, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        self.evidence["checks"]["restart_and_recreate_persistence"] = "pass"
        return before

    def _write_isolated_compose(self) -> Path:
        result = self.compose(
            "config",
            "--format",
            "json",
            label="resolve Compose config for temporary isolation override",
            timeout=60,
            env=self._environment(),
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SmokeFailure("Docker Compose did not return resolved JSON config.") from None
        isolated = isolated_compose_document(document, self.project)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", prefix="asr-smoke-compose-", delete=False
        ) as handle:
            os.chmod(handle.name, 0o600)
            json.dump(isolated, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        self.isolated_config_path = Path(handle.name)
        return self.isolated_config_path

    def _app_container_ip(self, config_path: Path) -> str:
        result = self.compose(
            "ps",
            "-q",
            "app",
            config_path=config_path,
            label="identify owned app container for loopback relay",
            timeout=30,
        )
        container_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise SmokeFailure("Could not identify the test-project app container for relay setup.")
        inspected = self.run_command(
            ["docker", "inspect", container_id],
            label="inspect owned app network address",
            timeout=30,
        )
        try:
            details = json.loads(inspected.stdout)[0]
            labels = details.get("Config", {}).get("Labels") or {}
            if labels.get("com.docker.compose.project") != self.project:
                raise SmokeFailure(
                    "The app container does not carry the unique project ownership label."
                )
            networks = details.get("NetworkSettings", {}).get("Networks", {})
            ip = next(
                (item.get("IPAddress") for item in networks.values() if item.get("IPAddress")),
                None,
            )
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError):
            raise SmokeFailure("Could not read the owned app container network address.") from None
        if not isinstance(ip, str):
            raise SmokeFailure("The internal app container has no address for the loopback relay.")
        return ip

    def _verify_egress_blocked(self, config_path: Path) -> None:
        probe = (
            "import json, socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=2).close()\n"
            "except OSError as error:\n"
            "    print(json.dumps({'blocked': True, 'error': type(error).__name__}))\n"
            "else:\n"
            "    print(json.dumps({'blocked': False}))\n"
        )
        for service in ("app", "worker"):
            result = self.container_python(
                service,
                probe,
                label=f"verify {service} outbound TCP denial",
                timeout=15,
                config_path=config_path,
            )
            if result.get("blocked") is not True:
                raise SmokeFailure(f"Outbound TCP was reachable from the isolated {service}.")
            self.log(f"isolated {service} outbound TCP: denied")
        self.evidence["checks"]["container_egress_denied"] = "pass"

    def _create_isolated_phase(self) -> Path:
        self._write_isolated_compose()
        assert self.isolated_config_path is not None
        self._verify_owned_resources()
        self.compose(
            "down",
            "--remove-orphans",
            label="remove owned containers and network before isolation; preserve named volumes",
            timeout=120,
        )
        self.compose(
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            str(self.options.startup_timeout),
            "app",
            "worker",
            config_path=self.isolated_config_path,
            label="start owned project on temporary internal network",
            timeout=max(self.options.startup_timeout + 60, 180),
        )
        target = self._app_container_ip(self.isolated_config_path)
        self.relay = LoopbackRelay(self.port, target)
        self.relay.start()
        self._wait_ready(config_path=self.isolated_config_path)
        self._verify_egress_blocked(self.isolated_config_path)
        self.evidence["checks"]["loopback_relay"] = "pass"
        self.log("host loopback reached app through the temporary relay")
        self.evidence["checks"]["isolated_network_startup"] = "pass"
        return self.isolated_config_path

    def _configure_notes_preference_in_browser_api(self, context) -> None:
        response = context.request.put(
            f"{self.base_url}/api/notes-preferences",
            data={"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
            headers={"Origin": self.base_url},
            timeout=15_000,
        )
        if response.status != 200:
            raise SmokeFailure("Browser-context notes preference setup failed.")

    def _assert_offline_notes_outcome(self, meeting_id: str, before: dict[str, Any]) -> None:
        """A connected provider accepts the run; with egress denied it must end provider_unavailable."""
        assert self.api is not None
        previous = before.get("task") or {}
        after = self.api.json("GET", f"/api/meetings/{meeting_id}/notes")
        task = after.get("task")
        if not isinstance(task, dict) or task.get("id") == previous.get("id"):
            raise SmokeFailure("The offline notes attempt did not create a task.")
        if task.get("status") != "failed" or task.get("error_code") != "provider_unavailable":
            raise SmokeFailure(
                f"The offline notes attempt ended {task.get('status')} "
                f"(code {task.get('error_code')}), not failed with provider_unavailable."
            )
        if after.get("source_locked") is not False:
            raise SmokeFailure("The failed offline notes task still holds the notes source.")
        if after.get("revisions") != before.get("revisions"):
            raise SmokeFailure("The failed offline notes attempt changed the saved notes.")
        providers = self.api.json("GET", "/api/providers").get("providers", [])
        chatgpt = next(
            (view for view in providers if isinstance(view, dict) and view.get("id") == "chatgpt"),
            {},
        )
        if chatgpt.get("status") != "connected":
            raise SmokeFailure("The offline notes attempt disconnected the persisted provider.")
        self.evidence["offline_notes_attempt"] = {
            "provider_status": "connected",
            "run_accepted_status": 202,
            "task_status": "failed",
            "task_error_code": "provider_unavailable",
            "saved_notes_unchanged": True,
            "source_locked": False,
            "external_call": "none; container egress denied",
        }

    def _browser_workflow(self, meeting_id: str, note_markdown: str) -> dict[str, Any]:
        blocked_hosts: list[str] = []
        browser_checks: dict[str, Any] = {}
        current_stage = "load_playwright"

        def report_stage(name: str) -> None:
            nonlocal current_stage
            current_stage = name
            self.log(f"browser stage: {name}")

        def complete_check(name: str) -> None:
            self._record_browser_check(browser_checks, name)

        report_stage(current_stage)
        try:
            try:
                from playwright.sync_api import expect, sync_playwright
            except ImportError:
                raise SmokeFailure(
                    "Playwright is required for the offline browser acceptance checks."
                ) from None
            assert self.access_token is not None
            with sync_playwright() as playwright:
                report_stage("launch_chromium")
                browser = playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-sync",
                    ],
                )
                browser_operation_failed = False
                try:
                    report_stage("create_browser_context")
                    context = browser.new_context(accept_downloads=True, service_workers="block")
                    report_stage("authenticate_session")
                    response = context.request.post(
                        f"{self.base_url}/api/access/session",
                        data={"token": self.access_token},
                        headers={"Origin": self.base_url},
                        timeout=15_000,
                    )
                    if response.status != 204:
                        raise SmokeFailure(
                            "Chromium could not authenticate with the test-project session."
                        )

                    def route_request(route) -> None:
                        parsed = urlsplit(route.request.url)
                        if (
                            parsed.scheme in {"http", "https"}
                            and parsed.netloc != urlsplit(self.base_url).netloc
                        ):
                            blocked_hosts.append(parsed.hostname or "unknown-host")
                            route.abort()
                        else:
                            route.continue_()

                    report_stage("block_nonlocal_requests")
                    context.route("**/*", route_request)
                    context.grant_permissions(
                        ["clipboard-read", "clipboard-write"], origin=self.base_url
                    )
                    page = context.new_page()
                    report_stage("open_home")
                    page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=30_000)

                    open_undated_history_meeting(page, meeting_id, on_stage=report_stage)
                    complete_check("history_read")

                    report_stage("edit_meeting_title")
                    edited_title = "Offline browser smoke review"
                    edit_meeting_title(page, edited_title)
                    assert self.api is not None
                    saved_meeting = self.api.json("GET", f"/api/meetings/{meeting_id}")
                    if saved_meeting.get("title") != edited_title:
                        raise SmokeFailure(
                            "Browser meeting title was not persisted through the API."
                        )
                    complete_check("history_edit")

                    report_stage("playback_and_seek")
                    page.locator("#meeting-tab-transcript").click()
                    audio = page.locator("#meeting-audio")
                    wait_for_locator_value(
                        page,
                        audio,
                        "element => element.readyState",
                        predicate=lambda ready_state: ready_state >= 1,
                        timeout_ms=20_000,
                        failure_message="Browser audio did not become ready in offline mode.",
                    )
                    playback = audio.evaluate(
                        "async element => { element.currentTime = Math.min(1, element.duration / 3); "
                        "await element.play(); return !element.paused; }"
                    )
                    if playback is not True:
                        raise SmokeFailure("Browser audio playback did not start in offline mode.")
                    page.wait_for_timeout(300)
                    audio.evaluate("element => element.pause()")
                    seek_target = min(
                        2.0, max(0.5, (audio.evaluate("element => element.duration") or 3) / 2)
                    )
                    audio.evaluate(
                        "(element, target) => { element.currentTime = target; }", seek_target
                    )
                    wait_for_locator_value(
                        page,
                        audio,
                        "(element, target) => Math.abs(element.currentTime - target) < 0.25",
                        argument=seek_target,
                        predicate=bool,
                        timeout_ms=5_000,
                        failure_message="Browser audio did not reach the requested seek position.",
                    )
                    complete_check("playback_and_seeking")

                    report_stage("edit_transcript_and_speaker")
                    first_row = page.locator(".transcript-segment").first
                    first_row.locator(".segment-edit-button").click()
                    first_row.locator(".segment-text-editor").fill(
                        "Offline browser smoke text edit."
                    )
                    first_row.locator("button[type='submit']").click()
                    expect(page.locator(".segment-edit-form")).to_have_count(0, timeout=15_000)
                    speaker = page.locator("#speaker-catalog-list > li").first
                    speaker.locator(".speaker-chip").click()
                    speaker_input = speaker.locator("input")
                    speaker_input.fill("Offline Browser Speaker")
                    speaker_input.press("Enter")
                    expect(page.locator("#speaker-catalog-list")).to_contain_text(
                        "Offline Browser Speaker", timeout=15_000
                    )
                    complete_check("local_transcript_and_speaker_edits")

                    report_stage("copy_and_export_notes")
                    page.locator("#meeting-tab-notes").click()
                    page.locator("#notes-saved-work").wait_for(state="visible", timeout=20_000)
                    page.locator("#notes-copy").click()
                    copied = wait_for_locator_value(
                        page,
                        page.locator("body"),
                        "async body => await navigator.clipboard.readText()",
                        predicate=lambda copied_text: copied_text == note_markdown,
                        timeout_ms=10_000,
                        failure_message="Saved notes copy did not match the stored Markdown.",
                    )
                    if copied != note_markdown:
                        raise SmokeFailure("Saved notes copy did not match the stored Markdown.")
                    with page.expect_download(timeout=15_000) as download_info:
                        page.locator("#notes-export").click()
                    exported_path = download_info.value.path()
                    if (
                        exported_path is None
                        or Path(exported_path).read_text(encoding="utf-8") != note_markdown
                    ):
                        raise SmokeFailure("Saved notes export did not match the stored Markdown.")
                    complete_check("saved_notes_copy_export")

                    report_stage("create_notes_without_provider_egress")
                    self._configure_notes_preference_in_browser_api(context)
                    notes_before = self.api.json("GET", f"/api/meetings/{meeting_id}/notes")
                    page.locator("body").evaluate(
                        "async (body, meetingId) => (await import('/assets/history.js'))"
                        ".meetingHistory.refreshMeeting(meetingId)",
                        meeting_id,
                    )
                    # The provider session stays connected; only the model catalog cannot load offline.
                    start_button = page.locator("#notes-start")
                    expect(start_button).to_be_enabled(timeout=90_000)
                    expect(start_button).to_contain_text("Tạo lại notes", timeout=15_000)
                    with page.expect_response(
                        lambda response: (
                            response.request.method == "POST"
                            and response.url.endswith("/notes-runs")
                        ),
                        timeout=30_000,
                    ) as run_response:
                        start_button.click()
                    if run_response.value.status != 202:
                        raise SmokeFailure("The connected provider did not accept the notes run.")
                    expect(page.locator("#notes-status")).to_contain_text(
                        OFFLINE_NOTES_STATUS_TEXT, timeout=OFFLINE_NOTES_TIMEOUT_MS
                    )
                    self._assert_offline_notes_outcome(meeting_id, notes_before)
                    complete_check("offline_notes_provider_unavailable")

                    report_stage("close_browser_context")
                    page.close()
                    context.close()
                except Exception:
                    browser_operation_failed = True
                    raise
                finally:
                    if not browser_operation_failed:
                        report_stage("close_browser")
                    try:
                        browser.close()
                    except Exception as close_error:
                        if not browser_operation_failed:
                            raise
                        self.log(f"browser cleanup failed; exception={type(close_error).__name__}")

            report_stage("validate_same_origin_requests")
            if blocked_hosts:
                hosts = ", ".join(sorted(set(blocked_hosts)))
                raise SmokeFailure(f"Browser attempted nonlocal requests; blocked hosts: {hosts}.")
            self.log("Chromium used only same-origin requests; no nonlocal request was attempted")
            return browser_checks
        except Exception as error:
            self.log(f"browser stage failed: {current_stage}; exception={type(error).__name__}")
            raise

    def _run_acceptance(self) -> None:
        self.evidence["source_commit"] = self._source_commit()
        self.evidence["image_reference"] = self.options.image
        self._start_installation()
        image_runtime = self._selected_runtime(from_image=True)
        image_identity = self._image_identity()
        self.evidence["image"] = image_identity
        parity = revision_parity(self.evidence["source_commit"], image_identity.get("revision"))
        if parity == "mismatch":
            raise SmokeFailure("The image revision label differs from the checkout revision.")
        self.evidence["checks"]["image_revision_parity"] = parity
        self.evidence["runtime"] = image_runtime
        self.log(
            f"selected image version={image_runtime.get('version')} "
            f"supported_schema={image_runtime.get('supported_schema_version')}"
        )
        token = self._read_access_token()
        self.api = ApiClient(self.base_url, self.log, self.redactor)
        self.api.exchange_access_token(token)
        self._wait_ready(client=self.api)
        running_runtime = self._selected_runtime(from_image=False)
        if running_runtime.get("version") != image_runtime.get("version"):
            raise SmokeFailure("Running app version differs from the selected image version.")
        if running_runtime.get("supported_schema_version") != image_runtime.get(
            "supported_schema_version"
        ):
            raise SmokeFailure("Running app schema support differs from the selected image report.")
        self.evidence["running_runtime"] = running_runtime
        self.evidence["checks"]["runtime_version_schema"] = "pass"

        self._seed_provider_fixture()
        first_meeting = self._create_meeting(self.api, "GPU smoke input")
        first_run = self._start_transcription(
            self.api, first_meeting, f"{self.project}-transcript-1"
        )
        self._edit_generation(self.api, first_meeting, first_run["generation"])
        second_run = self._start_transcription(
            self.api, first_meeting, f"{self.project}-transcript-2"
        )
        if second_run["generation"].get("generation_number") != 2:
            raise SmokeFailure("Public rerun did not create a second transcript generation.")
        self.evidence["checks"]["real_gpu_asr_diarization"] = "pass"
        self.evidence["checks"]["public_second_generation"] = "pass"
        self.evidence["meeting_id"] = first_meeting

        self.compose("stop", "worker", label="pause owned worker for synthetic notes fixture")
        notes_fixture = self._seed_saved_notes(first_meeting)
        self.compose(
            "start", "worker", label="restart owned worker after notes fixture", timeout=60
        )
        self._wait_ready(client=self.api)
        self._seed_notes_preference()
        self.evidence["notes_fixture"] = notes_fixture
        self.evidence["notes_fixture"]["type"] = (
            "synthetic local domain fixture; no provider inference"
        )
        snapshot = self._semantic_snapshot(self.api, first_meeting)
        if snapshot["database"]["observed_schema"] < 1:
            raise SmokeFailure("The observed database schema version is missing.")
        self.evidence["observed_schema"] = snapshot["database"]["observed_schema"]
        self.evidence["provider_fixture"] = snapshot["provider_fixture"]
        self._restart_lifecycle(self.api, first_meeting)

        self._create_isolated_phase()
        offline_meeting = self._create_meeting(self.api, "Offline GPU smoke input")
        offline_run = self._start_transcription(
            self.api, offline_meeting, f"{self.project}-offline-transcript"
        )
        if offline_run["generation"].get("segment_count", 0) < 1:
            raise SmokeFailure("Offline GPU transcription did not produce transcript segments.")
        self.evidence["checks"]["offline_gpu_asr_diarization"] = "pass"
        self.evidence["offline_meeting_id"] = offline_meeting
        note_id = notes_fixture.get("note_id")
        if not isinstance(note_id, str):
            raise SmokeFailure("Saved notes fixture did not return a note id.")
        note = self.api.json("GET", f"/api/notes/{note_id}")
        markdown = note.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            raise SmokeFailure("Saved notes fixture could not be read through the API.")
        browser_result = self._browser_workflow(first_meeting, markdown)
        self.evidence["checks"].update(browser_result)
        self.evidence["checks"]["offline_browser_external_requests"] = "none attempted"
        self.evidence["checks"]["isolated_network_startup"] = "pass"
        self.evidence["isolated_project"] = self.project
        self.log("offline API and Chromium workflow: pass")

    def _verify_owned_resources(self) -> None:
        containers: list[dict[str, Any]] = []
        networks: list[dict[str, Any]] = []
        ids_result = self.compose(
            "ps",
            "-a",
            "-q",
            label="list owned containers for cleanup verification",
            timeout=30,
        )
        container_ids = [
            item for item in ids_result.stdout.split() if re.fullmatch(r"[0-9a-f]{12,64}", item)
        ]
        if container_ids:
            inspected = self.run_command(
                ["docker", "inspect", *container_ids],
                label="verify Compose ownership labels before cleanup",
                timeout=30,
            )
            with contextlib.suppress(json.JSONDecodeError):
                containers = json.loads(inspected.stdout)
        network_result = self.run_command(
            [
                "docker",
                "network",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            label="list owned networks for cleanup verification",
            timeout=30,
        )
        network_ids = [
            item for item in network_result.stdout.split() if re.fullmatch(r"[0-9a-f]{12,64}", item)
        ]
        if network_ids:
            inspected = self.run_command(
                ["docker", "network", "inspect", *network_ids],
                label="verify network ownership labels before cleanup",
                timeout=30,
            )
            with contextlib.suppress(json.JSONDecodeError):
                networks = json.loads(inspected.stdout)
        if (container_ids and len(containers) != len(container_ids)) or (
            network_ids and len(networks) != len(network_ids)
        ):
            raise SmokeFailure("Could not verify ownership of every discovered Docker resource.")
        if not owned_compose_resources(self.project, containers, networks):
            raise SmokeFailure("A Docker resource did not carry this run's exact project label.")

    def cleanup(self) -> None:
        if self.relay is not None:
            with contextlib.suppress(Exception):
                self.relay.close()
            self.relay = None
        if not self.owned_project_started:
            if self.isolated_config_path is not None:
                with contextlib.suppress(OSError):
                    self.isolated_config_path.unlink(missing_ok=True)
            return
        ownership_verified = False
        try:
            self._verify_owned_resources()
            ownership_verified = True
            self.compose(
                "down",
                "--remove-orphans",
                config_path=self.isolated_config_path,
                label="remove only verified owned containers and network; preserve volumes",
                timeout=120,
            )
            self.evidence["cleanup"]["project_containers_and_network_removed"] = True
            self.evidence["cleanup"]["volumes_preserved"] = True
            self.log("owned test containers and network removed; named volumes preserved")
        except SmokeFailure as error:
            self.evidence["cleanup"]["errors"].append(str(error))
            if ownership_verified:
                self.evidence["cleanup"]["manual_command"] = (
                    f"docker compose --project-name {self.project} -f compose.yaml "
                    "-f compose.gpu.yaml "
                    "down --remove-orphans  # preserves named volumes"
                )
                self.log(
                    "automatic cleanup failed; see the project-scoped cleanup command in evidence"
                )
            else:
                self.log(
                    "cleanup was not attempted because Docker resource ownership "
                    "could not be verified"
                )
        finally:
            if self.isolated_config_path is not None:
                try:
                    self.isolated_config_path.unlink(missing_ok=True)
                except OSError:
                    self.evidence["cleanup"]["errors"].append(
                        "Temporary isolated Compose configuration could not be removed."
                    )

    def write_runtime_report(self) -> None:
        self.evidence["finished_at_utc"] = datetime.now(UTC).isoformat()
        if self.options.state_ref:
            self.evidence["state_ref"] = self.options.state_ref
        _atomic_json(self.run_dir / "smoke-evidence.json", self.evidence)
        checks = self.evidence.get("checks", {})
        runtime = self.evidence.get("runtime", {})
        runtime_version = runtime.get("version", "not recorded")
        supported_schema = runtime.get("supported_schema_version", "not recorded")
        lines = [
            "# Offline smoke run",
            "",
            f"- Status: **{self.evidence.get('status', 'unknown')}**",
            f"- Startup mode: `{self.options.mode}`",
            f"- Unique Compose project: `{self.project}`",
            f"- Source commit: `{self.evidence.get('source_commit', 'not recorded')}`",
            f"- Image: `{self.options.image}`",
            f"- Reported application version: `{runtime_version}`",
            f"- Supported schema: `{supported_schema}`",
            f"- Observed schema: `{self.evidence.get('observed_schema', 'not recorded')}`",
            "",
            "## Checks",
            "",
        ]
        for name, value in sorted(checks.items()):
            lines.append(f"- `{name}`: {value}")
        lines.extend(
            [
                "",
                "Synthetic provider and saved-notes fixtures establish local persistence only; "
                "no cloud inference or external account validity is tested.",
                "Review `smoke.log` and `smoke-evidence.json` for bounded command results. "
                "No tokens, cookies, transcript text, local input paths, or model contents are recorded.",
                "",
            ]
        )
        report = self.run_dir / "smoke-report.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        os.chmod(report, 0o600)

    def run(self) -> None:
        failure: str | None = None
        try:
            self.prepare()
            self._run_acceptance()
            self.evidence["status"] = "pass"
            self.evidence["blocker"] = None
        except SmokeFailure as error:
            failure = str(error)
            self.evidence["status"] = "fail"
            self.evidence["blocker"] = failure
            self.log(f"smoke failed: {failure}")
        except Exception as error:
            failure = f"Unexpected {type(error).__name__} during smoke; inspect the sanitized log."
            self.evidence["status"] = "fail"
            self.evidence["blocker"] = failure
            self.log(f"smoke failed: {failure}")
        finally:
            self.cleanup()
            if self.evidence.get("cleanup", {}).get("errors"):
                self.evidence["status"] = "fail"
                self.evidence["blocker"] = (
                    self.evidence.get("blocker") or "Owned Docker cleanup failed."
                )
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
            if self.run_dir.exists():
                self.write_runtime_report()
        if failure:
            raise SmokeFailure(failure)
        if self.evidence.get("cleanup", {}).get("errors"):
            raise SmokeFailure(
                "Automatic cleanup failed; see the project-scoped command in the report."
            )


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded, opt-in Linux/NVIDIA offline acceptance smoke using only a unique "
            "Compose project and prepared local assets."
        )
    )
    parser.add_argument("--image", required=True, help="Explicit local image ref, tag or digest.")
    parser.add_argument(
        "--models",
        required=True,
        type=Path,
        help="Parent directory containing prepared ASR and Community-1 models.",
    )
    parser.add_argument(
        "--audio",
        required=True,
        type=Path,
        help=(
            "Authorized local two-voice WAV, 2–90 seconds, at most 25 MiB; "
            "synthetic audio is recommended."
        ),
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Directory for private smoke logs and evidence."
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("source", "pull", "offline"),
        help="Installer route to exercise.",
    )
    parser.add_argument(
        "--project-prefix",
        default=DEFAULT_PROJECT_PREFIX,
        help="Safe prefix; a random suffix is always added.",
    )
    parser.add_argument(
        "--task-timeout", type=int, default=900, help="Bound each real ASR task in seconds."
    )
    parser.add_argument(
        "--startup-timeout", type=int, default=180, help="Bound service readiness in seconds."
    )
    parser.add_argument(
        "--state-ref", help="Optional caller-supplied correlation id recorded in the evidence."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.task_timeout < 30 or arguments.task_timeout > 7200:
        parser.error("--task-timeout must be between 30 and 7200 seconds")
    if arguments.startup_timeout < 30 or arguments.startup_timeout > 900:
        parser.error("--startup-timeout must be between 30 and 900 seconds")
    try:
        options = SmokeOptions(
            image=arguments.image,
            models=arguments.models,
            audio=arguments.audio,
            output=arguments.output,
            mode=arguments.mode,
            project_prefix=arguments.project_prefix,
            task_timeout=arguments.task_timeout,
            startup_timeout=arguments.startup_timeout,
            state_ref=arguments.state_ref,
        )
        harness = SmokeHarness(options)
        harness.run()
        print("Offline smoke passed. Evidence was written to the requested output directory.")
        return 0
    except SmokeFailure as error:
        print(f"Offline smoke failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
