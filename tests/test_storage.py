from __future__ import annotations

import os
import sqlite3

import pytest

from asr_studio.notes import get_note, notes_state
from asr_studio.storage import (
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    _migrate_v5,
    _migrate_v6,
    _migrate_v7,
    _migrate_v8,
    _migrate_v9,
    _migrate_v10,
    initialize,
)


def _create_legacy_database(database, *, version: int) -> None:
    """Build a real v4/v5 schema with transcript rows before testing migration."""
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        _migrate_v2(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        _migrate_v3(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
        _migrate_v4(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
        connection.execute(
            "INSERT INTO uploads(id, filename, declared_size, status, raw_key, managed_key) "
            "VALUES ('old-upload', 'old.wav', 1, 'consumed', 'uploads/old.part', "
            "'recordings/old.m4a')"
        )
        connection.execute(
            "INSERT INTO meetings(id, title, meeting_date, language_hint) "
            "VALUES ('old-meeting', 'Legacy meeting', '2024-05-06', 'en')"
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, upload_id, storage_key, duration_seconds) "
            "VALUES ('old-recording', 'old-meeting', 'old-upload', "
            "'recordings/old.m4a', 12.0)"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, transcript_end_seconds, segment_count) "
            "VALUES ('old-task', 'old-request', 'old-hash', 'old-meeting', 'transcription', "
            "'/models/whisper-small', 'whisper-small', 'cpu', 'int8', 'en', 'succeeded', "
            "'complete', 'transcript_only', 12.0, 2.5, 1)"
        )
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, language_probability, uncertainty_policy, "
            "segment_count) VALUES ('old-generation', 'old-meeting', 'old-task', 1, "
            "'complete', 'en', 'whisper-small', 0.9, 'legacy-policy', 1)"
        )
        connection.execute(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, uncertain) VALUES "
            "('old-segment', 'old-generation', 0, 1.25, 2.5, 'Kept text.', "
            "'legacy-speaker', 'legacy-speaker', 0)"
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = 'old-generation' WHERE id = 'old-meeting'"
        )
        if version >= 5:
            _migrate_v5(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
            connection.execute(
                "UPDATE tasks SET diarization_model_path = '/models/community-1' "
                "WHERE id = 'old-task'"
            )
            connection.execute(
                "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
                "model_name, device, compute_type, language_hint, status, stage, "
                "audio_duration_seconds, diarization_model_path) VALUES ('active-task', "
                "'active-request', 'active-hash', 'old-meeting', 'transcription', "
                "'/models/whisper-small', 'whisper-small', 'cpu', 'int8', 'en', 'accepted', "
                "'accepted', 12.0, '/models/community-1')"
            )
            connection.execute(
                "UPDATE processing_slot SET active_task_id = 'active-task' WHERE slot_id = 1"
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sqlite_persists_across_reopen_with_current_domain_schema(tmp_path):
    data_dir = tmp_path / "persistent-data"
    database = initialize(data_dir)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE acceptance_seed (value TEXT NOT NULL)")
        connection.execute("INSERT INTO acceptance_seed(value) VALUES ('persisted')")

    reopened = initialize(data_dir)

    assert reopened == database
    with sqlite3.connect(reopened) as connection:
        assert connection.execute("SELECT value FROM acceptance_seed").fetchone() == ("persisted",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    assert tables == {
        "schema_migrations",
        "acceptance_seed",
        "meetings",
        "uploads",
        "recordings",
        "tasks",
        "processing_slot",
        "transcript_generations",
        "transcript_segments",
        "generation_speakers",
        "notes_sources",
        "notes_revisions",
        "notes_preferences",
    }
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    with sqlite3.connect(database) as connection:
        meeting_columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings)")}
    assert {"deletion_requested_at", "deletion_error_code"}.issubset(meeting_columns)
    assert os.stat(data_dir).st_mode & 0o777 == 0o700
    assert os.stat(database).st_mode & 0o777 == 0o600


def test_schema_v1_database_migrates_transactionally_and_preserves_existing_data(tmp_path):
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute("CREATE TABLE legacy_values (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_values(value) VALUES ('kept')")

    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM legacy_values").fetchone() == ("kept",)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
        ]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "meetings",
            "uploads",
            "recordings",
            "tasks",
            "processing_slot",
            "transcript_generations",
            "transcript_segments",
            "generation_speakers",
            "notes_sources",
            "notes_revisions",
            "notes_preferences",
        }.issubset(tables)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v4_migration_preserves_existing_generation_and_backfills_its_speaker_catalog(
    tmp_path,
):
    data_dir = tmp_path / "version-four-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=4)

    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = 'old-meeting'"
        ).fetchone() == ("old-generation",)
        assert connection.execute(
            "SELECT text, start_seconds, end_seconds, original_speaker_id, speaker_id "
            "FROM transcript_segments WHERE id = 'old-segment'"
        ).fetchone() == ("Kept text.", 1.25, 2.5, "legacy-speaker", "legacy-speaker")
        assert connection.execute(
            "SELECT speaker_id, display_name, first_seen_seconds, last_seen_seconds "
            "FROM generation_speakers WHERE generation_id = 'old-generation'"
        ).fetchall() == [("legacy-speaker", "legacy-speaker", 1.25, 2.5)]
        assert connection.execute(
            "SELECT status, model_path, diarization_model_path FROM tasks WHERE id = 'old-task'"
        ).fetchone() == ("succeeded", "/models/whisper-small", "")
        assert connection.execute(
            "SELECT declared_size, received_bytes FROM uploads WHERE id = 'old-upload'"
        ).fetchone() == (1, 0)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v5_rebuild_preserves_tasks_transcript_rows_and_processing_slot(tmp_path):
    data_dir = tmp_path / "version-five-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)

    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, status, result_scope, diarization_model_path FROM tasks "
            "WHERE id = 'old-task'"
        ).fetchone() == (
            "old-task",
            "succeeded",
            "transcript_only",
            "/models/community-1",
        )
        assert connection.execute(
            "SELECT id, status, request_hash FROM tasks WHERE id = 'active-task'"
        ).fetchone() == ("active-task", "accepted", "active-hash")
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == ("active-task",)
        assert connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = 'old-meeting'"
        ).fetchone() == ("old-generation",)
        assert connection.execute(
            "SELECT text, review_reasons, overlap, assignment_version FROM transcript_segments "
            "WHERE id = 'old-segment'"
        ).fetchone() == ("Kept text.", "[]", 0, 1)
        assert connection.execute(
            "SELECT speaker_id, display_name FROM generation_speakers "
            "WHERE generation_id = 'old-generation'"
        ).fetchall() == [("legacy-speaker", "legacy-speaker")]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v6_migration_backfills_existing_notes_as_english(tmp_path):
    data_dir = tmp_path / "version-six-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        _migrate_v6(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (6)")
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('old-source', 'old-meeting', 'old-generation', 1, 1, '2024-05-06', '{}')"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_prompt_version, notes_schema_version, notes_source_id) "
            "VALUES ('old-notes-task', 'old-notes-request', 'old-notes-hash', 'old-meeting', "
            "'notes', '', 'gpt-6.1-sol', 'auto', 'provider', 'en', 'succeeded', 'succeeded', "
            "'notes_only', 12.0, 'chatgpt', 3, 'gpt-6.1-sol', 'high', "
            "'notes-hierarchical-v4', 1, 'old-source')"
        )
        connection.execute(
            "INSERT INTO notes_revisions(id, meeting_id, source_snapshot_id, task_id, markdown, "
            "provider, model, reasoning_effort, connection_version, prompt_version, "
            "schema_version) "
            "VALUES ('old-revision', 'old-meeting', 'old-source', 'old-notes-task', ?, "
            "'chatgpt', 'gpt-6.1-sol', 'high', 3, 'notes-hierarchical-v4', 1)",
            ("## Overview\n\nLegacy English notes.",),
        )

    initialize(data_dir)
    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT notes_output_language FROM tasks WHERE id = 'old-notes-task'"
        ).fetchone() == ("en",)
        assert connection.execute(
            "SELECT output_language, markdown FROM notes_revisions WHERE id = 'old-revision'"
        ).fetchone() == ("en", "## Overview\n\nLegacy English notes.")
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_v8_migration_keeps_a_populated_v7_database_and_allows_antigravity(tmp_path):
    data_dir = tmp_path / "version-seven-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        _migrate_v6(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (6)")
        _migrate_v7(connection)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (7)")
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('old-source', 'old-meeting', 'old-generation', 1, 1, '2024-05-06', '{}')"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_prompt_version, notes_schema_version, "
            "notes_source_id, notes_output_language) "
            "VALUES ('old-notes-task', 'old-notes-request', 'old-notes-hash', 'old-meeting', "
            "'notes', '', 'gpt-6.1-sol', 'auto', 'provider', 'en', 'succeeded', 'succeeded', "
            "'notes_only', 12.0, 'chatgpt', 3, 'gpt-6.1-sol', 'high', "
            "'notes-hierarchical-v4', 1, 'old-source', 'vi')"
        )
        connection.execute(
            "INSERT INTO notes_revisions(id, meeting_id, source_snapshot_id, task_id, markdown, "
            "edit_version, provider, model, reasoning_effort, connection_version, "
            "prompt_version, schema_version, output_language) "
            "VALUES ('old-revision', 'old-meeting', 'old-source', 'old-notes-task', ?, 3, "
            "'chatgpt', 'gpt-6.1-sol', 'high', 3, 'notes-hierarchical-v4', 1, 'vi')",
            ("## Tổng quan\n\nBản đã chỉnh sửa.",),
        )

    initialize(data_dir)
    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT status, notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_output_language FROM tasks WHERE id = 'old-notes-task'"
        ).fetchone() == ("succeeded", "chatgpt", 3, "gpt-6.1-sol", "high", "vi")
        assert connection.execute(
            "SELECT status FROM tasks WHERE id = 'active-task'"
        ).fetchone() == ("accepted",)
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == ("active-task",)
        assert connection.execute(
            "SELECT markdown, edit_version, provider, output_language FROM notes_revisions "
            "WHERE id = 'old-revision'"
        ).fetchone() == ("## Tổng quan\n\nBản đã chỉnh sửa.", 3, "chatgpt", "vi")
        assert connection.execute("SELECT id, provider FROM notes_preferences").fetchall() == [
            (1, None)
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
                "('tasks_meeting_created_idx', 'notes_revisions_meeting_idx')"
            )
        }
        assert indexes == {"tasks_meeting_created_idx", "notes_revisions_meeting_idx"}
        # Child tables still point at the rebuilt `tasks` table.
        assert {
            row[2] for row in connection.execute("PRAGMA foreign_key_list(processing_slot)")
        } == {"tasks"}
        assert {
            row[2] for row in connection.execute("PRAGMA foreign_key_list(notes_revisions)")
        } >= {"tasks"}

        notes_columns = (
            "id, request_id, request_hash, meeting_id, kind, model_path, model_name, device, "
            "compute_type, language_hint, status, stage, result_scope, audio_duration_seconds, "
            "notes_provider, notes_connection_version, notes_model, notes_reasoning_effort, "
            "notes_prompt_version, notes_schema_version, notes_source_id"
        )
        insert_task = (
            f"INSERT INTO tasks({notes_columns}) VALUES (?, ?, ?, 'old-meeting', 'notes', '', "
            "'m', 'auto', 'provider', 'en', 'failed', 'failed', 'notes_only', 12.0, ?, 1, 'm', "
            "'default', 'p', 1, 'old-source')"
        )
        connection.execute(insert_task, ("ag-task", "ag-request", "ag-hash", "antigravity"))
        connection.execute(
            "INSERT INTO notes_revisions(id, meeting_id, source_snapshot_id, task_id, markdown, "
            "provider, model, reasoning_effort, connection_version, prompt_version, "
            "schema_version) VALUES ('ag-revision', 'old-meeting', 'old-source', 'ag-task', "
            "'x', 'antigravity', 'm', 'default', 1, 'p', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_task, ("bad-task", "bad-request", "bad-hash", "other"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE notes_preferences SET provider = 'other'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO notes_preferences(id, provider) VALUES (2, NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tasks SET notes_provider = 'antigravity' WHERE id = 'active-task'"
            )


