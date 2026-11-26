from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest
from notes_helpers import StubNotesProvider, rerun_transcript, seed_completed_transcript
from test_transcription_api import _authorize

from asr_studio import notes
from asr_studio.meeting_deletion import delete_meeting
from asr_studio.notes import (
    accept_notes_task,
    export_note,
    get_note,
    get_notes_source,
    publish_notes_revision,
    update_note,
)
from asr_studio.notes_history import (
    get_saved_version,
    install_notes_history,
    list_saved_versions,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import TaskError, claim_next_task


def _claim(data_dir: Path, meeting_id: str, generation_id: str, request_id: str) -> str:
    accepted = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )
    claimed = claim_next_task(data_dir, owner_id=f"worker-{request_id}")
    assert claimed is not None and claimed["id"] == accepted["id"]
    return accepted["id"]


def _publish(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    request_id: str,
    markdown: str,
) -> dict[str, object]:
    task_id = _claim(data_dir, meeting_id, generation_id, request_id)
    revision = publish_notes_revision(
        data_dir,
        task_id,
        owner_id=f"worker-{request_id}",
        markdown=markdown,
        elapsed_seconds=0.1,
    )
    assert revision is not None
    return revision


def _accept_and_publish(data_dir: Path, request_id: str, markdown: str) -> dict[str, object]:
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(data_dir)
    return _publish(data_dir, meeting_id, generation_id, request_id, markdown)


def _install(data_dir: Path) -> None:
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        install_notes_history(connection)
        connection.commit()


