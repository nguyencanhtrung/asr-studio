"""Local operator commands for access credentials and runtime preflight checks."""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import ctranslate2
import qrcode
from qrcode.constants import ERROR_CORRECT_M

from asr_studio import __version__
from asr_studio.asr_models import CATALOG, model_path
from asr_studio.auth import AuthState, AuthStore
from asr_studio.config import Settings
from asr_studio.diarization_runner import (
    DIARIZATION_ERROR_MESSAGES,
    DiarizationConfigurationError,
    prepared_diarization_directory,
    safe_diarization_error,
)
from asr_studio.model_install import InstallError
from asr_studio.model_installer import download_model, install_model
from asr_studio.runner import (
    ModelConfigurationError,
    prepared_model_directory,
)
from asr_studio.runtime import (
    MODEL_REPOSITORIES,
    RuntimeConfigurationError,
    read_runtime_profile,
    select_runtime,
)
from asr_studio.storage import SCHEMA_VERSION
from asr_studio.tasks import TASK_ERROR_MESSAGES, TaskError, get_task
from asr_studio.worker import is_healthy as worker_is_healthy

_TASK_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_TASK_STATES = frozenset(
    {"accepted", "running", "stopping", "succeeded", "failed", "canceled", "interrupted"}
)
_SAFE_TASK_STAGES = frozenset(
    {
        "accepted",
        "starting",
        "starting_runner",
        "loading_model",
        "transcribing",
        "starting_diarization",
        "loading_diarization_model",
        "decoding_input",
        "diarizing",
        "publishing",
        "failed",
        "canceled",
        "interrupted",
    }
)
_SAFE_TASK_ERROR_MESSAGES = {**DIARIZATION_ERROR_MESSAGES, **TASK_ERROR_MESSAGES}


def access_link(state: AuthState, settings: Settings) -> str:
    return f"{settings.access_url_prefix}{state.token}"


def print_qr_code(value: str) -> None:
    qr_code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2, box_size=1)
    qr_code.add_data(value)
    qr_code.make(fit=True)
    for row in qr_code.get_matrix():
        print("".join("██" if cell else "  " for cell in row))


def print_access_details(state: AuthState, settings: Settings, *, rotated: bool = False) -> None:
    if rotated:
        print("Access token rotated. Previous links and sessions are invalid.")
        print()
    print("Local access link:")
    print(access_link(state, settings))
    print()
    print("Scan or save this QR code:")
    print_qr_code(access_link(state, settings))


def check_health(settings: Settings) -> int:
    request = Request(
        "http://127.0.0.1:8765/healthz",
        headers={"Host": settings.host_header},
    )
    try:
        with urlopen(request, timeout=2) as response:
            return 0 if response.status == 200 else 1
    except (OSError, URLError):
        return 1


def _select_runtime(settings: Settings, *, require_model: bool = True):
    try:
        return select_runtime(
            settings,
            get_cuda_device_count=ctranslate2.get_cuda_device_count,
            load_library=ctypes.CDLL,
            get_supported_compute_types=ctranslate2.get_supported_compute_types,
            require_model=require_model,
        )
    except RuntimeConfigurationError:
        print("runtime_unavailable: selecting_runtime", file=sys.stderr)
        return None


def check_model(settings: Settings) -> int:
    profile = _select_runtime(settings)
    if profile is None:
        return 1
    print("Local ASR model is ready.")
    return 0


def prepare_model(settings: Settings) -> int:
    profile = _select_runtime(settings, require_model=False)
    if profile is None:
        return 1
    try:
        prepared_model_directory(profile.model_path)
        print("Local ASR model is ready.")
        return 0
    except ModelConfigurationError:
        pass

    repository = MODEL_REPOSITORIES[profile.device]
    print("Preparing local ASR model...")
    try:
        from huggingface_hub import snapshot_download

        # The GPU default is in the catalog and stays on its pinned revision; whisper-small is not.
        revision = next(
            (model.revision for model in CATALOG.values() if model.repository == repository), None
        )
        snapshot_download(repo_id=repository, revision=revision, local_dir=profile.model_path)
        prepared_model_directory(profile.model_path)
    except (OSError, RuntimeError, ValueError, ModelConfigurationError):
        print("model_preparation_failed: model_preparation", file=sys.stderr)
        return 1
    print("Local ASR model is ready.")
    return 0


