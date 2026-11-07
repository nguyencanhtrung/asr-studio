"""Exclusive durable task worker with process-group cancellation and restart recovery."""

from __future__ import annotations

import argparse
import fcntl  # noqa: F401 - retained for callers/tests that probe the worker lock
import json
import math
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from asr_studio import service_runtime
from asr_studio.alignment import AlignmentError, align_segments_to_speakers, validate_turns
from asr_studio.asr_models import AUTO, engine_of
from asr_studio.config import Settings
from asr_studio.diarization_runner import (
    DIARIZATION_ERROR_MESSAGES,
    DiarizationConfigurationError,
    prepared_diarization_directory,
    safe_diarization_error,
)
from asr_studio.notes import publish_notes_revision
from asr_studio.recordings import RecordingError, recording_path
from asr_studio.runtime import (
    RuntimeConfigurationError,
    select_runtime,
    write_runtime_profile,
)
from asr_studio.safe_logging import emit_safe_log
from asr_studio.service_runtime import (
    LOCK_RETRY_INTERVAL_SECONDS,
    LOCK_RETRY_SECONDS,
    acquire_service_lock,
    acquire_service_probe,
    service_heartbeat_path,
    service_is_active,
    service_is_healthy,
    service_lock_is_held,
    service_lock_path,
    service_role,
    write_service_heartbeat,
)
from asr_studio.storage import initialize
from asr_studio.tasks import (
    TASK_ERROR_MESSAGES,
    TaskError,
    claim_next_task,
    fail_task,
    finish_canceled_task,
    finish_interrupted_task,
    publish_complete_generation,
    recover_abandoned_tasks,
    task_runtime_state,
    update_task_progress,
)

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_MAX_AGE_SECONDS = service_runtime.HEARTBEAT_MAX_AGE_SECONDS
TASK_POLL_INTERVAL_SECONDS = 0.5
TASK_PROGRESS_INTERVAL_SECONDS = 1.0
RUNNER_TERM_GRACE_SECONDS = 5.0
RUNNER_KILL_GRACE_SECONDS = 5.0
RUNNER_POST_EOF_GRACE_SECONDS = 2.0
NOTES_TASK_TIMEOUT_SECONDS = 3600.0
MAX_CHILD_EVENT_LINE_BYTES = 1024 * 1024
WORKER_LOCK_NAME = ".worker.lock"
WORKER_LOCK_RETRY_SECONDS = LOCK_RETRY_SECONDS
WORKER_LOCK_RETRY_INTERVAL_SECONDS = LOCK_RETRY_INTERVAL_SECONDS
_HEALTHY_STATES = frozenset({"idle", "running", "stopping"})


class WorkerAlreadyRunning(RuntimeError):
    """Another worker still owns the local processing lock."""


def heartbeat_path(data_dir: Path) -> Path:
    return service_heartbeat_path(data_dir, "worker")


def worker_lock_path(data_dir: Path) -> Path:
    return service_lock_path(data_dir, "worker")


def acquire_worker_lock(
    data_dir: Path, *, retry_seconds: float = WORKER_LOCK_RETRY_SECONDS
) -> int | None:
    return acquire_service_lock(data_dir, "worker", retry_seconds=retry_seconds)


def _worker_lock_is_held(data_dir: Path) -> bool:
    return service_lock_is_held(data_dir, "worker")


def write_heartbeat(data_dir: Path, state: str = "idle", *, role: str = "legacy") -> None:
    write_service_heartbeat(data_dir, "worker", state, role=role)


def is_healthy(data_dir: Path, *, now: float | None = None, role: str | None = None) -> bool:
    expected_roles = {role} if role is not None else None
    return service_is_healthy(data_dir, "worker", now=now, expected_roles=expected_roles)


def is_active(data_dir: Path, *, now: float | None = None, role: str | None = None) -> bool:
    expected_roles = {role} if role is not None else None
    return service_is_active(data_dir, "worker", now=now, expected_roles=expected_roles)