def _legacy_schema(data_dir: Path) -> None:
    """Return the disposable database to the table shape used before Notes history."""
    with sqlite3.connect(database_path(data_dir)) as connection:
        for trigger in (
            "notes_saved_versions_after_insert",
            "notes_saved_versions_after_save",
            "notes_saved_versions_update_guard",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE notes_saved_versions")
        connection.execute("DROP TABLE transcript_segment_reviews")
        connection.execute("DROP TABLE transcript_edit_events")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 22")


def test_install_backfills_current_projection_and_is_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    revision = _accept_and_publish(data_dir, "history-backfill-001", "Original\n")
    edited = "Only the latest legacy Markdown exists.\n"
    saved = update_note(data_dir, revision["id"], expected_edit_version=1, markdown=edited)
    source = get_notes_source(data_dir, revision["source_snapshot_id"])

    _legacy_schema(data_dir)
    initialize(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 22"
        ).fetchone() == (22,)
    _install(data_dir)

    with sqlite3.connect(database_path(data_dir)) as connection:
        rows = connection.execute(
            "SELECT edit_version, markdown, source_id, origin, saved_at "
            "FROM notes_saved_versions WHERE note_id = ?",
            (revision["id"],),
        ).fetchall()
        assert rows[0][:4] == (2, edited, revision["source_snapshot_id"], "legacy")
        assert len(rows) == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert rows[0][4] == saved["updated_at"]
    assert source == get_notes_source(data_dir, revision["source_snapshot_id"])


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_saves_capture_only_the_winner_and_time(tmp_path, monkeypatch, attempt):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segments = seed_completed_transcript(data_dir)
    published_at = f"2026-01-02 03:04:0{2 * attempt}"
    saved_at = f"2026-01-02 03:04:0{2 * attempt + 1}"
    now = published_at
    starting_save = None
    connect = notes._connect

    @contextmanager
    def connect_at_sample_time(path):
        with connect(path) as connection:
            connection.create_function("current_timestamp", 0, lambda: now)
            if starting_save is not None:
                # Both writers hold independent connections before either starts the transaction.
                starting_save.wait(timeout=5)
            yield connection

    monkeypatch.setattr(notes, "_connect", connect_at_sample_time)
    revision = _publish(data_dir, meeting_id, generation_id, "history-live-001", "Generated\n")
    assert revision["created_at"] == published_at
    source_id = revision["source_snapshot_id"]
    source = get_notes_source(data_dir, source_id)

    def save(markdown):
        try:
            return update_note(data_dir, revision["id"], expected_edit_version=1, markdown=markdown)
        except TaskError as error:
            assert (error.status_code, error.code) == (409, "notes_edit_version_changed")
            return None

    now = saved_at
    starting_save = Barrier(2)
    drafts = ("First operator draft\n", "Second operator draft\n")
    with ThreadPoolExecutor(max_workers=2) as writers:
        results = list(writers.map(save, drafts))
    assert results.count(None) == 1
    winner = 1 if results[0] is None else 0
    saved, winning_markdown = results[winner], drafts[winner]
    assert (saved["edit_version"], saved["markdown"], saved["updated_at"]) == (
        2,
        winning_markdown,
        saved_at,
    )

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        history = list_saved_versions(connection, revision["id"])
        assert history == [
            {
                "note_id": revision["id"],
                "edit_version": 1,
                "markdown": "Generated\n",
                "source_id": source_id,
                "origin": "ai",
                "saved_at": revision["created_at"],
            },
            {
                "note_id": revision["id"],
                "edit_version": 2,
                "markdown": winning_markdown,
                "source_id": source_id,
                "origin": "operator",
                "saved_at": saved["updated_at"],
            },
        ]
        assert get_saved_version(connection, revision["id"], 1) == history[0]
        assert get_saved_version(connection, revision["id"], 99) is None
        with pytest.raises(sqlite3.IntegrityError, match="notes_saved_versions_are_immutable"):
            connection.execute(
                "UPDATE notes_saved_versions SET markdown = 'tampered' WHERE note_id = ?",
                (revision["id"],),
            )
    latest = get_note(data_dir, revision["id"])
    assert (latest["edit_version"], latest["markdown"], latest["updated_at"]) == (
        2,
        winning_markdown,
        saved_at,
    )
    assert export_note(data_dir, revision["id"]) == winning_markdown
    assert get_notes_source(data_dir, source_id) == source


def test_regenerated_revision_has_separate_history_and_exact_source(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segments = seed_completed_transcript(data_dir)
    first = _publish(data_dir, meeting_id, generation_id, "history-regenerate-001", "First\n")
    rerun_transcript(data_dir, meeting_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        second_generation = connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()[0]
    second = _publish(data_dir, meeting_id, second_generation, "history-regenerate-002", "Second\n")
    _install(data_dir)

    with sqlite3.connect(database_path(data_dir)) as connection:
        assert first["id"] != second["id"]
        assert list_saved_versions(connection, first["id"])[0]["markdown"] == "First\n"
        assert list_saved_versions(connection, second["id"])[0]["markdown"] == "Second\n"
        assert (
            get_saved_version(connection, first["id"], 1)["source_id"]
            != get_saved_version(connection, second["id"], 1)["source_id"]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_saved_versions WHERE note_id IN (?, ?)",
            (first["id"], second["id"]),
        ).fetchone() == (2,)
    assert first["meeting_id"] == second["meeting_id"] == meeting_id


def test_meeting_deletion_removes_only_its_notes_history(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segments = seed_completed_transcript(data_dir)
    first = _publish(data_dir, meeting_id, generation_id, "history-delete-first", "First original")
    second = _publish(
        data_dir, meeting_id, generation_id, "history-delete-second", "Second original"
    )
    retained = _accept_and_publish(data_dir, "history-delete-retained", "Keep this original")
    for note_id in (first["id"], second["id"], retained["id"]):
        update_note(data_dir, note_id, expected_edit_version=1, markdown="Saved edit")

    database = database_path(data_dir)
    with sqlite3.connect(database) as connection:
        kept_versions = list_saved_versions(connection, retained["id"])
        assert len(kept_versions) == 2
        assert len(list_saved_versions(connection, first["id"])) == 2
        assert len(list_saved_versions(connection, second["id"])) == 2
    kept_source = get_notes_source(data_dir, retained["source_snapshot_id"])

    assert delete_meeting(data_dir, meeting_id) == {"meeting_id": meeting_id, "status": "deleted"}
    with sqlite3.connect(database) as connection:
        for note_id in (first["id"], second["id"]):
            assert list_saved_versions(connection, note_id) == []
        assert list_saved_versions(connection, retained["id"]) == kept_versions
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert get_notes_source(data_dir, retained["source_snapshot_id"]) == kept_source
    assert export_note(data_dir, retained["id"]) == "Saved edit"


def test_install_rolls_back_as_one_caller_transaction_and_rejects_invalid_reads(tmp_path):
    data_dir = tmp_path / "data"
    _accept_and_publish(data_dir, "history-atomic-001", "Generated\n")
    _legacy_schema(data_dir)
    database = database_path(data_dir)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        install_notes_history(connection)
        connection.rollback()
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("notes_saved_versions",),
            ).fetchone()
            is None
        )

    with sqlite3.connect(database) as connection:
        with pytest.raises(ValueError):
            list_saved_versions(connection, "")
        with pytest.raises(ValueError):
            get_saved_version(connection, "note", 0)


@pytest.mark.anyio
async def test_versioned_export_and_legacy_links_resolve_their_own_saved_content(client, settings):
    revision = _accept_and_publish(settings.data_dir, "notes-version-route", "Generated body\n")
    root = f"/api/notes/{revision['id']}"
    assert (await client.get(f"{root}/versions/1")).status_code == 401
    headers = await _authorize(client, settings)
    expected = ["Generated body\n", "First operator copy\n", "Current operator copy\n"]

    for edit_version, markdown in enumerate(expected[1:], 1):
        response = await client.patch(
            root,
            headers=headers,
            json={"expected_edit_version": edit_version, "markdown": markdown},
        )
        assert response.status_code == 200
    versions = (await client.get(f"{root}/versions")).json()
    assert versions["latest_edit_version"] == 3
    assert [entry["edit_version"] for entry in versions["versions"]] == [1, 2, 3]
    assert all("markdown" not in entry for entry in versions["versions"])
    assert [entry["origin"] for entry in versions["versions"]] == ["ai", "operator", "operator"]

    assert (await client.get(root)).json()["markdown"] == expected[2]
    assert (await client.get(f"{root}/export")).text == expected[2]
    for edit_version, markdown in enumerate(expected, 1):
        saved = (await client.get(f"{root}/versions/{edit_version}")).json()
        assert saved["markdown"] == markdown
        assert saved["source_snapshot_id"] == revision["source_snapshot_id"]
        assert saved["provider"] == revision["provider"]
        exported = await client.get(f"{root}/versions/{edit_version}/export")
        assert exported.text == markdown
        assert exported.headers["content-disposition"].endswith(
            f'filename="meeting-notes-v{edit_version}.md"'
        )
    assert (await client.get(f"{root}/versions/42")).status_code == 404
    assert (await client.get(f"{root}/versions/9223372036854775808")).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_inline", [True, False], ids=["legacy-inline", "structured"])
async def test_original_detail_and_export_keep_the_saved_markdown_and_citations(
    client, settings, legacy_inline
):
    data_dir = settings.data_dir
    headers = await _authorize(client, settings)
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    request_id = "notes-version-citations"
    task_id = _claim(data_dir, meeting_id, generation_id, request_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        (source_id,) = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    segment_id = segment_ids[0]
    latest_markdown = "## Decisions\n\n- Review on Monday.\n"
    original = latest_markdown
    citations = None
    display_citations = []
    warnings = []
    if legacy_inline:
        prefix = f"/meetings/{meeting_id}?notes_source={source_id}&segment="
        valid_link = f"{prefix}{segment_id}#transcript"
        broken_link = f"{prefix}missing"
        original = (
            "## Decisions\n\n"
            f"- Review on Monday. [00:00:01]({valid_link}) [00:00:07]({broken_link})\n"
        )
        warnings = [
            {"code": "source_link_invalid", "reason": "unknown_segment", "href": broken_link}
        ]
    else:
        source = {"id": segment_id, "start_seconds": 1.0}
        citation = {"section": "Decisions", "text": "Review on Monday."}
        citations = [{**citation, "sources": [source]}]
        display_citations = [
            {**citation, "sources": [{**source, "members": [source], "end_seconds": 2.0}]}
        ]
    revision = publish_notes_revision(
        data_dir,
        task_id,
        owner_id=f"worker-{request_id}",
        markdown=original,
        elapsed_seconds=0.1,
        citations=citations,
    )
    assert revision is not None
    root = f"/api/notes/{revision['id']}"
    with sqlite3.connect(database_path(data_dir)) as connection:
        (stored_original,) = connection.execute(
            "SELECT markdown FROM notes_revisions WHERE id = ?", (revision["id"],)
        ).fetchone()
    assert stored_original == original
    latest = (await client.get(root)).json()
    assert (latest["markdown"], latest["warnings"]) == (latest_markdown, [])
    assert (await client.get(f"{root}/export")).text == latest_markdown

    edited = "## Decisions\n\n- Review on Tuesday.\n"
    save = await client.patch(
        root, headers=headers, json={"expected_edit_version": 1, "markdown": edited}
    )
    assert save.status_code == 200
    assert (await client.get(root)).json()["markdown"] == edited
    assert (await client.get(f"{root}/export")).text == edited

    detail = await client.get(f"{root}/versions/1")
    exported = await client.get(f"{root}/versions/1/export")
    assert detail.status_code == exported.status_code == 200
    saved = detail.json()
    assert (saved["markdown"], exported.content) == (original, original.encode("utf-8"))
    assert saved["source_snapshot_id"] == source_id
    assert saved["citations"] == (citations or [])
    assert saved["display_citations"] == display_citations
    assert saved["warnings"] == warnings
