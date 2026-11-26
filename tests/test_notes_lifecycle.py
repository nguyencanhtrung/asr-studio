from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from notes_helpers import seed_cited_notes, seed_completed_transcript

from asr_studio import storage as storage_module
from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.meeting_deletion import delete_meeting
from asr_studio.notes import (
    export_note,
    export_note_version,
    get_note,
    get_note_version,
    list_note_versions,
    update_note,
)
from asr_studio.notes_lifecycle import (
    LIFECYCLE_FAULT_ENV,
    LIFECYCLE_FENCE_ENV,
    empty_trash,
    list_trash,
    restore_version,
    trash_version,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import TaskError


def _saves(data_dir: Path) -> tuple[str, dict[str, object], dict[str, object]]:
    meeting_id, revision, source, _segments = seed_cited_notes(data_dir)
    for version, markdown in enumerate(("Edit two\n", "Edit three\n", "Edit four\n"), 2):
        revision = update_note(
            data_dir,
            revision["id"],
            expected_edit_version=version - 1,
            markdown=markdown,
        )
    return meeting_id, revision, source


def _error(callable_, code: str) -> None:
    with pytest.raises(TaskError) as raised:
        callable_()
    assert raised.value.code == code


def _identities(trash: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "note_id": item["note_id"],
            "edit_version": item["edit_version"],
            "transition_id": item["transition_id"],
        }
        for item in trash["items"]
    ]


def test_trash_restore_purge_preserves_exact_saved_provenance_and_replay_state(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, revision, source = _saves(data_dir)
    note_id = revision["id"]

    original = get_note_version(data_dir, note_id, 1)
    saved = get_note_version(data_dir, note_id, 2)
    assert original["markdown"] != saved["markdown"]
    assert saved["citations"]
    source_id = saved["source_snapshot_id"]
    saved_at = saved["saved_at"]
    actor = saved["actor_role"]

    moved = trash_version(
        data_dir,
        meeting_id,
        operation_id="trash-v2-0001",
        note_id=note_id,
        edit_version=2,
    )
    transition = moved["current_state"]["transition_id"]
    assert moved["historical_outcome"]["status"] == "succeeded"
    assert moved["current_state"]["status"] == "trashed"
    assert "markdown" not in get_note_version(data_dir, note_id, 2)
    trashed = get_note_version(data_dir, note_id, 2)
    assert trashed["can_restore"] is True
    assert trashed["source_snapshot_id"] == source_id
    assert trashed["saved_at"] == saved_at
    assert trashed["actor_role"] == actor

    # The original and latest Save of this revision never enter Trash.
    _error(
        lambda: trash_version(
            data_dir,
            meeting_id,
            operation_id="trash-ai-0001",
            note_id=note_id,
            edit_version=1,
        ),
        "notes_version_protected",
    )
    _error(
        lambda: trash_version(
            data_dir,
            meeting_id,
            operation_id="trash-latest-0001",
            note_id=note_id,
            edit_version=revision["edit_version"],
        ),
        "notes_version_protected",
    )

    # A lost reply can be replayed, but it only reports the current state.
    replay = trash_version(
        data_dir,
        meeting_id,
        operation_id="trash-v2-0001",
        note_id=note_id,
        edit_version=2,
    )
    assert replay["historical_outcome"]["status"] == "succeeded"
    assert replay["current_state"]["status"] == "trashed"

    restored = restore_version(
        data_dir,
        meeting_id,
        operation_id="restore-v2-0001",
        note_id=note_id,
        edit_version=2,
        transition_id=transition,
    )
    assert restored["current_state"]["status"] == "active"
    restored_detail = get_note_version(data_dir, note_id, 2)
    assert restored_detail["markdown"] == saved["markdown"]
    assert restored_detail["citations"] == saved["citations"]
    assert restored_detail["saved_at"] == saved_at
    assert restored_detail["actor_role"] == actor
    assert restored_detail["source_snapshot_id"] == source_id

    # A second trash transition is distinct; the old Restore is not an ABA write.
    retrash = trash_version(
        data_dir,
        meeting_id,
        operation_id="trash-v2-0002",
        note_id=note_id,
        edit_version=2,
    )
    assert retrash["current_state"]["transition_id"] != transition
    trash = list_trash(data_dir, meeting_id)
    assert trash["count"] == 1
    assert trash["items"][0]["transition_id"] == retrash["current_state"]["transition_id"]

    purged = empty_trash(
        data_dir,
        meeting_id,
        operation_id="empty-v2-0001",
        confirmation_digest=trash["digest"],
        items=_identities(trash),
    )
    assert purged["historical_outcome"]["status"] == "succeeded"
    tombstone = get_note_version(data_dir, note_id, 2)
    assert tombstone["id"] == note_id
    assert tombstone["edit_version"] == 2
    assert tombstone["lifecycle_status"] == "purged"
    assert tombstone["tombstone"] is True
    assert "markdown" not in tombstone
    assert "citations" not in tombstone
    _error(lambda: export_note_version(data_dir, note_id, 2), "notes_version_unavailable")

    # The successful Restore receipt is historical; replay cannot restore a purged body.
    old_restore_replay = restore_version(
        data_dir,
        meeting_id,
        operation_id="restore-v2-0001",
        note_id=note_id,
        edit_version=2,
        transition_id=transition,
    )
    assert old_restore_replay["historical_outcome"]["status"] == "succeeded"
    assert old_restore_replay["current_state"]["status"] == "purged"
    assert list_trash(data_dir, meeting_id)["count"] == 0
    assert tombstone["source_snapshot_id"] == source["id"]
    assert get_note(data_dir, note_id)["markdown"] == "Edit four\n"
    assert export_note(data_dir, note_id) == "Edit four\n"
    with sqlite3.connect(database_path(data_dir)) as connection:
        receipt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(notes_lifecycle_operations)")
        }
        assert not receipt_columns.intersection({"markdown", "citations_json", "body"})