def prepare_asr_model(settings: Settings, asr_model: str) -> int:
    """Download one optional ASR model; no Recording is read and no worker needs to restart."""
    model = CATALOG[asr_model]
    path = Path(model_path(settings, asr_model)).expanduser().resolve()
    print("Preparing local ASR model...")
    try:
        install_model(model, path, install_id=uuid.uuid4().hex, fetch=download_model)
    except InstallError as error:
        # A conversion publishes by renaming into place, so it cannot use a directory with files.
        if error.code == "target_not_empty":
            print(
                "model_preparation_failed: the model directory is not empty. "
                "Move its files away and run this command again.",
                file=sys.stderr,
            )
        else:
            print(f"model_preparation_failed: {error.code}", file=sys.stderr)
        return 1
    print("Local ASR model is ready.")
    return 0


def check_runtime(settings: Settings, *, verbose: bool = False) -> int:
    profile = _select_runtime(settings)
    if profile is None:
        return 1
    diarization_error = _run_diarization_preflight(
        model_path=settings.diarization_model_path,
        device=profile.device,
        verbose=verbose,
    )
    if diarization_error is not None:
        code, message = diarization_error
        print(f"{code}: {message}", file=sys.stderr)
        if not verbose:
            print(
                "Re-run runtime check with --verbose to show local traceback details.",
                file=sys.stderr,
            )
        return 1
    if profile.device == "cuda":
        print("NVIDIA GPU is ready; local ASR runtime is ready.")
    else:
        print("CPU fallback is ready; local ASR runtime is ready.")
    print(f"Local Community-1 diarization model is ready on {profile.device}.")
    return 0


def check_installation(settings: Settings) -> int:
    """Check that the app can use the worker and both mounted local models."""
    if not worker_is_healthy(settings.data_dir):
        print("Installation incomplete: the transcription worker is not ready.", file=sys.stderr)
        return 1
    profile = read_runtime_profile(settings.data_dir)
    if profile is None:
        print("Installation incomplete: the app cannot read the worker model.", file=sys.stderr)
        return 1
    try:
        prepared_model_directory(profile.model_path)
        prepared_diarization_directory(settings.diarization_model_path)
    except (ModelConfigurationError, DiarizationConfigurationError, OSError):
        print("Installation incomplete: the app cannot read the mounted models.", file=sys.stderr)
        return 1
    print("Installation ready: worker and local models are visible to the app.")
    return 0


def _run_diarization_preflight(
    *, model_path: Path, device: str, verbose: bool = False
) -> tuple[str, str] | None:
    """Keep CTranslate2 and torch CUDA initialization in separate processes."""
    command = [
        sys.executable,
        "-m",
        "asr_studio.diarization_runner",
        "--check-runtime",
        "--model",
        str(model_path),
        "--device",
        device,
    ]
    if verbose:
        command.append("--verbose")
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=None if verbose else subprocess.DEVNULL,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return safe_diarization_error(None, None)
    if result.returncode == 0:
        return None
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            return safe_diarization_error(event.get("code"), event.get("message"))
    return safe_diarization_error(None, None)


