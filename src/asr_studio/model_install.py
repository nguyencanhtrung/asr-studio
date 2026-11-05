"""The file protocol between the app and the model installer, over one small shared volume.

The installer has the network and writes /models; the app has neither. They meet only here,
in a volume that holds no recording, transcript or credential. Every write is a temp file
renamed into place, so a reader never sees half a file.

    requests/<model>.json   the app creates it, once; present while a job is queued or running
    cancel/<model>.<job>    the app asks that one job to stop; a late cancel cannot touch a retry
    status/<model>.json     the installer's progress, and the last word after a job fails
    installer.json          the installer's heartbeat

The volume is private to these two services and they run as different users, so its
directories are world-writable and its files world-readable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from asr_studio.asr_models import CATALOG

ERROR_CODES = frozenset(
    {
        "insufficient_disk",
        "network",
        "disk_full",
        "integrity",
        "target_not_empty",
        "models_not_writable",
        "interrupted",
    }
)
PHASES = frozenset({"downloading", "converting", "verifying"})
JOB_ID = re.compile(r"[0-9a-f]{32}")  # it becomes part of a file name
STALE_SECONDS = 10.0


class InstallError(Exception):
    """An install that stopped for a reason the operator can act on."""

    def __init__(self, code: str, **details: int) -> None:
        super().__init__(code)
        if code not in ERROR_CODES:
            raise ValueError(code)
        self.code = code
        self.details = details


class InstallStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- files ---------------------------------------------------------------------------

    def _path(self, folder: str, model_id: str, suffix: str = "") -> Path:
        if model_id not in CATALOG:  # the id becomes a file name
            raise KeyError(model_id)
        return self.root / folder / f"{model_id}{suffix}"

    def _stage(self, path: Path, document: dict[str, Any]) -> Path:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):  # the other service made it and opened it up
            directory.chmod(0o777)
        temp = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)  # the app's umask would hide it from the installer
            json.dump(document, handle, separators=(",", ":"))
        return temp

    def _write(self, path: Path, document: dict[str, Any]) -> None:
        os.replace(self._stage(path, document), path)

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    # -- the app's side ------------------------------------------------------------------

    def request(self, model_id: str) -> str:
        """Queue one job and return its id. A model with a job already queued gets no second one."""
        path = self._path("requests", model_id, ".json")
        job_id = uuid.uuid4().hex
        temp = self._stage(path, {"id": job_id, "model": model_id, "at": time.time()})
        try:
            os.link(temp, path)  # fails if the file exists: the one job wins, content complete
        except FileExistsError:
            existing = self._read(path)
            return str(existing["id"]) if existing and "id" in existing else job_id
        finally:
            temp.unlink(missing_ok=True)
        return job_id

    def cancel(self, model_id: str) -> bool:
        """Ask the job to stop. False when there is none."""
        request = self._read(self._path("requests", model_id, ".json"))
        job_id = request.get("id") if request else None
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return False
        self._write(self._path("cancel", model_id, f".{job_id}"), {})
        return True

    def job(self, model_id: str) -> dict[str, Any]:
        """What the app shows for one model: idle, queued, running, cancelling or failed."""
        request = self._read(self._path("requests", model_id, ".json"))
        status = self._read(self._path("status", model_id, ".json"))
        if status is not None and request is not None and status.get("id") != request.get("id"):
            status = None  # left by an earlier job
        if status is not None and status.get("state") == "failed":
            error = status.get("error")
            return {
                "state": "failed",
                "error": error if error in ERROR_CODES else "interrupted",
                "required_bytes": status.get("required_bytes"),
                "free_bytes": status.get("free_bytes"),
            }
        if request is None:
            return {"state": "idle"}
        state = "queued" if status is None else str(status.get("state", "running"))
        if self.cancel_requested({**request, "model": model_id}):
            state = "cancelling"
        return {
            "state": state,
            "phase": phase
            if isinstance(phase := (status or {}).get("phase"), str) and phase in PHASES
            else None,
            "bytes_done": (status or {}).get("bytes_done", 0),
            "bytes_total": (status or {}).get("bytes_total", CATALOG[model_id].download_bytes),
        }

    def installer(self, now: float | None = None) -> dict[str, bool]:
        """Whether an installer is alive, and whether it can write the model directory."""
        beat = self._read(self.root / "installer.json")
        current = time.time() if now is None else now
        at = beat.get("at") if beat else None
        alive = isinstance(at, int | float) and abs(current - at) <= STALE_SECONDS
        return {
            "available": alive,
            "models_writable": alive and beat is not None and beat.get("models_writable") is True,
        }

    # -- the installer's side ------------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        """Queued and interrupted jobs, oldest first."""
        found = []
        for model_id in CATALOG:
            request = self._read(self._path("requests", model_id, ".json"))
            job_id = request.get("id") if request else None
            if isinstance(job_id, str) and JOB_ID.fullmatch(job_id):
                found.append({**request, "model": model_id})
        return sorted(
            found, key=lambda item: item["at"] if isinstance(item.get("at"), int | float) else 0
        )

    def status(self, model_id: str) -> dict[str, Any] | None:
        return self._read(self._path("status", model_id, ".json"))

    def cancel_requested(self, request: dict[str, Any]) -> bool:
        job_id = request.get("id")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return False
        return self._path("cancel", request["model"], f".{job_id}").exists()

    def report(self, request: dict[str, Any], **fields: Any) -> None:
        document = {"id": request["id"], "model": request["model"], **fields}
        self._write(self._path("status", request["model"], ".json"), document)

    def finish(self, request: dict[str, Any], *, keep: dict[str, Any] | None = None) -> None:
        """End a job. The status goes before the request, so a retry never meets a stale one."""
        model_id = request["model"]
        if keep is None:
            self._unlink(self._path("status", model_id, ".json"))
        else:
            self.report(request, **keep)
        self._unlink(self._path("cancel", model_id, f".{request['id']}"))
        self._unlink(self._path("requests", model_id, ".json"))

    def sweep_cancels(self) -> None:
        """Forget cancels of jobs that are gone, such as one that arrived after its job ended."""
        live = {f"{request['model']}.{request['id']}" for request in self.pending()}
        for marker in (self.root / "cancel").glob("*"):
            if marker.name not in live:
                marker.unlink(missing_ok=True)

    def beat(self, *, models_writable: bool) -> None:
        self._write(
            self.root / "installer.json",
            {"at": time.time(), "models_writable": models_writable},
        )