def test_schema_v9_adds_a_validated_coverage_column_and_keeps_existing_revisions(tmp_path):
    data_dir = tmp_path / "version-eight-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for version, migrate in ((6, _migrate_v6), (7, _migrate_v7), (8, _migrate_v8)):
            migrate(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('old-source', 'old-meeting', 'old-generation', 1, 1, '2024-05-06', '{}')"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_prompt_version, notes_schema_version, "
            "notes_source_id) VALUES ('old-notes-task', 'old-notes-request', 'old-notes-hash', "
            "'old-meeting', 'notes', '', 'gpt-6.1-sol', 'auto', 'provider', 'en', 'succeeded', "
            "'succeeded', 'notes_only', 12.0, 'chatgpt', 3, 'gpt-6.1-sol', 'high', "
            "'notes-hierarchical-v5', 1, 'old-source')"
        )
        connection.execute(
            "INSERT INTO notes_revisions(id, meeting_id, source_snapshot_id, task_id, markdown, "
            "provider, model, reasoning_effort, connection_version, prompt_version, "
            "schema_version) VALUES ('old-revision', 'old-meeting', 'old-source', "
            "'old-notes-task', 'Kept notes.', 'chatgpt', 'gpt-6.1-sol', 'high', 3, "
            "'notes-hierarchical-v5', 1)"
        )

    initialize(data_dir)
    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT markdown, coverage_json FROM notes_revisions WHERE id = 'old-revision'"
        ).fetchone() == ("Kept notes.", None)
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "UPDATE notes_revisions SET coverage_json = ? WHERE id = 'old-revision'",
            ('{"version":1,"chunks":[]}',),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE notes_revisions SET coverage_json = 'not json' WHERE id = 'old-revision'"
            )