def worker_role(data_dir: Path) -> str | None:
    return service_role(data_dir, "worker")


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_and_reap_process_group(
    process: subprocess.Popen[bytes],
    *,
    term_grace_seconds: float = RUNNER_TERM_GRACE_SECONDS,
    kill_grace_seconds: float = RUNNER_KILL_GRACE_SECONDS,
) -> bool:
    """Stop the runner's entire process group and wait until its leader is reaped."""
    process_group_id = process.pid
    if _group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGTERM)

    term_deadline = time.monotonic() + term_grace_seconds
    while time.monotonic() < term_deadline:
        process.poll()
        if process.returncode is not None and not _group_exists(process_group_id):
            return True
        time.sleep(0.05)

    if _group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGKILL)

    kill_deadline = time.monotonic() + kill_grace_seconds
    while time.monotonic() < kill_deadline:
        process.poll()
        if process.returncode is not None and not _group_exists(process_group_id):
            return True
        time.sleep(0.05)
    process.poll()
    return process.returncode is not None and not _group_exists(process_group_id)


def _safe_runner_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYANNOTE_METRICS_ENABLED": "0",
            "DO_NOT_TRACK": "1",
            "ORT_DISABLE_TELEMETRY": "1",
        }
    )
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "HF_API_TOKEN"):
        environment.pop(name, None)
    return environment