def test_empty_trash_requires_exact_current_set_and_rolls_back_fault(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    meeting_id, revision, _source = _saves(data_dir)
    note_id = revision["id"]
    trash_version(
        data_dir,
        meeting_id,
        operation_id="trash-v2-fault",
        note_id=note_id,
        edit_version=2,
    )
    trash_version(
        data_dir,
        meeting_id,
        operation_id="trash-v3-fault",
        note_id=note_id,
        edit_version=3,
    )
    confirmed = list_trash(data_dir, meeting_id)
    assert confirmed["count"] == 2

    _error(
        lambda: empty_trash(
            data_dir,
            meeting_id,
            operation_id="empty-stale-0001",
            confirmation_digest=confirmed["digest"],
            items=_identities({**confirmed, "items": confirmed["items"][1:]}),
        ),
        "trash_confirmation_stale",
    )
    monkeypatch.setenv(LIFECYCLE_FAULT_ENV, "after_purge_item_0")
    with pytest.raises(RuntimeError, match="after_purge_item_0"):
        empty_trash(
            data_dir,
            meeting_id,
            operation_id="empty-fault-0001",
            confirmation_digest=confirmed["digest"],
            items=_identities(confirmed),
        )
    monkeypatch.delenv(LIFECYCLE_FAULT_ENV)
    after_fault = list_trash(data_dir, meeting_id)
    assert after_fault["count"] == 2
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_saved_versions WHERE note_id = ?", (note_id,)
        ).fetchone() == (4,)
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_lifecycle_operations WHERE operation_id = ?",
            ("empty-fault-0001",),
        ).fetchone() == (0,)

    emptied = empty_trash(
        data_dir,
        meeting_id,
        operation_id="empty-fault-0002",
        confirmation_digest=after_fault["digest"],
        items=_identities(after_fault),
    )
    assert emptied["historical_outcome"]["status"] == "succeeded"
    versions = list_note_versions(data_dir, note_id)
    assert [entry["lifecycle_status"] for entry in versions["versions"]] == [
        "active",
        "purged",
        "purged",
        "active",
    ]


