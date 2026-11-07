"""Provider-backed notes service with its own lifetime and recovery boundary."""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

from asr_studio.antigravity import AntigravityProvider
from asr_studio.chatgpt import ChatGPTProvider
from asr_studio.config import Settings
from asr_studio.safe_logging import emit_safe_log
from asr_studio.service_runtime import (
    LOCK_RETRY_SECONDS,
    acquire_service_lock,
    service_heartbeat_path,
    service_is_active,
    service_is_healthy,
    service_lock_is_held,
    service_lock_path,
    write_service_heartbeat,
)
from asr_studio.storage import initialize
from asr_studio.tasks import claim_next_task, recover_abandoned_tasks
from asr_studio.worker import _run_notes_task, worker_role
from asr_studio.worker import is_active as worker_is_active
from asr_studio.worker import is_healthy as worker_is_healthy

NOTES_SERVICE_ROLE = "notes"
HEARTBEAT_INTERVAL_SECONDS = 5
TASK_POLL_INTERVAL_SECONDS = 0.5


class NotesServiceAlreadyRunning(RuntimeError):
    """Another notes service still owns the notes lifetime lock."""


def heartbeat_path(data_dir: Path) -> Path:
    return service_heartbeat_path(data_dir, NOTES_SERVICE_ROLE)


def lock_path(data_dir: Path) -> Path:
    return service_lock_path(data_dir, NOTES_SERVICE_ROLE)


def acquire_lock(data_dir: Path, *, retry_seconds: float = LOCK_RETRY_SECONDS) -> int | None:
    return acquire_service_lock(data_dir, NOTES_SERVICE_ROLE, retry_seconds=retry_seconds)


def write_heartbeat(data_dir: Path, state: str = "idle") -> None:
    write_service_heartbeat(data_dir, NOTES_SERVICE_ROLE, state, role=NOTES_SERVICE_ROLE)


def is_healthy(data_dir: Path, *, now: float | None = None) -> bool:
    return service_is_healthy(
        data_dir,
        NOTES_SERVICE_ROLE,
        now=now,
        expected_roles={NOTES_SERVICE_ROLE},
    )


def is_active(data_dir: Path, *, now: float | None = None) -> bool:
    return service_is_active(
        data_dir,
        NOTES_SERVICE_ROLE,
        now=now,
        expected_roles={NOTES_SERVICE_ROLE},
    )


def _legacy_worker_active(data_dir: Path) -> bool:
    """Use the legacy worker's lifetime lock as the overlap authority.

    A pre-split worker may have a stale or untagged heartbeat while it is still
    processing.  The worker lock is therefore checked independently of
    heartbeat freshness; the role tag only distinguishes it from the split
    transcription worker.
    """
    if not service_lock_is_held(data_dir, "worker"):
        return False
    if worker_role(data_dir) != "transcription":
        return True
    # A stale split heartbeat with a held worker lock is ambiguous during a
    # pre-split/upgrade overlap; fail closed and protect the task.
    return not worker_is_active(data_dir, role="transcription")


def is_ready(data_dir: Path) -> bool:
    """Notes readiness accepts the split service or an active legacy combined worker."""
    return is_healthy(data_dir) or worker_is_healthy(data_dir, role="legacy")


def _notes_loop(settings: Settings, lock_fd: int, stop_event: threading.Event) -> None:
    last_heartbeat = 0.0
    legacy_was_seen = False
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            write_heartbeat(settings.data_dir, "idle")
            last_heartbeat = now
        legacy_active = _legacy_worker_active(settings.data_dir)
        if legacy_active:
            legacy_was_seen = True
            stop_event.wait(TASK_POLL_INTERVAL_SECONDS)
            continue
        if legacy_was_seen:
            # A pre-split legacy process may have started without the notes
            # reservation probe. Recover its notes task only after its worker
            # lifetime lock is gone.
            recover_abandoned_tasks(
                settings.data_dir,
                role=NOTES_SERVICE_ROLE,
                legacy_worker_active=False,
            )
            legacy_was_seen = False
        task = claim_next_task(
            settings.data_dir,
            owner_id=uuid.uuid4().hex,
            role=NOTES_SERVICE_ROLE,
            allow_legacy=True,
        )
        if task is None:
            stop_event.wait(TASK_POLL_INTERVAL_SECONDS)
            continue
        write_heartbeat(settings.data_dir, "running")
        last_heartbeat = time.monotonic()
        _run_notes_task(settings, lock_fd, task, stop_event, heartbeat_writer=write_heartbeat)
        write_heartbeat(settings.data_dir, "idle")
        last_heartbeat = time.monotonic()


def run_notes_service(settings: Settings) -> None:
    previous_umask = os.umask(0o077)
    stop_event = threading.Event()
    previous_handlers = {
        signum: signal.signal(signum, lambda *_: stop_event.set())
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    lock_fd: int | None = None
    try:
        initialize(settings.data_dir)
        lock_fd = acquire_lock(settings.data_dir)
        if lock_fd is None:
            raise NotesServiceAlreadyRunning(
                "Another notes service still owns the notes processing lock."
            )
        provider_dir = settings.provider_dir
        if provider_dir is None:
            raise RuntimeError("ASR_PROVIDER_DIR could not be resolved")
        provider_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ChatGPTProvider(provider_dir, callback_url="").ensure()
        AntigravityProvider(provider_dir).ensure()
        legacy_active = _legacy_worker_active(settings.data_dir)
        write_heartbeat(settings.data_dir, "starting")
        recover_abandoned_tasks(
            settings.data_dir,
            role=NOTES_SERVICE_ROLE,
            legacy_worker_active=legacy_active,
        )
        write_heartbeat(settings.data_dir, "idle")
        _notes_loop(settings, lock_fd, stop_event)
    finally:
        if lock_fd is not None:
            with suppress(OSError):
                write_heartbeat(settings.data_dir, "stopped")
        if lock_fd is not None:
            os.close(lock_fd)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        os.umask(previous_umask)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="asr-studio-notes-service")
    parser.add_argument("--health-check", action="store_true")
    arguments = parser.parse_args()
    settings = Settings.from_env()
    if arguments.health_check:
        return 0 if is_healthy(settings.data_dir) else 1
    try:
        run_notes_service(settings)
    except NotesServiceAlreadyRunning:
        emit_safe_log(
            event="notes_service_error",
            stage="notes_service_startup",
            code="notes_service_already_running",
        )
        return 1
    except Exception:
        emit_safe_log(
            event="notes_service_error",
            stage="notes_service_startup",
            code="notes_service_unavailable",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
