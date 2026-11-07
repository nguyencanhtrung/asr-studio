"""Small durable lifetime primitives shared by the processing services."""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path

HEARTBEAT_MAX_AGE_SECONDS = 20
LOCK_RETRY_SECONDS = 1.0
LOCK_RETRY_INTERVAL_SECONDS = 0.05
HEALTHY_STATES = frozenset({"idle", "running", "stopping"})
ACTIVE_STATES = frozenset({*HEALTHY_STATES, "starting"})
SERVICE_FILES = {
    "worker": (".worker.lock", "worker.liveness"),
    "transcription": (".transcription.lock", "transcription.liveness"),
    "notes": (".notes-service.lock", "notes-service.liveness"),
}


def _files_for(service: str) -> tuple[str, str]:
    try:
        return SERVICE_FILES[service]
    except KeyError:
        raise ValueError(f"Unknown service: {service}") from None


def service_lock_path(data_dir: Path, service: str) -> Path:
    lock_name, _ = _files_for(service)
    return Path(data_dir) / lock_name


def service_heartbeat_path(data_dir: Path, service: str) -> Path:
    _, heartbeat_name = _files_for(service)
    return Path(data_dir) / heartbeat_name


def acquire_service_lock(
    data_dir: Path,
    service: str,
    *,
    retry_seconds: float = LOCK_RETRY_SECONDS,
) -> int | None:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    descriptor = os.open(
        service_lock_path(directory, service),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def acquire_service_probe(
    data_dir: Path,
    service: str,
    *,
    retry_seconds: float = LOCK_RETRY_SECONDS,
) -> int | None:
    """Hold a shared lifetime probe on a service lock for the process lifetime."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    descriptor = os.open(
        service_lock_path(directory, service),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def service_lock_is_held(data_dir: Path, service: str) -> bool:
    path = service_lock_path(data_dir, service)
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def write_service_heartbeat(
    data_dir: Path,
    service: str,
    state: str = "idle",
    *,
    role: str | None = None,
) -> None:
    if state not in {*HEALTHY_STATES, "starting", "stopped"}:
        raise ValueError("Unknown service heartbeat state")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{service}-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as temporary_file:
            fields = [state, str(time.time_ns())]
            if role is not None:
                fields.append(role)
            temporary_file.write(" ".join(fields) + "\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, service_heartbeat_path(directory, service))
        os.chmod(service_heartbeat_path(directory, service), 0o600)
    except OSError:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def service_role(data_dir: Path, service: str) -> str | None:
    path = service_heartbeat_path(data_dir, service)
    try:
        fields = path.read_text(encoding="ascii").split()
    except OSError:
        return "legacy" if service == "worker" else None
    if len(fields) >= 3 and fields[2]:
        return fields[2]
    # Heartbeats written before role tagging belong to the combined worker.
    return "legacy" if service == "worker" else None


def _heartbeat_is_current(path: Path, now: float | None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return -1 <= age <= HEARTBEAT_MAX_AGE_SECONDS


def _state(data_dir: Path, service: str) -> str | None:
    try:
        return (
            service_heartbeat_path(data_dir, service)
            .read_text(encoding="ascii")
            .split(maxsplit=1)[0]
        )
    except (OSError, IndexError):
        return None


def service_is_healthy(
    data_dir: Path,
    service: str,
    *,
    now: float | None = None,
    expected_roles: set[str] | frozenset[str] | None = None,
) -> bool:
    path = service_heartbeat_path(data_dir, service)
    state = _state(data_dir, service)
    return (
        state in HEALTHY_STATES
        and _heartbeat_is_current(path, now)
        and service_lock_is_held(data_dir, service)
        and (expected_roles is None or service_role(data_dir, service) in expected_roles)
    )


def service_is_active(
    data_dir: Path,
    service: str,
    *,
    now: float | None = None,
    expected_roles: set[str] | frozenset[str] | None = None,
) -> bool:
    path = service_heartbeat_path(data_dir, service)
    state = _state(data_dir, service)
    return (
        state in ACTIVE_STATES
        and _heartbeat_is_current(path, now)
        and service_lock_is_held(data_dir, service)
        and (expected_roles is None or service_role(data_dir, service) in expected_roles)
    )