def test_lifecycle_scope_fence_save_continuity_and_operation_binding(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    meeting_id, revision, _source = _saves(data_dir)
    other_seed = seed_completed_transcript(data_dir, title="A separate meeting")
    other_meeting, other_revision, _other_source, _other_segments = seed_cited_notes(
        data_dir, existing=other_seed
    )
    note_id = revision["id"]

    # The valid operation receipt belongs to meeting A.  A meeting-B request
    # with the same operation ID must fail as ordinary absence before receipt
    # comparison, rather than disclose idempotency state.
    first_move = trash_version(
        data_dir,
        meeting_id,
        operation_id="scope-shared-0001",
        note_id=note_id,
        edit_version=2,
    )
    _error(
        lambda: trash_version(
            data_dir,
            other_meeting,
            operation_id="scope-shared-0001",
            note_id=note_id,
            edit_version=2,
        ),
        "notes_version_not_found",
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE meetings SET deletion_requested_at = CURRENT_TIMESTAMP WHERE id = ?",
            (other_meeting,),
        )
    _error(
        lambda: trash_version(
            data_dir,
            other_meeting,
            operation_id="scope-shared-0001",
            note_id=note_id,
            edit_version=2,
        ),
        "meeting_deletion_pending",
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE meetings SET deletion_requested_at = NULL WHERE id = ?",
            (other_meeting,),
        )
    restored = restore_version(
        data_dir,
        meeting_id,
        operation_id="scope-restore-0001",
        note_id=note_id,
        edit_version=2,
        transition_id=first_move["current_state"]["transition_id"],
    )
    assert restored["current_state"]["status"] == "active"
    second_move = trash_version(
        data_dir,
        meeting_id,
        operation_id="scope-move-0002",
        note_id=note_id,
        edit_version=2,
    )
    _error(
        lambda: restore_version(
            data_dir,
            other_meeting,
            operation_id="scope-restore-0001",
            note_id=note_id,
            edit_version=2,
            transition_id=first_move["current_state"]["transition_id"],
        ),
        "notes_version_not_found",
    )
    other_trash = list_trash(data_dir, other_meeting)
    assert other_trash["count"] == 0
    _error(
        lambda: empty_trash(
            data_dir,
            other_meeting,
            operation_id="scope-empty-0001",
            confirmation_digest=list_trash(data_dir, meeting_id)["digest"],
            items=[
                {
                    "note_id": note_id,
                    "edit_version": 2,
                    "transition_id": second_move["current_state"]["transition_id"],
                }
            ],
        ),
        "notes_version_not_found",
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_lifecycle_operations WHERE operation_id IN (?, ?, ?)",
            ("scope-shared-0001", "scope-restore-0001", "scope-empty-0001"),
        ).fetchone() == (2,)

    # The second revision remains independent while the first meeting's exact
    # confirmed set is purged.
    assert other_revision["meeting_id"] == other_meeting
    confirmed = list_trash(data_dir, meeting_id)
    empty_trash(
        data_dir,
        meeting_id,
        operation_id="scope-empty-0002",
        confirmation_digest=confirmed["digest"],
        items=_identities(confirmed),
    )
    assert list_trash(data_dir, other_meeting)["count"] == 0

    monkeypatch.setenv(LIFECYCLE_FENCE_ENV, "1")
    _error(
        lambda: trash_version(
            data_dir,
            meeting_id,
            operation_id="fenced-trash-0001",
            note_id=note_id,
            edit_version=3,
        ),
        "notes_lifecycle_fenced",
    )
    # Normal Save journaling remains enabled while lifecycle changes are fenced.
    saved = update_note(data_dir, note_id, expected_edit_version=4, markdown="Still writable\n")
    assert saved["edit_version"] == 5
    monkeypatch.delenv(LIFECYCLE_FENCE_ENV)
    trash_version(
        data_dir,
        meeting_id,
        operation_id="unfenced-trash-0001",
        note_id=note_id,
        edit_version=3,
    )
    _error(
        lambda: trash_version(
            data_dir,
            meeting_id,
            operation_id="unfenced-trash-0001",
            note_id=note_id,
            edit_version=4,
        ),
        "idempotency_conflict",
    )


def test_meeting_finalizer_cleans_lifecycle_children_without_touching_other_meeting(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, revision, source = _saves(data_dir)
    other_seed = seed_completed_transcript(data_dir, title="Retained meeting")
    other_meeting, other_revision, other_source, _segments = seed_cited_notes(
        data_dir, existing=other_seed
    )
    moved = trash_version(
        data_dir,
        meeting_id,
        operation_id="finalizer-trash-0001",
        note_id=revision["id"],
        edit_version=2,
    )
    trash = list_trash(data_dir, meeting_id)
    empty_trash(
        data_dir,
        meeting_id,
        operation_id="finalizer-empty-0001",
        confirmation_digest=trash["digest"],
        items=_identities(trash),
    )
    assert moved["current_state"]["status"] == "trashed"

    assert delete_meeting(data_dir, meeting_id) == {"meeting_id": meeting_id, "status": "deleted"}
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_version_lifecycle WHERE note_id = ?", (revision["id"],)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_lifecycle_operations WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (
        get_note_version(data_dir, other_revision["id"], 1)["markdown"]
        == other_revision["markdown"]
    )
    assert (
        other_source["id"]
        == get_note_version(data_dir, other_revision["id"], 1)["source_snapshot_id"]
    )
    assert source["id"] != other_source["id"]


def test_schema_v24_reopen_and_old_binary_refusal(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    initialize(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (24,)
    monkeypatch.setattr(storage_module, "SCHEMA_VERSION", 23)
    with pytest.raises(RuntimeError, match="schema is newer"):
        initialize(data_dir)


@pytest.mark.anyio
async def test_lifecycle_api_fixed_reads_and_content_free_receipt_replay(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        meeting_id, revision, _source = _saves(settings.data_dir)
        note_id = revision["id"]
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=settings.public_url,
        ) as client:
            assert (await client.get(f"/api/meetings/{meeting_id}/notes-trash")).status_code == 401
            headers = {"Origin": settings.public_url}
            token = AuthStore(settings.auth_dir).load().token
            session = await client.post(
                "/api/access/session", headers=headers, json={"token": token}
            )
            assert session.status_code == 204

            initial = await client.get(f"/api/meetings/{meeting_id}/notes-trash")
            assert initial.status_code == 200
            assert initial.json()["count"] == 0
            assert initial.json()["meeting_title"]

            moved = await client.post(
                f"/api/meetings/{meeting_id}/notes-trash",
                headers=headers,
                json={
                    "operation_id": "api-trash-v2-0001",
                    "note_id": note_id,
                    "edit_version": 2,
                },
            )
            assert moved.status_code == 200
            transition = moved.json()["current_state"]["transition_id"]
            assert "markdown" not in moved.json()
            fixed = await client.get(f"/api/notes/{note_id}/versions/2")
            assert fixed.status_code == 200
            assert fixed.json()["lifecycle_status"] == "trashed"
            assert fixed.json()["can_restore"] is True
            assert "markdown" not in fixed.json()
            assert (await client.get(f"/api/notes/{note_id}/versions/2/export")).status_code == 410

            restored = await client.post(
                f"/api/meetings/{meeting_id}/notes-trash/{note_id}/2/restore",
                headers=headers,
                json={
                    "operation_id": "api-restore-v2-0001",
                    "transition_id": transition,
                },
            )
            assert restored.status_code == 200
            assert restored.json()["current_state"]["status"] == "active"
            assert (await client.get(f"/api/notes/{note_id}/versions/2")).json()["markdown"] == (
                "Edit two\n"
            )

            retrash = await client.post(
                f"/api/meetings/{meeting_id}/notes-trash",
                headers=headers,
                json={
                    "operation_id": "api-trash-v2-0002",
                    "note_id": note_id,
                    "edit_version": 2,
                },
            )
            assert retrash.status_code == 200
            confirmed = (await client.get(f"/api/meetings/{meeting_id}/notes-trash")).json()
            assert confirmed["count"] == 1
            assert set(confirmed) >= {"meeting_id", "meeting_title", "count", "digest", "items"}
            identities = [
                {
                    "note_id": item["note_id"],
                    "edit_version": item["edit_version"],
                    "transition_id": item["transition_id"],
                }
                for item in confirmed["items"]
            ]
            emptied = await client.post(
                f"/api/meetings/{meeting_id}/notes-trash/empty",
                headers=headers,
                json={
                    "operation_id": "api-empty-0001",
                    "confirmation_digest": confirmed["digest"],
                    "items": identities,
                },
            )
            assert emptied.status_code == 200
            assert "markdown" not in emptied.json()
            tombstone = await client.get(f"/api/notes/{note_id}/versions/2")
            assert tombstone.status_code == 200
            assert tombstone.json()["tombstone"] is True
            assert "markdown" not in tombstone.json()
            assert (await client.get(f"/api/meetings/{meeting_id}/notes-trash")).json()[
                "count"
            ] == 0

            old_restore_replay = await client.post(
                f"/api/meetings/{meeting_id}/notes-trash/{note_id}/2/restore",
                headers=headers,
                json={
                    "operation_id": "api-restore-v2-0001",
                    "transition_id": transition,
                },
            )
            assert old_restore_replay.status_code == 200
            assert old_restore_replay.json()["historical_outcome"]["status"] == "succeeded"
            assert old_restore_replay.json()["current_state"]["status"] == "purged"
            assert "markdown" not in old_restore_replay.json()