def _verify_torch_cuda() -> None:
    """Probe torch CUDA in a short-lived process so the worker keeps no model state."""
    command = (
        "import torch; "
        "raise SystemExit(0 if torch.cuda.is_available() "
        "and torch.empty(1, device='cuda').is_cuda else 1)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=_safe_runner_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("Cannot verify the PyTorch CUDA runtime.") from error
    if result.returncode != 0:
        raise RuntimeConfigurationError(
            "PyTorch cannot allocate on the selected CUDA device for speaker diarization."
        )


def _decode_event(line: bytes) -> dict[str, Any]:
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("Invalid runner event")
    return value


def _runner_command(task: dict[str, Any], audio_path: Path) -> list[str]:
    """Build a command without turning language metadata into a decoder constraint."""
    return [
        sys.executable,
        "-m",
        "asr_studio.runner",
        "--model",
        str(task["model_path"]),
        "--audio",
        str(audio_path),
        "--device",
        str(task["device"]),
        "--compute-type",
        str(task["compute_type"]),
        "--engine",
        engine_of(task.get("asr_model", AUTO), Path(str(task["model_path"]))),
    ]


def _diarization_command(task: dict[str, Any], audio_path: Path) -> list[str]:
    """Build a local-path-only Community-1 command."""
    return [
        sys.executable,
        "-m",
        "asr_studio.diarization_runner",
        "--model",
        str(task["diarization_model_path"]),
        "--audio",
        str(audio_path),
        "--device",
        str(task["device"]),
        "--diagnostic-id",
        str(task["id"]),
        "--meeting-id",
        str(task["meeting_id"]),
    ]


def _notes_command(task: dict[str, Any], owner_id: str, data_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "asr_studio.notes_runner",
        "--data-dir",
        str(data_dir),
        "--task-id",
        str(task["id"]),
        "--owner-id",
        owner_id,
    ]


def _run_child(
    settings: Settings,
    lock_fd: int,
    task_id: str,
    owner_id: str,
    command: list[str],
    stop_event: threading.Event,
    task_started: float,
    progress: Any,
    on_event: Any,
    *,
    deadline: float | None = None,
    heartbeat_writer: Any = write_heartbeat,
) -> dict[str, object]:
    """Run one isolated phase and return only after its process group is gone."""
    process: subprocess.Popen[bytes] | None = None
    result: dict[str, object] = {
        "return_code": None,
        "stop_reason": None,
        "protocol_error": False,
    }
    runtime_state = task_runtime_state(settings.data_dir, task_id)
    if runtime_state is None or runtime_state[1] != owner_id:
        result["stop_reason"] = "ownership_lost"
        return result
    if runtime_state[0] == "stopping":
        result["stop_reason"] = "canceled"
        return result
    if stop_event.is_set():
        result["stop_reason"] = "interrupted"
        return result
    if deadline is not None and time.monotonic() >= deadline:
        result["stop_reason"] = "deadline"
        return result

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            pass_fds=(lock_fd,),
            start_new_session=True,
            env={
                **_safe_runner_environment(),
                "ASR_DATA_DIR": str(settings.data_dir),
                "ASR_PROVIDER_DIR": str(settings.provider_dir),
            },
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_descriptor = process.stdout.fileno()
        stderr_descriptor = process.stderr.fileno()
        pending = bytearray()
        reached_eof = False
        stderr_eof = False
        stderr_seen = False
        post_eof_deadline: float | None = None
        child_exit_time: float | None = None
        last_progress_write = 0.0
        last_heartbeat = 0.0

        def consume_line(line: bytes) -> bool:
            try:
                event = _decode_event(line)
                return bool(on_event(event))
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                return False

        while not reached_eof or not stderr_eof or process.poll() is None:
            descriptors = []
            if not reached_eof:
                descriptors.append(stdout_descriptor)
            if not stderr_eof:
                descriptors.append(stderr_descriptor)
            if descriptors:
                readable, _, _ = select.select(descriptors, [], [], 0.2)
            else:
                stop_event.wait(0.2)
                readable = []
            for readable_descriptor in readable:
                chunk = os.read(readable_descriptor, 65536)
                if readable_descriptor == stderr_descriptor:
                    if not chunk:
                        stderr_eof = True
                    else:
                        stderr_seen = True
                    continue
                if not chunk:
                    reached_eof = True
                    post_eof_deadline = time.monotonic() + RUNNER_POST_EOF_GRACE_SECONDS
                    continue
                pending.extend(chunk)
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if len(raw_line) > MAX_CHILD_EVENT_LINE_BYTES or not consume_line(raw_line):
                        result["protocol_error"] = True
                        break
                if len(pending) > MAX_CHILD_EVENT_LINE_BYTES:
                    result["protocol_error"] = True
                if result["protocol_error"]:
                    break
            if result["protocol_error"]:
                break

            now = time.monotonic()
            runtime_state = task_runtime_state(settings.data_dir, task_id)
            if runtime_state is None or runtime_state[1] != owner_id:
                result["stop_reason"] = "ownership_lost"
            elif runtime_state[0] == "stopping":
                result["stop_reason"] = "canceled"
            elif stop_event.is_set():
                result["stop_reason"] = "interrupted"
            elif deadline is not None and now >= deadline:
                result["stop_reason"] = "deadline"
            if result["stop_reason"] is not None:
                break

            if now - last_progress_write >= TASK_PROGRESS_INTERVAL_SECONDS:
                progress_values = progress()
                if len(progress_values) == 3:
                    stage, transcript_end, segment_count = progress_values
                    progress_current = progress_total = progress_unit = None
                else:
                    (
                        stage,
                        transcript_end,
                        segment_count,
                        progress_current,
                        progress_total,
                        progress_unit,
                    ) = progress_values
                if not update_task_progress(
                    settings.data_dir,
                    task_id,
                    owner_id=owner_id,
                    stage=stage,
                    elapsed_seconds=now - task_started,
                    transcript_end_seconds=transcript_end,
                    segment_count=segment_count,
                    progress_current=progress_current,
                    progress_total=progress_total,
                    progress_unit=progress_unit,
                ):
                    result["stop_reason"] = "ownership_lost"
                    break
                last_progress_write = now
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                heartbeat_writer(settings.data_dir, "running")
                last_heartbeat = now
            if process.poll() is not None and child_exit_time is None:
                child_exit_time = now
            if (
                reached_eof
                and (process.poll() is None or not stderr_eof)
                and post_eof_deadline is not None
                and now >= post_eof_deadline
            ):
                result["protocol_error"] = True
                break
            if (
                child_exit_time is not None
                and (not reached_eof or not stderr_eof)
                and now - child_exit_time > 2
            ):
                result["protocol_error"] = True
                break

        if (
            pending.strip()
            and result["stop_reason"] is None
            and not result["protocol_error"]
            and not consume_line(bytes(pending))
        ):
            result["protocol_error"] = True

        if result["stop_reason"] is not None or result["protocol_error"]:
            heartbeat_writer(settings.data_dir, "stopping")
            while not terminate_and_reap_process_group(process):
                if stop_event.wait(0.5):
                    pass
                heartbeat_writer(settings.data_dir, "stopping")
        else:
            result["return_code"] = process.wait()
            if _group_exists(process.pid):
                while not terminate_and_reap_process_group(process, term_grace_seconds=0.1):
                    heartbeat_writer(settings.data_dir, "stopping")
                    time.sleep(0.5)
                result["protocol_error"] = True
        result["return_code"] = process.returncode
        if stderr_seen:
            emit_safe_log(
                event="child_stderr",
                stage="child_process",
                code="child_stderr_suppressed",
            )
        return result
    except BaseException:
        if process is not None:
            reaped = terminate_and_reap_process_group(process)
            while not reaped:
                heartbeat_writer(settings.data_dir, "stopping")
                reaped = terminate_and_reap_process_group(process, term_grace_seconds=0.1)
                if not reaped:
                    time.sleep(0.5)
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if process is not None and process.stderr is not None:
            process.stderr.close()


def _finish_after_child_stop(
    settings: Settings,
    task_id: str,
    owner_id: str,
    stop_event: threading.Event,
    elapsed_seconds: float,
) -> None:
    runtime_state = task_runtime_state(settings.data_dir, task_id)
    if runtime_state is None or runtime_state[1] != owner_id:
        return
    if runtime_state[0] == "stopping":
        finish_canceled_task(
            settings.data_dir, task_id, owner_id=owner_id, elapsed_seconds=elapsed_seconds
        )
    elif stop_event.is_set():
        finish_interrupted_task(
            settings.data_dir, task_id, owner_id=owner_id, elapsed_seconds=elapsed_seconds
        )


def _run_notes_task(
    settings: Settings,
    lock_fd: int,
    task: dict[str, Any],
    stop_event: threading.Event,
    *,
    heartbeat_writer: Any = write_heartbeat,
) -> None:
    task_id = str(task["id"])
    owner_id = str(task["owner_id"])
    started = time.monotonic()
    deadline = started + NOTES_TASK_TIMEOUT_SECONDS
    state: dict[str, Any] = {
        "stage": "starting_notes",
        "markdown": None,
        "coverage": None,
        "citations": None,
        "error": None,
        "progress_current": None,
        "progress_total": None,
        "progress_unit": None,
    }

    def finish_failure(code: str, message: str) -> None:
        elapsed = time.monotonic() - started
        _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
        runtime_state = task_runtime_state(settings.data_dir, task_id)
        if runtime_state is None or runtime_state[1] != owner_id:
            return
        if runtime_state[0] in {"stopping", "canceled"} or stop_event.is_set():
            return
        fail_task(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            error_code=code,
            message=message,
            elapsed_seconds=elapsed,
        )

    def on_event(event: dict[str, Any]) -> bool:
        event_type = event["type"]
        if event_type == "stage" and event.get("stage") == "generating_notes":
            state["stage"] = "generating_notes"
            return True
        if event_type == "progress":
            stage = event.get("stage")
            current = event.get("current")
            total = event.get("total")
            unit = event.get("unit")
            if (
                stage
                not in {
                    "preparing_source",
                    "generating",
                    "chunking",
                    "merging",
                    "repairing",
                    "rendering",
                }
                or isinstance(current, bool)
                or not isinstance(current, int)
                or current < 0
                or (
                    total is not None
                    and (isinstance(total, bool) or not isinstance(total, int) or total < current)
                )
                or unit not in {"phase", "calls", "chunks", "repairs", "recoveries"}
            ):
                return False
            state["stage"] = stage
            state["progress_current"] = current
            state["progress_total"] = total
            state["progress_unit"] = unit
            return True
        if event_type == "result" and state["markdown"] is None:
            markdown = event.get("markdown")
            coverage = event.get("coverage")
            citations = event.get("citations")
            if (
                not isinstance(markdown, str)
                or "\x00" in markdown
                or not isinstance(coverage, dict)
                or not coverage
                or not (citations is None or isinstance(citations, list))
            ):
                return False
            state["markdown"] = markdown
            state["coverage"] = coverage
            state["citations"] = citations
            return True
        if event_type == "error" and state["error"] is None:
            code = event.get("code")
            message = event.get("message")
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
                or not isinstance(message, str)
                or len(message) > 500
            ):
                return False
            state["error"] = (code, message)
            return True
        return False

    try:
        if not update_task_progress(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            stage=state["stage"],
            elapsed_seconds=0,
            transcript_end_seconds=0,
            segment_count=int(task["segment_count"]),
            clear_progress=True,
        ):
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, 0)
            return
        child = _run_child(
            settings,
            lock_fd,
            task_id,
            owner_id,
            _notes_command(task, owner_id, settings.data_dir),
            stop_event,
            started,
            lambda: (
                state["stage"],
                0.0,
                int(task["segment_count"]),
                state["progress_current"],
                state["progress_total"],
                state["progress_unit"],
            ),
            on_event,
            deadline=deadline,
            heartbeat_writer=heartbeat_writer,
        )
        elapsed = time.monotonic() - started
        if child["stop_reason"] == "deadline":
            finish_failure(
                "notes_timeout",
                "Notes generation exceeded the task-wide time limit.",
            )
            return
        if child["stop_reason"] is not None:
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
            return
        if state["error"] is not None:
            code, message = state["error"]
            finish_failure(code, message)
            return
        if child["protocol_error"] or child["return_code"] != 0 or state["markdown"] is None:
            finish_failure(
                "notes_runner_protocol_error",
                "The notes worker did not return a complete result.",
            )
            return
        if time.monotonic() >= deadline:
            finish_failure(
                "notes_timeout",
                "Notes generation exceeded the task-wide time limit.",
            )
            return
        if not update_task_progress(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            stage="publishing",
            elapsed_seconds=elapsed,
            transcript_end_seconds=0,
            segment_count=int(task["segment_count"]),
            clear_progress=True,
        ):
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
            return
        revision = publish_notes_revision(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            markdown=state["markdown"],
            elapsed_seconds=elapsed,
            coverage=state["coverage"],
            citations=state["citations"],
        )
        if revision is None:
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
    except TaskError:
        finish_failure("notes_publication_failed", TASK_ERROR_MESSAGES["notes_publication_failed"])
    except (OSError, subprocess.SubprocessError, sqlite3.Error):
        emit_safe_log(event="notes_error", stage="publishing", code="notes_failed")
        finish_failure("notes_failed", "Notes generation failed. Check the worker logs.")


