"""SQLite initialization for the durable app data volume."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 15


def _migrate_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE meetings ("
        "id TEXT PRIMARY KEY, "
        "title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200), "
        "meeting_date TEXT CHECK(meeting_date IS NULL OR ("
        "length(meeting_date) = 10 AND "
        "meeting_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')), "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "metadata_version INTEGER NOT NULL DEFAULT 1 CHECK(metadata_version >= 1), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE TABLE uploads ("
        "id TEXT PRIMARY KEY, "
        "filename TEXT NOT NULL, "
        "declared_size INTEGER NOT NULL CHECK(declared_size > 0), "
        "status TEXT NOT NULL CHECK(status IN ("
        "'receiving', 'uploaded', 'validating', 'ready', 'failed', 'consumed')), "
        "raw_key TEXT NOT NULL UNIQUE, "
        "managed_key TEXT UNIQUE, "
        "content_started INTEGER NOT NULL DEFAULT 0 CHECK(content_started IN (0, 1)), "
        "duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds > 0), "
        "source_codec TEXT, "
        "failure_code TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute("CREATE INDEX uploads_status_idx ON uploads(status)")
    connection.execute(
        "CREATE TABLE recordings ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE RESTRICT, "
        "upload_id TEXT NOT NULL UNIQUE REFERENCES uploads(id) ON DELETE RESTRICT, "
        "storage_key TEXT NOT NULL UNIQUE, "
        "content_type TEXT NOT NULL DEFAULT 'audio/mp4', "
        "duration_seconds REAL NOT NULL CHECK(duration_seconds > 0), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )


def _migrate_v3(connection: sqlite3.Connection) -> None:
    # Bumped only when meeting_date changes; later Notes revisions compare it to
    # their source snapshot to detect staleness. Title edits never touch it.
    connection.execute(
        "ALTER TABLE meetings ADD COLUMN notes_source_version INTEGER NOT NULL DEFAULT 1 "
        "CHECK(notes_source_version >= 1)"
    )
    connection.execute("CREATE INDEX meetings_date_idx ON meetings(meeting_date)")


def _migrate_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, "
        "request_id TEXT NOT NULL UNIQUE, "
        "request_hash TEXT NOT NULL, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "kind TEXT NOT NULL CHECK(kind = 'transcription'), "
        "model_path TEXT NOT NULL, "
        "model_name TEXT NOT NULL, "
        "device TEXT NOT NULL CHECK(device IN ('auto', 'cpu', 'cuda')), "
        "compute_type TEXT NOT NULL, "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "status TEXT NOT NULL CHECK(status IN ("
        "'accepted', 'running', 'stopping', 'succeeded', 'failed', 'canceled', 'interrupted')), "
        "stage TEXT NOT NULL, "
        "result_scope TEXT CHECK(result_scope IS NULL OR result_scope = 'transcript_only'), "
        "audio_duration_seconds REAL NOT NULL CHECK(audio_duration_seconds > 0), "
        "transcript_end_seconds REAL NOT NULL DEFAULT 0 CHECK(transcript_end_seconds >= 0), "
        "segment_count INTEGER NOT NULL DEFAULT 0 CHECK(segment_count >= 0), "
        "elapsed_seconds REAL NOT NULL DEFAULT 0 CHECK(elapsed_seconds >= 0), "
        "owner_id TEXT, "
        "error_code TEXT, "
        "error_message TEXT, "
        "accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "started_at TEXT, "
        "heartbeat_at TEXT, "
        "cancel_requested_at TEXT, "
        "completed_at TEXT, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE INDEX tasks_meeting_created_idx ON tasks(meeting_id, accepted_at DESC, id DESC)"
    )
    connection.execute(
        "CREATE TABLE transcript_generations ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT, "
        "generation_number INTEGER NOT NULL CHECK(generation_number >= 1), "
        "status TEXT NOT NULL CHECK(status = 'complete'), "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "model_name TEXT NOT NULL, "
        "language_probability REAL CHECK(language_probability IS NULL OR "
        "(language_probability >= 0 AND language_probability <= 1)), "
        "source_version INTEGER NOT NULL DEFAULT 1 CHECK(source_version >= 1), "
        "uncertainty_policy TEXT NOT NULL, "
        "segment_count INTEGER NOT NULL CHECK(segment_count >= 0), "
        "completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(meeting_id, generation_number)"
        ")"
    )
    connection.execute(
        "CREATE INDEX transcript_generations_meeting_idx "
        "ON transcript_generations(meeting_id, generation_number DESC)"
    )
    connection.execute(
        "CREATE TABLE transcript_segments ("
        "id TEXT PRIMARY KEY, "
        "generation_id TEXT NOT NULL REFERENCES transcript_generations(id) ON DELETE RESTRICT, "
        "sequence INTEGER NOT NULL CHECK(sequence >= 0), "
        "start_seconds REAL NOT NULL CHECK(start_seconds >= 0), "
        "end_seconds REAL NOT NULL CHECK(end_seconds >= start_seconds), "
        "text TEXT NOT NULL, "
        "original_speaker_id TEXT, "
        "speaker_id TEXT, "
        "avg_logprob REAL, "
        "no_speech_prob REAL CHECK(no_speech_prob IS NULL OR "
        "(no_speech_prob >= 0 AND no_speech_prob <= 1)), "
        "compression_ratio REAL CHECK(compression_ratio IS NULL OR compression_ratio >= 0), "
        "uncertain INTEGER NOT NULL CHECK(uncertain IN (0, 1)), "
        "UNIQUE(generation_id, sequence)"
        ")"
    )
    connection.execute(
        "CREATE INDEX transcript_segments_generation_idx "
        "ON transcript_segments(generation_id, sequence)"
    )
    connection.execute(
        "CREATE TABLE processing_slot ("
        "slot_id INTEGER PRIMARY KEY CHECK(slot_id = 1), "
        "active_task_id TEXT UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute("INSERT INTO processing_slot(slot_id, active_task_id) VALUES (1, NULL)")
    connection.execute(
        "ALTER TABLE meetings ADD COLUMN current_generation_id TEXT "
        "REFERENCES transcript_generations(id) ON DELETE SET NULL"
    )


def _migrate_v5(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN diarization_model_path TEXT NOT NULL DEFAULT ''"
    )
    connection.execute("ALTER TABLE transcript_generations ADD COLUMN diarization_model_name TEXT")
    connection.execute(
        "ALTER TABLE transcript_generations ADD COLUMN regular_turn_count INTEGER NOT NULL "
        "DEFAULT 0 CHECK(regular_turn_count >= 0)"
    )
    connection.execute(
        "ALTER TABLE transcript_generations ADD COLUMN exclusive_turn_count INTEGER NOT NULL "
        "DEFAULT 0 CHECK(exclusive_turn_count >= 0)"
    )
    connection.execute("ALTER TABLE transcript_generations ADD COLUMN alignment_policy TEXT")
    connection.execute(
        "ALTER TABLE transcript_segments ADD COLUMN review_reasons TEXT NOT NULL DEFAULT '[]' "
        "CHECK(json_valid(review_reasons))"
    )
    connection.execute(
        "ALTER TABLE transcript_segments ADD COLUMN overlap INTEGER NOT NULL DEFAULT 0 "
        "CHECK(overlap IN (0, 1))"
    )
    connection.execute(
        "ALTER TABLE transcript_segments ADD COLUMN speaker_dominance REAL CHECK("
        "speaker_dominance IS NULL OR (speaker_dominance >= 0 AND speaker_dominance <= 1))"
    )
    connection.execute(
        "ALTER TABLE transcript_segments ADD COLUMN assignment_version INTEGER NOT NULL "
        "DEFAULT 1 CHECK(assignment_version >= 1)"
    )
    connection.execute(
        "CREATE TABLE generation_speakers ("
        "generation_id TEXT NOT NULL REFERENCES transcript_generations(id) ON DELETE RESTRICT, "
        "speaker_id TEXT NOT NULL CHECK(length(trim(speaker_id)) BETWEEN 1 AND 80), "
        "display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 80), "
        "first_seen_seconds REAL NOT NULL CHECK(first_seen_seconds >= 0), "
        "last_seen_seconds REAL NOT NULL CHECK(last_seen_seconds >= first_seen_seconds), "
        "PRIMARY KEY(generation_id, speaker_id), "
        "UNIQUE(generation_id, display_name)"
        ")"
    )
    connection.execute(
        "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
        "first_seen_seconds, last_seen_seconds) "
        "SELECT generation_id, speaker_id, speaker_id, MIN(start_seconds), MAX(end_seconds) "
        "FROM ("
        "SELECT generation_id, original_speaker_id AS speaker_id, start_seconds, end_seconds "
        "FROM transcript_segments WHERE original_speaker_id IS NOT NULL "
        "UNION ALL "
        "SELECT generation_id, speaker_id, start_seconds, end_seconds "
        "FROM transcript_segments WHERE speaker_id IS NOT NULL"
        ") GROUP BY generation_id, speaker_id"
    )
    connection.execute(
        "CREATE TRIGGER transcript_segments_speakers_insert "
        "BEFORE INSERT ON transcript_segments WHEN "
        "(NEW.original_speaker_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM generation_speakers WHERE generation_id = NEW.generation_id "
        "AND speaker_id = NEW.original_speaker_id)) OR "
        "(NEW.speaker_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM generation_speakers WHERE generation_id = NEW.generation_id "
        "AND speaker_id = NEW.speaker_id)) "
        "BEGIN SELECT RAISE(ABORT, 'speaker_generation_mismatch'); END"
    )
    connection.execute(
        "CREATE TRIGGER transcript_segments_speakers_update "
        "BEFORE UPDATE OF generation_id, original_speaker_id, speaker_id "
        "ON transcript_segments WHEN "
        "(NEW.generation_id != OLD.generation_id) OR "
        "(NEW.original_speaker_id IS NOT OLD.original_speaker_id) OR "
        "(NEW.original_speaker_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM generation_speakers WHERE generation_id = NEW.generation_id "
        "AND speaker_id = NEW.original_speaker_id)) OR "
        "(NEW.speaker_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM generation_speakers WHERE generation_id = NEW.generation_id "
        "AND speaker_id = NEW.speaker_id)) "
        "BEGIN SELECT RAISE(ABORT, 'speaker_generation_mismatch'); END"
    )
    connection.execute(
        "CREATE TRIGGER generation_speakers_delete_guard BEFORE DELETE ON generation_speakers "
        "BEGIN SELECT RAISE(ABORT, 'generation_speakers_are_immutable'); END"
    )


def _migrate_v6(connection: sqlite3.Connection) -> None:
    """Add immutable notes sources and revisions, then extend the shared task table."""
    connection.execute(
        "CREATE TABLE notes_sources ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "generation_id TEXT NOT NULL REFERENCES transcript_generations(id) ON DELETE RESTRICT, "
        "source_version INTEGER NOT NULL CHECK(source_version >= 1), "
        "notes_source_version INTEGER NOT NULL CHECK(notes_source_version >= 1), "
        "meeting_date TEXT CHECK(meeting_date IS NULL OR ("
        "length(meeting_date) = 10 AND meeting_date GLOB "
        "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')), "
        "snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE INDEX notes_sources_meeting_idx "
        "ON notes_sources(meeting_id, created_at DESC, id DESC)"
    )

    connection.execute(
        "CREATE TABLE tasks_v6 ("
        "id TEXT PRIMARY KEY, "
        "request_id TEXT NOT NULL UNIQUE, "
        "request_hash TEXT NOT NULL, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "kind TEXT NOT NULL CHECK(kind IN ('transcription', 'notes')), "
        "model_path TEXT NOT NULL, "
        "model_name TEXT NOT NULL, "
        "device TEXT NOT NULL CHECK(device IN ('auto', 'cpu', 'cuda')), "
        "compute_type TEXT NOT NULL, "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "status TEXT NOT NULL CHECK(status IN ("
        "'accepted', 'running', 'stopping', 'succeeded', 'failed', 'canceled', 'interrupted')), "
        "stage TEXT NOT NULL, "
        "result_scope TEXT CHECK(result_scope IS NULL OR result_scope IN "
        "('transcript_only', 'notes_only')), "
        "audio_duration_seconds REAL NOT NULL CHECK(audio_duration_seconds > 0), "
        "transcript_end_seconds REAL NOT NULL DEFAULT 0 CHECK(transcript_end_seconds >= 0), "
        "segment_count INTEGER NOT NULL DEFAULT 0 CHECK(segment_count >= 0), "
        "elapsed_seconds REAL NOT NULL DEFAULT 0 CHECK(elapsed_seconds >= 0), "
        "owner_id TEXT, "
        "error_code TEXT, "
        "error_message TEXT, "
        "accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "started_at TEXT, "
        "heartbeat_at TEXT, "
        "cancel_requested_at TEXT, "
        "completed_at TEXT, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "diarization_model_path TEXT NOT NULL DEFAULT '', "
        "notes_provider TEXT CHECK(notes_provider IS NULL OR notes_provider = 'chatgpt'), "
        "notes_connection_version INTEGER CHECK(notes_connection_version IS NULL OR "
        "notes_connection_version >= 0), "
        "notes_model TEXT, "
        "notes_reasoning_effort TEXT, "
        "notes_prompt_version TEXT, "
        "notes_schema_version INTEGER CHECK(notes_schema_version IS NULL OR "
        "notes_schema_version >= 1), "
        "notes_source_id TEXT REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "CHECK((kind = 'transcription' AND notes_provider IS NULL AND "
        "notes_connection_version IS NULL AND notes_model IS NULL AND "
        "notes_reasoning_effort IS NULL AND notes_prompt_version IS NULL AND "
        "notes_schema_version IS NULL AND notes_source_id IS NULL) OR "
        "(kind = 'notes' AND notes_provider = 'chatgpt' AND "
        "notes_connection_version IS NOT NULL AND notes_model IS NOT NULL AND "
        "notes_reasoning_effort IS NOT NULL AND notes_prompt_version IS NOT NULL AND "
        "notes_schema_version IS NOT NULL AND notes_source_id IS NOT NULL))"
        ")"
    )
    connection.execute(
        "INSERT INTO tasks_v6(id, request_id, request_hash, meeting_id, kind, model_path, "
        "model_name, device, compute_type, language_hint, status, stage, result_scope, "
        "audio_duration_seconds, transcript_end_seconds, segment_count, elapsed_seconds, "
        "owner_id, error_code, error_message, accepted_at, started_at, heartbeat_at, "
        "cancel_requested_at, completed_at, updated_at, diarization_model_path) "
        "SELECT id, request_id, request_hash, meeting_id, kind, model_path, model_name, device, "
        "compute_type, language_hint, status, stage, result_scope, audio_duration_seconds, "
        "transcript_end_seconds, segment_count, elapsed_seconds, owner_id, error_code, "
        "error_message, accepted_at, started_at, heartbeat_at, cancel_requested_at, completed_at, "
        "updated_at, diarization_model_path FROM tasks"
    )
    connection.execute("DROP TABLE tasks")
    connection.execute("ALTER TABLE tasks_v6 RENAME TO tasks")
    connection.execute(
        "CREATE INDEX tasks_meeting_created_idx ON tasks(meeting_id, accepted_at DESC, id DESC)"
    )

    connection.execute(
        "CREATE TABLE notes_revisions ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "source_snapshot_id TEXT NOT NULL REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT, "
        "markdown TEXT NOT NULL, "
        "edit_version INTEGER NOT NULL DEFAULT 1 CHECK(edit_version >= 1), "
        "provider TEXT NOT NULL CHECK(provider = 'chatgpt'), "
        "model TEXT NOT NULL, "
        "reasoning_effort TEXT NOT NULL, "
        "connection_version INTEGER NOT NULL CHECK(connection_version >= 0), "
        "prompt_version TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version >= 1), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE INDEX notes_revisions_meeting_idx "
        "ON notes_revisions(meeting_id, created_at DESC, id DESC)"
    )


def _migrate_v7(connection: sqlite3.Connection) -> None:
    """Pin each notes task and revision to its requested output language."""
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN notes_output_language TEXT NOT NULL DEFAULT 'en' "
        "CHECK(notes_output_language IN ('en', 'vi'))"
    )
    connection.execute(
        "ALTER TABLE notes_revisions ADD COLUMN output_language TEXT NOT NULL DEFAULT 'en' "
        "CHECK(output_language IN ('en', 'vi'))"
    )


_TASKS_V8_COLUMNS = (
    "id, request_id, request_hash, meeting_id, kind, model_path, model_name, device, "
    "compute_type, language_hint, status, stage, result_scope, audio_duration_seconds, "
    "transcript_end_seconds, segment_count, elapsed_seconds, owner_id, error_code, "
    "error_message, accepted_at, started_at, heartbeat_at, cancel_requested_at, "
    "completed_at, updated_at, diarization_model_path, notes_provider, "
    "notes_connection_version, notes_model, notes_reasoning_effort, notes_prompt_version, "
    "notes_schema_version, notes_source_id, notes_output_language"
)


def _migrate_v8(connection: sqlite3.Connection) -> None:
    """Allow Antigravity as a notes provider and remember the last chosen provider."""
    connection.execute(
        "CREATE TABLE tasks_v8 ("
        "id TEXT PRIMARY KEY, "
        "request_id TEXT NOT NULL UNIQUE, "
        "request_hash TEXT NOT NULL, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "kind TEXT NOT NULL CHECK(kind IN ('transcription', 'notes')), "
        "model_path TEXT NOT NULL, "
        "model_name TEXT NOT NULL, "
        "device TEXT NOT NULL CHECK(device IN ('auto', 'cpu', 'cuda')), "
        "compute_type TEXT NOT NULL, "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "status TEXT NOT NULL CHECK(status IN ("
        "'accepted', 'running', 'stopping', 'succeeded', 'failed', 'canceled', 'interrupted')), "
        "stage TEXT NOT NULL, "
        "result_scope TEXT CHECK(result_scope IS NULL OR result_scope IN "
        "('transcript_only', 'notes_only')), "
        "audio_duration_seconds REAL NOT NULL CHECK(audio_duration_seconds > 0), "
        "transcript_end_seconds REAL NOT NULL DEFAULT 0 CHECK(transcript_end_seconds >= 0), "
        "segment_count INTEGER NOT NULL DEFAULT 0 CHECK(segment_count >= 0), "
        "elapsed_seconds REAL NOT NULL DEFAULT 0 CHECK(elapsed_seconds >= 0), "
        "owner_id TEXT, "
        "error_code TEXT, "
        "error_message TEXT, "
        "accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "started_at TEXT, "
        "heartbeat_at TEXT, "
        "cancel_requested_at TEXT, "
        "completed_at TEXT, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "diarization_model_path TEXT NOT NULL DEFAULT '', "
        "notes_provider TEXT CHECK(notes_provider IS NULL OR "
        "notes_provider IN ('chatgpt', 'antigravity')), "
        "notes_connection_version INTEGER CHECK(notes_connection_version IS NULL OR "
        "notes_connection_version >= 0), "
        "notes_model TEXT, "
        "notes_reasoning_effort TEXT, "
        "notes_prompt_version TEXT, "
        "notes_schema_version INTEGER CHECK(notes_schema_version IS NULL OR "
        "notes_schema_version >= 1), "
        "notes_source_id TEXT REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "notes_output_language TEXT NOT NULL DEFAULT 'en' "
        "CHECK(notes_output_language IN ('en', 'vi')), "
        "CHECK((kind = 'transcription' AND notes_provider IS NULL AND "
        "notes_connection_version IS NULL AND notes_model IS NULL AND "
        "notes_reasoning_effort IS NULL AND notes_prompt_version IS NULL AND "
        "notes_schema_version IS NULL AND notes_source_id IS NULL) OR "
        "(kind = 'notes' AND notes_provider IN ('chatgpt', 'antigravity') AND "
        "notes_connection_version IS NOT NULL AND notes_model IS NOT NULL AND "
        "notes_reasoning_effort IS NOT NULL AND notes_prompt_version IS NOT NULL AND "
        "notes_schema_version IS NOT NULL AND notes_source_id IS NOT NULL))"
        ")"
    )
    connection.execute(
        f"INSERT INTO tasks_v8({_TASKS_V8_COLUMNS}) SELECT {_TASKS_V8_COLUMNS} FROM tasks"
    )
    connection.execute("DROP TABLE tasks")
    connection.execute("ALTER TABLE tasks_v8 RENAME TO tasks")
    connection.execute(
        "CREATE INDEX tasks_meeting_created_idx ON tasks(meeting_id, accepted_at DESC, id DESC)"
    )

    connection.execute(
        "CREATE TABLE notes_revisions_v8 ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT, "
        "source_snapshot_id TEXT NOT NULL REFERENCES notes_sources(id) ON DELETE RESTRICT, "
        "task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT, "
        "markdown TEXT NOT NULL, "
        "edit_version INTEGER NOT NULL DEFAULT 1 CHECK(edit_version >= 1), "
        "provider TEXT NOT NULL CHECK(provider IN ('chatgpt', 'antigravity')), "
        "model TEXT NOT NULL, "
        "reasoning_effort TEXT NOT NULL, "
        "connection_version INTEGER NOT NULL CHECK(connection_version >= 0), "
        "prompt_version TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version >= 1), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "output_language TEXT NOT NULL DEFAULT 'en' CHECK(output_language IN ('en', 'vi'))"
        ")"
    )
    revision_columns = (
        "id, meeting_id, source_snapshot_id, task_id, markdown, edit_version, provider, model, "
        "reasoning_effort, connection_version, prompt_version, schema_version, created_at, "
        "updated_at, output_language"
    )
    connection.execute(
        f"INSERT INTO notes_revisions_v8({revision_columns}) "
        f"SELECT {revision_columns} FROM notes_revisions"
    )
    connection.execute("DROP TABLE notes_revisions")
    connection.execute("ALTER TABLE notes_revisions_v8 RENAME TO notes_revisions")
    connection.execute(
        "CREATE INDEX notes_revisions_meeting_idx "
        "ON notes_revisions(meeting_id, created_at DESC, id DESC)"
    )

    connection.execute(
        "CREATE TABLE notes_preferences ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), "
        "provider TEXT CHECK(provider IS NULL OR provider IN ('chatgpt', 'antigravity'))"
        ")"
    )
    connection.execute("INSERT INTO notes_preferences(id, provider) VALUES (1, NULL)")


def _migrate_v9(connection: sqlite3.Connection) -> None:
    """Keep the coverage manifest that shows which source parts a notes revision processed."""
    connection.execute(
        "ALTER TABLE notes_revisions ADD COLUMN coverage_json TEXT "
        "CHECK(coverage_json IS NULL OR json_valid(coverage_json))"
    )


def _migrate_v10(connection: sqlite3.Connection) -> None:
    """Keep the timestamp citations of each notes revision apart from its Markdown."""
    # NULL marks a revision made before citations were kept: it shows none, and no row is rewritten.
    connection.execute(
        "ALTER TABLE notes_revisions ADD COLUMN citations_json TEXT "
        "CHECK(citations_json IS NULL OR json_valid(citations_json))"
    )


def _migrate_v11(connection: sqlite3.Connection) -> None:
    """Make a notes source snapshot unchangeable in the database, not only by convention."""
    connection.execute(
        "CREATE TRIGGER notes_sources_update_guard BEFORE UPDATE ON notes_sources "
        "BEGIN SELECT RAISE(ABORT, 'notes_sources_are_immutable'); END"
    )


def _migrate_v12(connection: sqlite3.Connection) -> None:
    """Persist restartable meeting deletion and scope speaker removal to marked meetings."""
    connection.execute("ALTER TABLE meetings ADD COLUMN deletion_requested_at TEXT")
    connection.execute("ALTER TABLE meetings ADD COLUMN deletion_error_code TEXT")
    connection.execute("DROP TRIGGER generation_speakers_delete_guard")
    connection.execute(
        "CREATE TRIGGER generation_speakers_delete_guard BEFORE DELETE ON generation_speakers "
        "WHEN NOT EXISTS ("
        "SELECT 1 FROM transcript_generations AS generations "
        "JOIN meetings ON meetings.id = generations.meeting_id "
        "WHERE generations.id = OLD.generation_id "
        "AND meetings.deletion_requested_at IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'generation_speakers_are_immutable'); END"
    )


def _migrate_v13(connection: sqlite3.Connection) -> None:
    """Track the last upload chunk committed to durable storage."""
    connection.execute(
        "ALTER TABLE uploads ADD COLUMN received_bytes INTEGER NOT NULL DEFAULT 0 "
        "CHECK(received_bytes >= 0 AND received_bytes <= declared_size)"
    )


def _migrate_v14(connection: sqlite3.Connection) -> None:
    """Persist bounded notes-pipeline progress independently of transcript timing."""
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0 "
        "CHECK(progress_current >= 0)"
    )
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN progress_total INTEGER "
        "CHECK(progress_total IS NULL OR progress_total >= 0)"
    )
    connection.execute("ALTER TABLE tasks ADD COLUMN progress_unit TEXT")


def _migrate_v15(connection: sqlite3.Connection) -> None:
    """Record each run's chosen ASR model and the runtime its transcript really came from."""
    connection.execute("ALTER TABLE tasks ADD COLUMN asr_model TEXT NOT NULL DEFAULT 'auto'")
    connection.execute("ALTER TABLE transcript_generations ADD COLUMN device TEXT")
    connection.execute("ALTER TABLE transcript_generations ADD COLUMN compute_type TEXT")
    connection.execute(
        "UPDATE transcript_generations SET "
        "device = (SELECT device FROM tasks WHERE tasks.id = transcript_generations.task_id), "
        "compute_type = (SELECT compute_type FROM tasks "
        "WHERE tasks.id = transcript_generations.task_id)"
    )


