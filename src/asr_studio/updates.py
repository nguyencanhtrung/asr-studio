"""Read-only host update status and request admission during maintenance."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from asr_studio import __version__

STABLE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def available_version() -> str:
    directory = os.environ.get("ASR_UPDATE_DIR")
    if not directory:
        return ""
    try:
        root = Path(directory)
        if (root / "mode").read_text().strip() != "online":
            return ""
        tag = json.loads((root / "release.json").read_text())["tag"]
        candidate = STABLE.fullmatch(tag)
        installed = STABLE.fullmatch("v" + __version__)
        if (
            candidate
            and installed
            and tuple(map(int, candidate.groups())) > tuple(map(int, installed.groups()))
        ):
            return tag
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ""


@contextmanager
def admit_work() -> Iterator[bool]:
    directory = os.environ.get("ASR_UPDATE_DIR")
    if not directory:
        yield True
        return
    import fcntl

    try:
        lock = (Path(directory) / "admission.lock").open("rb")
    except FileNotFoundError:
        # Direct Compose installations may never have initialized the updater.
        yield True
        return
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield not (Path(directory) / "maintenance").exists()


def require_idle() -> None:
    """Run inside the current app while the host holds its exclusive admission lock."""
    with admit_work() as admitted:
        if admitted:
            raise SystemExit("The running app does not share the host maintenance lock.")
    data = Path(os.environ["ASR_DATA_DIR"]) / "app.sqlite3"
    checks = {
        "upload": "SELECT 1 FROM uploads WHERE status IN ('receiving','uploaded','validating')",
        "capture": "SELECT 1 FROM extension_recording_sessions "
        "WHERE state IN ('receiving','finalizing','archiving','archived')",
        "processing": "SELECT 1 FROM processing_workflows WHERE state IN ('waiting','running')",
        "task": "SELECT 1 FROM tasks WHERE status IN ('accepted','running','stopping')",
    }
    with closing(sqlite3.connect(data.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        for label, query in checks.items():
            if db.execute(query + " LIMIT 1").fetchone():
                raise SystemExit(f"Update deferred: pending {label}.")
    if any((Path(os.environ["ASR_INSTALL_DIR"]) / "requests").glob("*.json")):
        raise SystemExit("Update deferred: model installation.")


if __name__ == "__main__":
    require_idle()