def _run_task(
    settings: Settings,
    lock_fd: int,
    task: dict[str, Any],
    stop_event: threading.Event,
    *,
    heartbeat_writer: Any = write_heartbeat,
) -> None:
    if task.get("kind") == "notes":
        _run_notes_task(settings, lock_fd, task, stop_event, heartbeat_writer=heartbeat_writer)
        return
    task_id = str(task["id"])
    owner_id = str(task["owner_id"])
    started = time.monotonic()
    phase = "transcription"
    state: dict[str, Any] = {
        "stage": "starting_runner",
        "segments": [],
        "latest_segment_end": 0.0,
        "language_probability": None,
        "asr_complete": None,
        "asr_error": None,
        "regular_turns": [],
        "exclusive_turns": [],
        "diarization_complete": None,
        "diarization_error": None,
    }

    def progress() -> tuple[str, float, int, None, None, None]:
        return state["stage"], state["latest_segment_end"], len(state["segments"]), None, None, None

    def finish_error(code: str, message: str) -> None:
        if phase == "diarization":
            code, message = safe_diarization_error(code, message)
        emit_safe_log(event=f"{phase}_error", stage=state["stage"], code=code)
        elapsed = time.monotonic() - started
        _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
        runtime_state = task_runtime_state(settings.data_dir, task_id)
        if runtime_state is None or runtime_state[1] != owner_id:
            return
        if runtime_state[0] in {"stopping", "canceled"} or stop_event.is_set():
            return
        fail_task(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            error_code=code,
            message=message,
            elapsed_seconds=elapsed,
        )

    def handle_asr_event(event: dict[str, Any]) -> bool:
        event_type = event["type"]
        if state["asr_complete"] is not None or state["asr_error"] is not None:
            return False
        if event_type == "stage" and event.get("stage") in {"loading_model", "transcribing"}:
            state["stage"] = event["stage"]
            return True
        if event_type == "segment":
            start = event.get("start_seconds")
            end = event.get("end_seconds")
            text = event.get("text")
            segment_id = event.get("id")
            if (
                isinstance(start, bool)
                or not isinstance(start, (int, float))
                or not math.isfinite(float(start))
                or start < 0
                or isinstance(end, bool)
                or not isinstance(end, (int, float))
                or not math.isfinite(float(end))
                or end < start
                or not isinstance(text, str)
                or not isinstance(segment_id, str)
            ):
                return False
            segment = {key: value for key, value in event.items() if key != "type"}
            state["segments"].append(segment)
            state["latest_segment_end"] = max(state["latest_segment_end"], float(end))
            state["stage"] = "transcribing"
            return True
        if event_type == "complete":
            count = event.get("segment_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return False
            state["asr_complete"] = event
            state["language_probability"] = event.get("language_probability")
            return True
        if event_type == "error":
            state["asr_error"] = {
                "code": str(event.get("code") or "transcription_failed"),
                "message": str(event.get("message") or "Local transcription failed."),
            }
            return True
        return False

    def handle_diarization_event(event: dict[str, Any]) -> bool:
        event_type = event["type"]
        if state["diarization_complete"] is not None or state["diarization_error"] is not None:
            return False
        if event_type == "stage" and event.get("stage") in {
            "loading_diarization_model",
            "decoding_input",
            "diarizing",
        }:
            state["stage"] = event["stage"]
            return True
        if event_type == "turn":
            kind = event.get("kind")
            if kind not in {"regular", "exclusive"}:
                return False
            turn = {
                "start_seconds": event.get("start_seconds"),
                "end_seconds": event.get("end_seconds"),
                "speaker_id": event.get("speaker_id"),
            }
            (state["regular_turns"] if kind == "regular" else state["exclusive_turns"]).append(turn)
            return True
        if event_type == "complete":
            for key in ("regular_turn_count", "exclusive_turn_count"):
                value = event.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return False
            state["diarization_complete"] = event
            return True
        if event_type == "error":
            code, message = safe_diarization_error(event.get("code"), event.get("message"))
            state["diarization_error"] = {
                "code": code,
                "message": message,
            }
            return True
        return False

    try:
        audio_path = recording_path(settings.data_dir, task["meeting_id"])
        update_task_progress(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            stage=state["stage"],
            elapsed_seconds=0,
            transcript_end_seconds=0,
            segment_count=0,
        )
        asr_result = _run_child(
            settings,
            lock_fd,
            task_id,
            owner_id,
            _runner_command(task, audio_path),
            stop_event,
            started,
            progress,
            handle_asr_event,
            heartbeat_writer=heartbeat_writer,
        )
        if asr_result["stop_reason"] is not None:
            _finish_after_child_stop(
                settings, task_id, owner_id, stop_event, time.monotonic() - started
            )
            return
        if state["asr_error"] is not None:
            finish_error(state["asr_error"]["code"], state["asr_error"]["message"])
            return
        if (
            asr_result["protocol_error"]
            or asr_result["return_code"] != 0
            or state["asr_complete"] is None
            or state["asr_complete"].get("segment_count") != len(state["segments"])
        ):
            finish_error(
                "runner_protocol_error",
                "The local transcription runner did not return a complete result.",
            )
            return

        phase = "diarization"
        state["stage"] = "starting_diarization"
        diarization_result = _run_child(
            settings,
            lock_fd,
            task_id,
            owner_id,
            _diarization_command(task, audio_path),
            stop_event,
            started,
            progress,
            handle_diarization_event,
            heartbeat_writer=heartbeat_writer,
        )
        if diarization_result["stop_reason"] is not None:
            _finish_after_child_stop(
                settings, task_id, owner_id, stop_event, time.monotonic() - started
            )
            return
        if state["diarization_error"] is not None:
            finish_error(state["diarization_error"]["code"], state["diarization_error"]["message"])
            return
        completed = state["diarization_complete"]
        if (
            diarization_result["protocol_error"]
            or diarization_result["return_code"] != 0
            or completed is None
            or completed["regular_turn_count"] != len(state["regular_turns"])
            or completed["exclusive_turn_count"] != len(state["exclusive_turns"])
        ):
            finish_error(
                "diarization_failed",
                DIARIZATION_ERROR_MESSAGES["diarization_failed"],
            )
            return

        try:
            regular_turns = validate_turns(state["regular_turns"])
            exclusive_turns = validate_turns(state["exclusive_turns"])
            alignment = align_segments_to_speakers(
                state["segments"], regular_turns, exclusive_turns
            )
        except AlignmentError:
            finish_error(
                "diarization_failed",
                DIARIZATION_ERROR_MESSAGES["diarization_failed"],
            )
            return

        phase = "publishing"
        state["stage"] = "publishing"
        elapsed = time.monotonic() - started
        if not update_task_progress(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            stage="publishing",
            elapsed_seconds=elapsed,
            transcript_end_seconds=state["latest_segment_end"],
            segment_count=len(state["segments"]),
        ):
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
            return
        generation = publish_complete_generation(
            settings.data_dir,
            task_id,
            owner_id=owner_id,
            segments=alignment["segments"],
            speakers=alignment["speakers"],
            regular_turn_count=alignment["regular_turn_count"],
            exclusive_turn_count=alignment["exclusive_turn_count"],
            alignment_policy=alignment["alignment_policy"],
            language_probability=state["language_probability"],
            elapsed_seconds=elapsed,
        )
        if generation is None:
            _finish_after_child_stop(settings, task_id, owner_id, stop_event, elapsed)
    except RecordingError:
        finish_error("recording_unavailable", "Meeting recording is unavailable to the worker.")
    except TaskError as error:
        if phase == "publishing":
            finish_error("publication_failed", TASK_ERROR_MESSAGES["publication_failed"])
        else:
            finish_error(error.code, error.message)
    except (OSError, subprocess.SubprocessError):
        if phase == "diarization":
            finish_error("diarization_failed", DIARIZATION_ERROR_MESSAGES["diarization_failed"])
        elif phase == "publishing":
            finish_error("publication_failed", TASK_ERROR_MESSAGES["publication_failed"])
        else:
            finish_error(
                "runner_start_failed", "The local transcription runner could not be started."
            )
    except sqlite3.Error:
        if phase == "publishing":
            finish_error("publication_failed", TASK_ERROR_MESSAGES["publication_failed"])
        elif phase == "diarization":
            finish_error("diarization_failed", DIARIZATION_ERROR_MESSAGES["diarization_failed"])
        else:
            finish_error(
                "transcription_failed", "Local transcription failed. Check the worker logs."
            )
    except Exception:
        if phase == "diarization":
            finish_error("diarization_failed", DIARIZATION_ERROR_MESSAGES["diarization_failed"])
        elif phase == "publishing":
            finish_error("publication_failed", TASK_ERROR_MESSAGES["publication_failed"])
        else:
            finish_error(
                "transcription_failed", "Local transcription failed. Check the worker logs."
            )


def _worker_loop(
    settings: Settings,
    lock_fd: int,
    stop_event: threading.Event,
    *,
    role: str = "legacy",
    heartbeat_writer: Any = write_heartbeat,
) -> None:
    last_heartbeat = 0.0
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            heartbeat_writer(settings.data_dir, "idle")
            last_heartbeat = now
        allowed_kinds = (
            {"transcription"}
            if role == "legacy" and service_is_active(settings.data_dir, "notes")
            else None
        )
        task = claim_next_task(
            settings.data_dir,
            owner_id=uuid.uuid4().hex,
            role=role,
            allowed_kinds=allowed_kinds,
        )
        if task is None:
            stop_event.wait(TASK_POLL_INTERVAL_SECONDS)
            continue
        heartbeat_writer(settings.data_dir, "running")
        last_heartbeat = time.monotonic()
        _run_task(settings, lock_fd, task, stop_event, heartbeat_writer=heartbeat_writer)
        heartbeat_writer(settings.data_dir, "idle")
        last_heartbeat = time.monotonic()


def run_worker(settings: Settings, *, role: str = "legacy") -> None:
    if role not in {"legacy", "transcription"}:
        raise ValueError(f"Unknown worker role: {role}")
    previous_umask = os.umask(0o077)
    stop_event = threading.Event()
    previous_handlers = {
        signum: signal.signal(signum, lambda *_: stop_event.set())
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    lock_fd: int | None = None
    notes_probe_fd: int | None = None

    def heartbeat_writer(directory: Path, state: str) -> None:
        write_heartbeat(directory, state, role=role)

    try:
        initialize(settings.data_dir)
        lock_fd = acquire_worker_lock(settings.data_dir)
        if lock_fd is None:
            raise WorkerAlreadyRunning("Another worker still owns the local processing lock.")
        if role == "legacy":
            notes_probe_fd = acquire_service_probe(settings.data_dir, "notes")
            if notes_probe_fd is None:
                raise WorkerAlreadyRunning("The notes service still owns its processing lock.")
        profile = select_runtime(settings)
        if profile.device == "cuda":
            _verify_torch_cuda()
        try:
            prepared_diarization_directory(settings.diarization_model_path)
        except DiarizationConfigurationError as error:
            raise RuntimeConfigurationError(
                "Local Community-1 model is not prepared at "
                f"{settings.diarization_model_path}: {error}"
            ) from error
        write_runtime_profile(settings.data_dir, profile)
        heartbeat_writer(settings.data_dir, "starting")
        if role == "legacy":
            recover_abandoned_tasks(settings.data_dir)
        else:
            recover_abandoned_tasks(settings.data_dir, role=role)
        heartbeat_writer(settings.data_dir, "idle")
        if role == "legacy":
            _worker_loop(settings, lock_fd, stop_event)
        else:
            _worker_loop(
                settings,
                lock_fd,
                stop_event,
                role=role,
                heartbeat_writer=heartbeat_writer,
            )
    finally:
        if lock_fd is not None:
            with suppress(OSError):
                heartbeat_writer(settings.data_dir, "stopped")
        if notes_probe_fd is not None:
            with suppress(OSError):
                os.close(notes_probe_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        os.umask(previous_umask)


def main() -> int:
    parser = argparse.ArgumentParser(prog="asr-studio-worker")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--role", choices=("legacy", "transcription"), default="legacy")
    arguments = parser.parse_args()
    settings = Settings.from_env()
    if arguments.health_check:
        return 0 if is_healthy(settings.data_dir, role=arguments.role) else 1
    try:
        if arguments.role == "legacy":
            run_worker(settings)
        else:
            run_worker(settings, role=arguments.role)
    except RuntimeConfigurationError:
        emit_safe_log(event="worker_error", stage="worker_startup", code="runtime_unavailable")
        return 1
    except WorkerAlreadyRunning:
        emit_safe_log(event="worker_error", stage="worker_startup", code="worker_already_running")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