def database_path(data_dir: Path) -> Path:
    return Path(data_dir) / "app.sqlite3"


def initialize(data_dir: Path) -> Path:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = database_path(directory)
    if path.is_symlink():
        raise RuntimeError("SQLite database path must not be a symbolic link")
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    else:
        os.chmod(path, 0o600)

    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        current_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this application")
        if current_version < 2:
            _migrate_v2(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        if current_version < 3:
            _migrate_v3(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
        if current_version < 4:
            _migrate_v4(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
        if current_version < 5:
            _migrate_v5(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
        connection.commit()
        if current_version < 6:
            # SQLite cannot change foreign_keys inside a transaction. The standard
            # create-copy-drop-rename rebuild keeps child references pointed at `tasks`.
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
                if current_version < 6:
                    _migrate_v6(connection)
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (6)")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("Database migration produced foreign-key violations")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        current_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if current_version < 7:
            _migrate_v7(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (7)")
        connection.commit()
        if current_version < 8:
            # Same rebuild pattern as v6: child tables keep pointing at the new `tasks`.
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
                if current_version < 8:
                    _migrate_v8(connection)
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (8)")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("Database migration produced foreign-key violations")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        current_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if current_version < 9:
            _migrate_v9(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (9)")
        if current_version < 10:
            _migrate_v10(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (10)")
        if current_version < 11:
            _migrate_v11(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (11)")
        if current_version < 12:
            _migrate_v12(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (12)")
        if current_version < 13:
            _migrate_v13(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (13)")
        if current_version < 14:
            _migrate_v14(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (14)")
        if current_version < 15:
            _migrate_v15(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (15)")
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return path