def test_schema_v10_adds_a_validated_citations_column_and_keeps_a_populated_v9_database(tmp_path):
    data_dir = tmp_path / "version-nine-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)
    edited = "## Overview\n\n- Kept and edited by hand.\n"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for version, migrate in (
            (6, _migrate_v6),
            (7, _migrate_v7),
            (8, _migrate_v8),
            (9, _migrate_v9),
        ):
            migrate(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('old-source', 'old-meeting', 'old-generation', 1, 1, '2024-05-06', '{}')"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_prompt_version, notes_schema_version, "
            "notes_source_id) VALUES ('old-notes-task', 'old-notes-request', 'old-notes-hash', "
            "'old-meeting', 'notes', '', 'gpt-6.1-sol', 'auto', 'provider', 'en', 'succeeded', "
            "'succeeded', 'notes_only', 12.0, 'chatgpt', 3, 'gpt-6.1-sol', 'high', "
            "'notes-hierarchical-v6', 1, 'old-source')"
        )
        connection.execute(
            "INSERT INTO notes_revisions(id, meeting_id, source_snapshot_id, task_id, markdown, "
            "edit_version, provider, model, reasoning_effort, connection_version, "
            "prompt_version, schema_version, coverage_json) VALUES ('old-revision', "
            "'old-meeting', 'old-source', 'old-notes-task', ?, 4, 'chatgpt', 'gpt-6.1-sol', "
            "'high', 3, 'notes-hierarchical-v6', 1, '{\"version\":1}')",
            (edited,),
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (9,)

    initialize(data_dir)
    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT markdown, edit_version, coverage_json, citations_json FROM notes_revisions "
            "WHERE id = 'old-revision'"
        ).fetchone() == (edited, 4, '{"version":1}', None)
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "UPDATE notes_revisions SET citations_json = '[]' WHERE id = 'old-revision'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE notes_revisions SET citations_json = 'not json' WHERE id = 'old-revision'"
            )
        connection.execute(
            "UPDATE notes_revisions SET citations_json = NULL WHERE id = 'old-revision'"
        )

    # The revision made before citations were kept reads as it did, with none to show.
    revision = notes_state(data_dir, "old-meeting")["revisions"][0]
    assert (revision["markdown"], revision["edit_version"], revision["citations"]) == (
        edited,
        4,
        [],
    )
    assert get_note(data_dir, "old-revision")["citations"] == []


