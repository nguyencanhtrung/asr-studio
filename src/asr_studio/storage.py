# ruff: noqa: E501

"""SQLite initialization for the durable app data volume."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 19


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


def _migrate_v16(connection: sqlite3.Connection) -> None:
    """Remember which processing service owns a claimed task."""
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN owner_role TEXT NOT NULL DEFAULT 'legacy' "
        "CHECK(owner_role IN ('legacy', 'transcription', 'notes'))"
    )


def _migrate_v17(connection: sqlite3.Connection) -> None:
    """Add durable device, pairing-code and session state for the extension API."""
    connection.execute(
        "CREATE TABLE extension_auth_installation ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), "
        "backend_id TEXT NOT NULL CHECK(length(backend_id) = 32)"
        ")"
    )
    connection.execute(
        "CREATE TABLE extension_devices ("
        "id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 16 AND 128), "
        "owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 16 AND 128), "
        "device_id TEXT CHECK(device_id IS NULL OR length(device_id) BETWEEN 1 AND 128), "
        "server_url TEXT CHECK(server_url IS NULL OR length(server_url) BETWEEN 1 AND 256), "
        "device_lifetime TEXT NOT NULL CHECK(device_lifetime IN ('7d', '30d', '90d', 'never')), "
        "device_expires_at TEXT, "
        "pending_expires_at TEXT, "
        "created_at TEXT NOT NULL, "
        "last_seen_at TEXT, "
        "revoked_at TEXT, "
        "generation INTEGER NOT NULL CHECK(generation >= 0), "
        "device_token_hash BLOB UNIQUE"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_devices_status_idx "
        "ON extension_devices(server_url, revoked_at, device_expires_at)"
    )
    connection.execute(
        "CREATE TABLE extension_pairing_codes ("
        "id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 16 AND 128), "
        "device_record_id TEXT NOT NULL REFERENCES extension_devices(id) ON DELETE RESTRICT, "
        "code_hash BLOB NOT NULL UNIQUE CHECK(length(code_hash) = 32), "
        "server_url TEXT CHECK(server_url IS NULL OR length(server_url) BETWEEN 1 AND 256), "
        "device_lifetime TEXT NOT NULL CHECK(device_lifetime IN ('7d', '30d', '90d', 'never')), "
        "created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, "
        "consumed_at TEXT, "
        "invalidated_at TEXT"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_pairing_codes_device_idx "
        "ON extension_pairing_codes(device_record_id, expires_at)"
    )
    connection.execute(
        "CREATE TABLE extension_sessions ("
        "token_hash BLOB PRIMARY KEY NOT NULL CHECK(length(token_hash) = 32), "
        "device_record_id TEXT NOT NULL REFERENCES extension_devices(id) ON DELETE RESTRICT, "
        "device_generation INTEGER NOT NULL CHECK(device_generation >= 1), "
        "server_url TEXT NOT NULL CHECK(length(server_url) BETWEEN 1 AND 256), "
        "scope TEXT NOT NULL CHECK(scope = 'ingest'), "
        "created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, "
        "revoked_at TEXT"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_sessions_device_idx "
        "ON extension_sessions(device_record_id, device_generation, expires_at)"
    )
    connection.execute(
        "CREATE TABLE extension_pairing_rate ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), "
        "window_started_at TEXT NOT NULL, "
        "attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0)"
        ")"
    )
    connection.execute(
        "INSERT INTO extension_pairing_rate(id, window_started_at, attempt_count) "
        "VALUES (1, CURRENT_TIMESTAMP, 0)"
    )


def _migrate_v18(connection: sqlite3.Connection) -> None:
    """Add source-aware extension ingest state and rebuild the legacy recording link."""
    connection.execute(
        "CREATE TABLE extension_recording_sessions ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), "
        "owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 16 AND 128), "
        "client_recording_id TEXT NOT NULL CHECK(length(client_recording_id) BETWEEN 1 AND 128), "
        "capture_epoch TEXT NOT NULL CHECK(length(capture_epoch) BETWEEN 1 AND 128), "
        "payload_fingerprint TEXT NOT NULL CHECK(length(payload_fingerprint) = 64), "
        "language_hint TEXT NOT NULL CHECK(language_hint IN ('en', 'vi')), "
        "language_hint_omitted INTEGER NOT NULL CHECK(language_hint_omitted IN (0, 1)), "
        "mime_type TEXT NOT NULL CHECK(mime_type = 'audio/webm;codecs=opus'), "
        "state TEXT NOT NULL CHECK(state IN ("
        "'receiving', 'finalizing', 'archiving', 'archived', 'ready', 'failed', 'canceled')), "
        "epoch INTEGER NOT NULL CHECK(epoch >= 1), "
        "next_sequence INTEGER NOT NULL CHECK(next_sequence >= 0), "
        "next_offset INTEGER NOT NULL CHECK(next_offset >= 0), "
        "finalize_id TEXT, "
        "finalize_payload_hash TEXT CHECK(finalize_payload_hash IS NULL OR "
        "length(finalize_payload_hash) = 64), "
        "finalize_total_bytes INTEGER CHECK(finalize_total_bytes IS NULL OR "
        "finalize_total_bytes >= 0), "
        "finalize_chunk_count INTEGER CHECK(finalize_chunk_count IS NULL OR "
        "finalize_chunk_count >= 0), "
        "finalize_capture_complete INTEGER CHECK(finalize_capture_complete IS NULL OR "
        "finalize_capture_complete IN (0, 1)), "
        "manifest_hash TEXT CHECK(manifest_hash IS NULL OR length(manifest_hash) = 64), "
        "raw_key TEXT UNIQUE, "
        "raw_bytes INTEGER CHECK(raw_bytes IS NULL OR raw_bytes >= 0), "
        "archive_sha256 TEXT CHECK(archive_sha256 IS NULL OR length(archive_sha256) = 64), "
        "receipt_at TEXT, "
        "error_code TEXT, "
        "error_message TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(owner_id, client_recording_id)"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_recording_sessions_state_idx "
        "ON extension_recording_sessions(state, updated_at, id)"
    )
    connection.execute(
        "CREATE TABLE extension_recording_chunks ("
        "session_id TEXT NOT NULL REFERENCES extension_recording_sessions(id) ON DELETE RESTRICT, "
        "sequence INTEGER NOT NULL CHECK(sequence >= 0), "
        "offset_bytes INTEGER NOT NULL CHECK(offset_bytes >= 0), "
        "length_bytes INTEGER NOT NULL CHECK(length_bytes > 0), "
        "sha256 TEXT NOT NULL CHECK(length(sha256) = 64), "
        "storage_key TEXT NOT NULL UNIQUE, "
        "committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(session_id, sequence), "
        "UNIQUE(session_id, offset_bytes)"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_recording_chunks_session_offset_idx "
        "ON extension_recording_chunks(session_id, offset_bytes, sequence)"
    )
    connection.execute(
        "CREATE TABLE extension_recording_reservations ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), "
        "session_id TEXT NOT NULL REFERENCES extension_recording_sessions(id) ON DELETE RESTRICT, "
        "sequence INTEGER NOT NULL CHECK(sequence >= 0), "
        "reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes > 0), "
        "temp_name TEXT NOT NULL CHECK(length(temp_name) BETWEEN 1 AND 255), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(session_id, sequence)"
        ")"
    )
    connection.execute(
        "CREATE INDEX extension_recording_reservations_session_idx "
        "ON extension_recording_reservations(session_id, sequence)"
    )
    connection.execute(
        "CREATE TABLE extension_recording_tombstones ("
        "owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 16 AND 128), "
        "client_recording_id TEXT NOT NULL CHECK(length(client_recording_id) BETWEEN 1 AND 128), "
        "session_id TEXT NOT NULL CHECK(length(session_id) = 32), "
        "payload_fingerprint TEXT NOT NULL CHECK(length(payload_fingerprint) = 64), "
        "state TEXT NOT NULL CHECK(state = 'deleted'), "
        "deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(owner_id, client_recording_id), "
        "UNIQUE(session_id)"
        ")"
    )
    connection.execute(
        "CREATE TABLE recordings_v18 ("
        "id TEXT PRIMARY KEY, "
        "meeting_id TEXT NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE RESTRICT, "
        "upload_id TEXT UNIQUE REFERENCES uploads(id) ON DELETE RESTRICT, "
        "extension_session_id TEXT UNIQUE REFERENCES "
        "extension_recording_sessions(id) ON DELETE RESTRICT, "
        "storage_key TEXT UNIQUE, "
        "content_type TEXT DEFAULT 'audio/mp4', "
        "duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds > 0), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "CHECK((upload_id IS NOT NULL AND extension_session_id IS NULL "
        "AND storage_key IS NOT NULL AND content_type IS NOT NULL "
        "AND duration_seconds IS NOT NULL AND duration_seconds > 0) OR "
        "(upload_id IS NULL AND extension_session_id IS NOT NULL AND "
        "((storage_key IS NULL AND content_type IS NULL AND duration_seconds IS NULL) OR "
        "(storage_key IS NOT NULL AND content_type IS NOT NULL "
        "AND duration_seconds IS NOT NULL AND duration_seconds > 0))))"
        ")"
    )
    connection.execute(
        "INSERT INTO recordings_v18(id, meeting_id, upload_id, storage_key, content_type, "
        "duration_seconds, created_at) "
        "SELECT id, meeting_id, upload_id, storage_key, content_type, duration_seconds, "
        "created_at FROM recordings"
    )
    connection.execute("DROP TABLE recordings")
    connection.execute("ALTER TABLE recordings_v18 RENAME TO recordings")
    connection.execute(
        "CREATE TRIGGER extension_recordings_ready_insert_guard "
        "BEFORE INSERT ON recordings WHEN NEW.extension_session_id IS NOT NULL AND "
        "(SELECT state FROM extension_recording_sessions "
        "WHERE id = NEW.extension_session_id) = 'ready' "
        "AND (NEW.storage_key IS NULL OR NEW.content_type IS NULL OR "
        "NEW.duration_seconds IS NULL OR NEW.duration_seconds <= 0) "
        "BEGIN SELECT RAISE(ABORT, 'extension_ready_recording_requires_derivative'); END"
    )
    connection.execute(
        "CREATE TRIGGER extension_recordings_ready_update_guard "
        "BEFORE UPDATE OF extension_session_id, storage_key, content_type, duration_seconds "
        "ON recordings WHEN NEW.extension_session_id IS NOT NULL AND "
        "(SELECT state FROM extension_recording_sessions "
        "WHERE id = NEW.extension_session_id) = 'ready' "
        "AND (NEW.storage_key IS NULL OR NEW.content_type IS NULL OR "
        "NEW.duration_seconds IS NULL OR NEW.duration_seconds <= 0) "
        "BEGIN SELECT RAISE(ABORT, 'extension_ready_recording_requires_derivative'); END"
    )
    connection.execute(
        "CREATE TRIGGER extension_recording_sessions_ready_insert_guard "
        "BEFORE INSERT ON extension_recording_sessions "
        "WHEN NEW.state = 'ready' AND NOT EXISTS ("
        "SELECT 1 FROM recordings WHERE extension_session_id = NEW.id "
        "AND storage_key IS NOT NULL AND content_type IS NOT NULL "
        "AND duration_seconds IS NOT NULL AND duration_seconds > 0) "
        "BEGIN SELECT RAISE(ABORT, 'extension_ready_recording_requires_derivative'); END"
    )
    connection.execute(
        "CREATE TRIGGER extension_recording_sessions_ready_guard "
        "BEFORE UPDATE OF state ON extension_recording_sessions "
        "WHEN NEW.state = 'ready' AND NOT EXISTS ("
        "SELECT 1 FROM recordings WHERE extension_session_id = NEW.id "
        "AND storage_key IS NOT NULL AND content_type IS NOT NULL "
        "AND duration_seconds IS NOT NULL AND duration_seconds > 0) "
        "BEGIN SELECT RAISE(ABORT, 'extension_ready_recording_requires_derivative'); END"
    )


def _migrate_v19(connection: sqlite3.Connection) -> None:
    """Add the durable automatic processing state machine.

    The tables deliberately keep workflow state separate from the existing task
    queue.  The latter remains the execution primitive used by the manual UI and
    workers; these rows are the durable intent/fence around it.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processing_preferences ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), "
        "asr_choice TEXT NOT NULL DEFAULT 'auto', "
        "asr_language TEXT NOT NULL DEFAULT 'vi' CHECK(asr_language IN ('en', 'vi')), "
        "notes_provider TEXT CHECK(notes_provider IS NULL OR notes_provider IN ('chatgpt', 'antigravity')), "
        "notes_model TEXT, notes_reasoning_effort TEXT, "
        "notes_output_language TEXT NOT NULL DEFAULT 'en' CHECK(notes_output_language IN ('en', 'vi')), "
        "version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute("INSERT OR IGNORE INTO processing_preferences(id) VALUES (1)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processing_workflows ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), "
        "meeting_id TEXT NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE RESTRICT, "
        "recording_id TEXT NOT NULL UNIQUE REFERENCES recordings(id) ON DELETE RESTRICT, "
        "extension_session_id TEXT UNIQUE REFERENCES extension_recording_sessions(id) ON DELETE RESTRICT, "
        "state TEXT NOT NULL CHECK(state IN ('waiting', 'running', 'succeeded', 'canceled', 'deleted', 'needs_attention')), "
        "epoch INTEGER NOT NULL DEFAULT 1 CHECK(epoch >= 1), "
        "ready_sequence INTEGER NOT NULL DEFAULT 0 CHECK(ready_sequence >= 0), "
        "asr_choice TEXT NOT NULL DEFAULT 'auto', "
        "asr_language TEXT NOT NULL CHECK(asr_language IN ('en', 'vi')), "
        "notes_provider TEXT CHECK(notes_provider IS NULL OR notes_provider IN ('chatgpt', 'antigravity')), "
        "notes_model TEXT, notes_reasoning_effort TEXT, notes_output_language TEXT NOT NULL DEFAULT 'en' CHECK(notes_output_language IN ('en', 'vi')), "
        "preferences_version INTEGER NOT NULL DEFAULT 1 CHECK(preferences_version >= 1), "
        "last_error_code TEXT, last_error_message TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS processing_workflows_queue_idx ON processing_workflows(state, ready_sequence, id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processing_steps ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), "
        "workflow_id TEXT NOT NULL REFERENCES processing_workflows(id) ON DELETE RESTRICT, "
        "kind TEXT NOT NULL CHECK(kind IN ('transcription', 'notes')), "
        "state TEXT NOT NULL CHECK(state IN ('waiting', 'accepted', 'running', 'succeeded', 'failed', 'canceled', 'needs_attention')), "
        "waiting_reason TEXT, next_eligible_at REAL NOT NULL DEFAULT 0, attempt_number INTEGER NOT NULL DEFAULT 0 CHECK(attempt_number >= 0), "
        "task_id TEXT, current_attempt_id TEXT, source_snapshot_id TEXT, source_generation_id TEXT, source_version INTEGER, notes_source_version INTEGER, metadata_version INTEGER, "
        "asr_choice TEXT, asr_language TEXT, asr_model TEXT, asr_model_path TEXT, device TEXT, compute_type TEXT, diarization_model_path TEXT, "
        "notes_provider TEXT, notes_model TEXT, notes_reasoning_effort TEXT, notes_output_language TEXT, notes_connection_version INTEGER, recipe_version INTEGER NOT NULL DEFAULT 1, "
        "last_error_code TEXT, last_error_message TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(workflow_id, kind)"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS processing_steps_ready_idx ON processing_steps(state, next_eligible_at, updated_at, id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processing_attempts ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), step_id TEXT NOT NULL REFERENCES processing_steps(id) ON DELETE RESTRICT, "
        "attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1), request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, workflow_epoch INTEGER NOT NULL CHECK(workflow_epoch >= 1), "
        "state TEXT NOT NULL CHECK(state IN ('waiting', 'accepted', 'running', 'succeeded', 'failed', 'canceled', 'interrupted')), task_id TEXT UNIQUE, execution_no INTEGER NOT NULL DEFAULT 1 CHECK(execution_no >= 1), "
        "error_code TEXT, error_message TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(step_id, attempt_number)"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS processing_attempts_step_idx ON processing_attempts(step_id, attempt_number DESC)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processing_outbox ("
        "id TEXT PRIMARY KEY CHECK(length(id) = 32), event_key TEXT NOT NULL UNIQUE, workflow_id TEXT NOT NULL REFERENCES processing_workflows(id) ON DELETE RESTRICT, "
        "event_type TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, processed_at TEXT"
        ")"
    )
    task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    for column in (
        "processing_workflow_id",
        "processing_step_id",
        "processing_attempt_id",
        "processing_epoch",
    ):
        if column not in task_columns:
            column_type = "INTEGER" if column == "processing_epoch" else "TEXT"
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")
    preference_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(processing_preferences)")
    }
    if "version" not in preference_columns:
        connection.execute(
            "ALTER TABLE processing_preferences ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    workflow_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(processing_workflows)")
    }
    if "preferences_version" not in workflow_columns:
        connection.execute(
            "ALTER TABLE processing_workflows ADD COLUMN preferences_version INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS tasks_processing_workflow_idx ON tasks(processing_workflow_id, processing_step_id)"
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
        if current_version < 16:
            _migrate_v16(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (16)")
        if current_version < 17:
            _migrate_v17(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (17)")
        connection.commit()
        if current_version < 18:
            # recordings changes its source and nullable derivative constraints. SQLite
            # requires the create-copy-drop-rename rebuild to happen with FK checks off.
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
                if current_version < 18:
                    _migrate_v18(connection)
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (18)")
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
        if current_version < 19:
            _migrate_v19(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (19)")
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return path
