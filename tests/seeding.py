"""Insert meetings straight into SQLite so history tests need no media tools."""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
from pathlib import Path

from media_tools import require_media_tools

from asr_studio.diarization_runner import COMMUNITY_1_REQUIRED_FILES
from asr_studio.storage import database_path


def prepare_diarization_model(path: Path) -> Path:
    """Create the minimal Community-1 file layout accepted by local preflight tests."""
    path.mkdir(parents=True, exist_ok=True)
    for relative_path in COMMUNITY_1_REQUIRED_FILES:
        target = path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = b"pipeline: {}\n" if relative_path.name == "config.yaml" else b"model-data"
        target.write_bytes(content)
    return path


def seed_meeting(
    data_dir: Path,
    title: str,
    meeting_date: str | None = None,
    *,
    language_hint: str = "en",
    duration_seconds: float = 1.0,
) -> str:
    meeting_id = uuid.uuid4().hex
    upload_id = uuid.uuid4().hex
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO uploads(id, filename, declared_size, status, raw_key, managed_key) "
            "VALUES (?, 'seed.wav', 1, 'consumed', ?, ?)",
            (upload_id, f"uploads/{upload_id}.part", f"recordings/{upload_id}.m4a"),
        )
        connection.execute(
            "INSERT INTO meetings(id, title, meeting_date, language_hint) VALUES (?, ?, ?, ?)",
            (meeting_id, title, meeting_date, language_hint),
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, upload_id, storage_key, duration_seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                meeting_id,
                upload_id,
                f"recordings/{upload_id}.m4a",
                duration_seconds,
            ),
        )
    connection.close()
    return meeting_id


def prepare_recording(data_dir: Path, meeting_id: str) -> Path:
    """Give a seeded meeting a short, playable managed recording."""
    require_media_tools()
    with sqlite3.connect(database_path(data_dir)) as connection:
        (key,) = connection.execute(
            "SELECT storage_key FROM recordings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
    audio = data_dir / key
    audio.parent.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "0.25",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return audio