def diagnose_task(settings: Settings, task_id: str) -> int:
    """Print only stable task fields and allowlisted diarization details."""
    if not _TASK_ID_RE.fullmatch(task_id):
        print("Task ID must be 32 lowercase hexadecimal characters.", file=sys.stderr)
        return 2
    try:
        task = get_task(settings.data_dir, task_id)
    except TaskError as error:
        if error.code == "task_not_found":
            print("Task not found.", file=sys.stderr)
        else:
            print("Task diagnostics are unavailable.", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("Task diagnostics are unavailable.", file=sys.stderr)
        return 1

    meeting_id = task.get("meeting_id")
    safe_meeting_id = (
        meeting_id
        if isinstance(meeting_id, str) and _TASK_ID_RE.fullmatch(meeting_id)
        else "unknown"
    )
    status = task.get("status")
    stage = task.get("stage")
    error_code = task.get("error_code")
    code = error_code if isinstance(error_code, str) else ""

    print(f"Diagnostic ID: {task_id}")
    print(f"Meeting ID: {safe_meeting_id}")
    safe_status = status if status in _SAFE_TASK_STATES else "unknown"
    safe_stage = stage if stage in _SAFE_TASK_STAGES else "unknown"
    print(f"Status: {safe_status}")
    print(f"Stage: {safe_stage}")
    if safe_status == "failed" and safe_stage == "failed":
        print("Failure phase: See worker logs for this Diagnostic ID.")
    if not code:
        print("Error: none")
    elif code in _SAFE_TASK_ERROR_MESSAGES:
        print(f"Code: {code}")
        print(f"Message: {_SAFE_TASK_ERROR_MESSAGES[code]}")
    else:
        print("Code: other")
        print(
            "Message: This task failed during another local processing phase. "
            "Check the worker logs or retry."
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asr-studio")
    parser.add_argument("--version", action="version", version=f"asr-studio {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    version_parser = commands.add_parser("version", help="report application and schema versions")
    version_parser.add_argument("--json", action="store_true", help="print a JSON version report")
    version_parser.add_argument("--expect-version")
    version_parser.add_argument("--expect-schema", type=int)
    access_parser = commands.add_parser("access", help="manage the local access token")
    access_commands = access_parser.add_subparsers(dest="access_command", required=True)
    access_commands.add_parser("show", help="print the current access link and QR code")
    access_commands.add_parser("rotate", help="replace the token and invalidate old sessions")
    commands.add_parser("health", help="check the local app liveness endpoint")
    model_parser = commands.add_parser("model", help="validate local ASR model resources")
    model_commands = model_parser.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("check", help="validate the configured local ASR model")
    prepare_parser = model_commands.add_parser(
        "prepare", help="download the selected local ASR model if needed"
    )
    prepare_parser.add_argument(
        "--asr-model",
        choices=tuple(CATALOG),
        help="prepare this optional model instead of the hardware default",
    )
    runtime_parser = commands.add_parser("runtime", help="validate local ASR execution")
    runtime_commands = runtime_parser.add_subparsers(dest="runtime_command", required=True)
    runtime_check_parser = runtime_commands.add_parser(
        "check", help="validate the model and compute runtime"
    )
    runtime_check_parser.add_argument(
        "--verbose", action="store_true", help="show local preflight traceback details"
    )
    installation_parser = commands.add_parser("installation", help="check the running installation")
    installation_commands = installation_parser.add_subparsers(
        dest="installation_command", required=True
    )
    installation_commands.add_parser("check", help="check worker and app-visible models")
    task_parser = commands.add_parser("task", help="inspect a local processing task")
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)
    diagnose_parser = task_commands.add_parser("diagnose", help="show safe task diagnostics")
    diagnose_parser.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "version":
        if (arguments.expect_version is None) != (arguments.expect_schema is None):
            print(
                "Both --expect-version and --expect-schema are required together.", file=sys.stderr
            )
            return 2
        report = {
            "version": __version__,
            "supported_schema_version": SCHEMA_VERSION,
        }
        if arguments.expect_version is not None and (
            arguments.expect_version != __version__ or arguments.expect_schema != SCHEMA_VERSION
        ):
            print(
                f"Version/schema mismatch: image reports {__version__} / schema {SCHEMA_VERSION}.",
                file=sys.stderr,
            )
            return 1
        if arguments.json:
            print(json.dumps(report, separators=(",", ":")))
        else:
            print(f"asr-studio {__version__} (supported schema {SCHEMA_VERSION})")
        return 0

    settings = Settings.from_env()
    if arguments.command == "health":
        return check_health(settings)
    if arguments.command == "model":
        if arguments.model_command == "prepare":
            if arguments.asr_model:
                return prepare_asr_model(settings, arguments.asr_model)
            return prepare_model(settings)
        return check_model(settings)
    if arguments.command == "runtime":
        return check_runtime(settings, verbose=arguments.verbose)
    if arguments.command == "installation":
        return check_installation(settings)
    if arguments.command == "task":
        return diagnose_task(settings, arguments.task_id)

    store = AuthStore(settings.auth_dir)
    if arguments.access_command == "show":
        print_access_details(store.ensure(), settings)
        return 0
    if arguments.access_command == "rotate":
        print_access_details(store.rotate(), settings, rotated=True)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