def test_schema_v11_forbids_updating_a_notes_source_and_keeps_a_populated_v10_database(tmp_path):
    data_dir = tmp_path / "version-ten-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "app.sqlite3"
    _create_legacy_database(database, version=5)
    snapshot = '{"segments":[{"id":"old-segment","text":"As first saved."}]}'
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for version, migrate in (
            (6, _migrate_v6),
            (7, _migrate_v7),
            (8, _migrate_v8),
            (9, _migrate_v9),
            (10, _migrate_v10),
        ):
            migrate(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('old-source', 'old-meeting', 'old-generation', 1, 1, '2024-05-06', ?)",
            (snapshot,),
        )
        # Before the migration nothing stops a raw UPDATE, which is why the trigger exists.
        connection.execute("UPDATE notes_sources SET meeting_date = NULL WHERE id = 'old-source'")
        connection.execute(
            "UPDATE notes_sources SET meeting_date = '2024-05-06' WHERE id = 'old-source'"
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (10,)

    initialize(data_dir)
    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (15,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT snapshot_json, meeting_date FROM notes_sources WHERE id = 'old-source'"
        ).fetchone() == (snapshot, "2024-05-06")
        for column, value in (("snapshot_json", "'{}'"), ("meeting_date", "NULL")):
            with pytest.raises(sqlite3.IntegrityError, match="notes_sources_are_immutable"):
                connection.execute(f"UPDATE notes_sources SET {column} = {value}")
        assert connection.execute(
            "SELECT snapshot_json, meeting_date FROM notes_sources WHERE id = 'old-source'"
        ).fetchone() == (snapshot, "2024-05-06")
        # A snapshot is still made by inserting a new row.
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES "
            "('new-source', 'old-meeting', 'old-generation', 2, 1, NULL, '{}')"
        )


def test_schema_v15_records_the_runtime_of_existing_generations(tmp_path):
    data_dir = tmp_path / "data"
    database = initialize(data_dir)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("ALTER TABLE tasks DROP COLUMN asr_model")
        connection.execute("ALTER TABLE transcript_generations DROP COLUMN device")
        connection.execute("ALTER TABLE transcript_generations DROP COLUMN compute_type")
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES ('m', 'Older', 'vi')"
        )
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, "
            "audio_duration_seconds) VALUES ('t', 'r', 'h', 'm', 'transcription', '/m', 'm', "
            "'cuda', 'float16', 'vi', 'succeeded', 'succeeded', 1)"
        )
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, uncertainty_policy, segment_count) "
            "VALUES ('g', 'm', 't', 1, 'complete', 'vi', 'm', 'p', 0)"
        )

    initialize(data_dir)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT device, compute_type FROM transcript_generations WHERE id = 'g'"
        ).fetchone() == ("cuda", "float16")
        assert connection.execute("SELECT asr_model FROM tasks WHERE id = 't'").fetchone() == (
            "auto",
        )
