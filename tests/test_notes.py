from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from notes_helpers import (
    StubNotesProvider,
    default_cited_document,
    rerun_transcript,
    seed_completed_transcript,
)

from asr_studio.chatgpt import ChatGPTError, ChatGPTProvider
from asr_studio.notes import (
    accept_notes_task,
    assert_provider_mutation_allowed,
    export_note,
    get_note,
    get_notes_source,
    has_active_notes_task,
    last_notes_provider,
    load_notes_task_input,
    notes_state,
    publish_notes_revision,
    update_note,
)
from asr_studio.notes_document import PROMPT_VERSION, NotesDocumentError, validate_document
from asr_studio.recordings import RecordingError, update_meeting
from asr_studio.storage import database_path
from asr_studio.tasks import (
    TaskError,
    add_current_generation_speaker,
    cancel_task,
    claim_next_task,
    fail_task,
    finish_canceled_task,
    reassign_current_generation_speaker,
    recover_abandoned_tasks,
    transcription_state,
    update_current_generation_segment_text,
    update_current_generation_speaker_name,
)
from asr_studio.worker import acquire_worker_lock


def _accept(data_dir: Path, meeting_id: str, generation_id: str, *, request_id="notes-req-0001"):
    return accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )


class _ExplicitNotesProvider(StubNotesProvider):
    def __init__(self) -> None:
        super().__init__()
        self.explicit_pins: list[tuple[str, str]] = []

    @contextmanager
    def notes_task_pin_for_run(self, *, model: str, reasoning_effort: str):
        self.explicit_pins.append((model, reasoning_effort))
        yield {
            "provider": "chatgpt",
            "connection_version": 11,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }


def _markdown(text="Approved next step."):
    return f"# Meeting Notes\n\n{text}\n"


def test_acceptance_freezes_source_pins_account_and_locks_only_its_meeting(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    other_meeting, other_generation, other_segments = seed_completed_transcript(
        data_dir, title="Other meeting"
    )

    accepted = _accept(data_dir, meeting_id, generation_id)
    retry = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="notes-req-0001",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=False,
    )

    assert accepted["id"] == retry["id"]
    assert accepted["kind"] == "notes"
    assert accepted["result_scope"] == "notes_only"
    # Notes have their own status panel; they must not replace local ASR progress.
    assert transcription_state(data_dir, meeting_id)["task"]["kind"] == "transcription"
    assert has_active_notes_task(data_dir, meeting_id=meeting_id)
    assert not has_active_notes_task(data_dir, meeting_id=other_meeting)
    with sqlite3.connect(database_path(data_dir)) as connection:
        row = connection.execute(
            "SELECT notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_prompt_version, notes_schema_version "
            "FROM tasks WHERE id = ?",
            (accepted["id"],),
        ).fetchone()
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
        slot_id = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()[0]
    assert row == ("chatgpt", 7, "gpt-6.1-sol", "high", PROMPT_VERSION, 1)
    assert slot_id == accepted["id"]
    snapshot = get_notes_source(data_dir, source_id)
    assert snapshot["meeting_id"] == meeting_id
    assert snapshot["generation_id"] == generation_id
    assert snapshot["meeting_date"] == "2026-10-19"
    assert [item["id"] for item in snapshot["segments"]] == segment_ids
    assert snapshot["segments"][0]["speaker_name"] == "Morgan"
    encoded_snapshot = json.dumps(snapshot)
    assert "Planning meeting" not in encoded_snapshot
    assert "storage_key" not in encoded_snapshot and "access_token" not in encoded_snapshot
    assert notes_state(data_dir, meeting_id)["source_locked"] is True

    renamed = update_meeting(
        data_dir, meeting_id, expected_version=1, fields={"title": "Renamed meeting"}
    )
    assert renamed["title"] == "Renamed meeting"
    with pytest.raises(RecordingError) as date_lock:
        update_meeting(
            data_dir,
            meeting_id,
            expected_version=2,
            fields={"meeting_date": "2026-10-20"},
        )
    assert date_lock.value.code == "notes_source_locked"
    with pytest.raises(TaskError) as segment_lock:
        update_current_generation_segment_text(
            data_dir,
            meeting_id,
            segment_ids[0],
            expected_generation_id=generation_id,
            expected_source_version=1,
            text="Changed while notes are running.",
        )
    assert segment_lock.value.code == "notes_source_locked"

    changed_other = update_current_generation_segment_text(
        data_dir,
        other_meeting,
        other_segments[0],
        expected_generation_id=other_generation,
        expected_source_version=1,
        text="A different meeting can still change.",
    )
    assert changed_other["source_version"] == 2
    stopping = cancel_task(data_dir, accepted["id"])
    assert stopping["status"] == "canceled"
    assert not has_active_notes_task(data_dir, meeting_id=meeting_id)
    assert notes_state(data_dir, meeting_id)["source_locked"] is False
    changed = update_current_generation_segment_text(
        data_dir,
        meeting_id,
        segment_ids[0],
        expected_generation_id=generation_id,
        expected_source_version=1,
        text="The lock released after the task stopped.",
    )
    assert changed["source_version"] == 2


def test_explicit_run_selects_historical_source_without_preference_mutation_or_replay_pin(
    tmp_path,
):
    data_dir = tmp_path / "data"
    meeting_id, historical_generation_id, _ = seed_completed_transcript(data_dir)
    previous = _publish(
        data_dir,
        meeting_id,
        historical_generation_id,
        markdown="# Edited Notes\n\nKeep this revision.\n",
    )
    edited = update_note(
        data_dir,
        previous["id"],
        expected_edit_version=1,
        markdown="# Edited Notes\n\nThe operator kept this edit.\n",
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE notes_preferences SET provider = 'antigravity' WHERE id = 1")
    preference_before = last_notes_provider(data_dir)
    rerun_transcript(data_dir, meeting_id)
    current = transcription_state(data_dir, meeting_id)["generation"]
    run = {
        "provider": "chatgpt",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "generation_id": historical_generation_id,
        "source_version": 1,
    }
    provider = _ExplicitNotesProvider()

    accepted = accept_notes_task(
        data_dir,
        provider=provider,
        meeting_id=meeting_id,
        request_id="explicit-historical-001",
        expected_generation_id=current["id"],
        expected_source_version=current["source_version"],
        expected_metadata_version=1,
        worker_ready=True,
        run=run,
    )

    assert provider.explicit_pins == [("gpt-6.1-sol", "high")]
    assert last_notes_provider(data_dir) == preference_before == "antigravity"
    assert get_note(data_dir, edited["id"])["markdown"] == (
        "# Edited Notes\n\nThe operator kept this edit.\n"
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        task_row = connection.execute(
            "SELECT notes_provider, notes_connection_version, notes_model, "
            "notes_reasoning_effort, notes_source_id FROM tasks WHERE id = ?",
            (accepted["id"],),
        ).fetchone()
    assert task_row[:4] == ("chatgpt", 11, "gpt-6.1-sol", "high")
    snapshot = get_notes_source(data_dir, task_row[4])
    assert snapshot["generation_id"] == historical_generation_id
    assert [segment["text"] for segment in snapshot["segments"]] == [
        "We need a launch plan.",
        "Morgan will send a draft.",
    ]

    replay = accept_notes_task(
        data_dir,
        provider=_UntouchedProvider(),
        meeting_id=meeting_id,
        request_id="explicit-historical-001",
        expected_generation_id=current["id"],
        expected_source_version=current["source_version"],
        expected_metadata_version=1,
        worker_ready=False,
        run=run,
    )
    assert replay["id"] == accepted["id"]
    changed_run = {**run, "reasoning_effort": "medium"}
    with pytest.raises(TaskError) as conflict:
        accept_notes_task(
            data_dir,
            provider=_UntouchedProvider(),
            meeting_id=meeting_id,
            request_id="explicit-historical-001",
            expected_generation_id=current["id"],
            expected_source_version=current["source_version"],
            expected_metadata_version=1,
            worker_ready=False,
            run=changed_run,
        )
    assert conflict.value.code == "idempotency_conflict"


def test_explicit_run_rejects_outer_choice_fields(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    run = {
        "provider": "chatgpt",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "generation_id": generation_id,
        "source_version": 1,
    }

    for kwargs in (
        {"provider_id": "chatgpt"},
        {"expected_model": "gpt-6.1-sol"},
        {"expected_reasoning_effort": "high"},
    ):
        with pytest.raises(TaskError) as failure:
            accept_notes_task(
                data_dir,
                provider=_ExplicitNotesProvider(),
                meeting_id=meeting_id,
                request_id=f"explicit-mixed-{len(kwargs)}-{next(iter(kwargs))}",
                expected_generation_id=generation_id,
                expected_source_version=1,
                expected_metadata_version=1,
                worker_ready=True,
                run=run,
                **kwargs,
            )
        assert failure.value.code == "invalid_request"


def test_explicit_run_keeps_current_guards_and_validates_selected_source_version(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, historical_generation_id, _ = seed_completed_transcript(data_dir)
    rerun_transcript(data_dir, meeting_id)
    current = transcription_state(data_dir, meeting_id)["generation"]
    base_run = {
        "provider": "chatgpt",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "generation_id": historical_generation_id,
        "source_version": 1,
    }
    cases = (
        (
            "stale-current-generation",
            {"expected_generation_id": historical_generation_id},
            "current_generation_changed",
        ),
        (
            "stale-current-source",
            {"expected_source_version": 2},
            "source_version_changed",
        ),
        (
            "stale-metadata",
            {"expected_metadata_version": 2},
            "metadata_version_changed",
        ),
        (
            "stale-selected-source",
            {"run": {**base_run, "source_version": 2}},
            "source_version_changed",
        ),
    )
    for request_suffix, changes, expected_code in cases:
        provider = _ExplicitNotesProvider()
        arguments = {
            "expected_generation_id": current["id"],
            "expected_source_version": current["source_version"],
            "expected_metadata_version": 1,
            "run": base_run,
        }
        arguments.update(changes)
        with pytest.raises(TaskError) as failure:
            accept_notes_task(
                data_dir,
                provider=provider,
                meeting_id=meeting_id,
                request_id=f"explicit-guard-{request_suffix}",
                worker_ready=True,
                **arguments,
            )
        assert failure.value.code == expected_code


def test_busy_notes_refusal_does_not_wait_for_provider_operation_lock(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(data_dir)
    provider = StubNotesProvider()
    accepted = accept_notes_task(
        data_dir,
        provider=provider,
        meeting_id=meeting_id,
        request_id="busy-notes-first",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )
    ready = threading.Event()
    release = threading.Event()

    def hold_provider_lock():
        with provider.store.operation():
            ready.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_provider_lock)
    holder.start()
    assert ready.wait(timeout=2)
    started = time.monotonic()
    try:
        with pytest.raises(TaskError) as refused:
            accept_notes_task(
                data_dir,
                provider=provider,
                meeting_id=meeting_id,
                request_id="busy-notes-second",
                expected_generation_id=generation_id,
                expected_source_version=1,
                expected_metadata_version=1,
                worker_ready=True,
            )
    finally:
        release.set()
        holder.join(timeout=2)
    assert refused.value.code == "system_busy"
    assert time.monotonic() - started < 1.0
    assert accepted["status"] == "accepted"


@pytest.mark.parametrize("output_language", ["en", "vi"])
def test_busy_precheck_rechecks_a_request_accepted_after_initial_lookup(
    tmp_path, monkeypatch, output_language
):
    from asr_studio import notes

    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    lookup = notes._existing_task
    raced = {}

    def accept_between_reads(*args):
        existing = lookup(*args)
        if not raced:
            raced["started"] = True
            raced["task"] = _accept(
                data_dir, meeting_id, generation_id, request_id="notes-race-request"
            )
        return existing

    monkeypatch.setattr(notes, "_existing_task", accept_between_reads)
    kwargs = dict(
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="notes-race-request",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
        output_language=output_language,
    )
    if output_language == "en":
        assert accept_notes_task(data_dir, **kwargs)["id"] == raced["task"]["id"]
    else:
        with pytest.raises(TaskError) as conflict:
            accept_notes_task(data_dir, **kwargs)
        assert conflict.value.code == "idempotency_conflict"


def test_notes_output_language_is_pinned_and_part_of_idempotency(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(data_dir)
    english = _accept(data_dir, meeting_id, generation_id, request_id="language-req-0001")
    explicit_english = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="language-req-0001",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=False,
        output_language="en",
    )
    assert english["output_language"] == "en"
    assert explicit_english["id"] == english["id"]
    with pytest.raises(TaskError) as conflict:
        accept_notes_task(
            data_dir,
            provider=StubNotesProvider(),
            meeting_id=meeting_id,
            request_id="language-req-0001",
            expected_generation_id=generation_id,
            expected_source_version=1,
            expected_metadata_version=1,
            worker_ready=False,
            output_language="vi",
        )
    assert conflict.value.code == "idempotency_conflict"

    cancel_task(data_dir, english["id"])
    vietnamese = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="language-req-0002",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
        output_language="vi",
    )
    assert vietnamese["output_language"] == "vi"
    task = claim_next_task(data_dir, owner_id="language-worker")
    assert task is not None and task["id"] == vietnamese["id"]
    loaded_task, _snapshot = load_notes_task_input(
        data_dir, vietnamese["id"], owner_id="language-worker"
    )
    assert loaded_task["notes_output_language"] == "vi"
    markdown = "## Tổng quan\n\n- Bản nháp đã lưu.\n"
    revision = publish_notes_revision(
        data_dir,
        vietnamese["id"],
        owner_id="language-worker",
        markdown=markdown,
        elapsed_seconds=1.0,
    )
    assert revision is not None
    assert revision["output_language"] == "vi"
    assert revision["markdown"] == markdown
    assert get_note(data_dir, revision["id"])["output_language"] == "vi"
    assert notes_state(data_dir, meeting_id)["revisions"][0]["output_language"] == "vi"


@pytest.mark.parametrize("language", [None, True, False, "EN", "fr", "", 0])
def test_notes_output_language_rejects_values_outside_exact_language_codes(tmp_path, language):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(data_dir)

    with pytest.raises(TaskError) as invalid:
        accept_notes_task(
            data_dir,
            provider=StubNotesProvider(),
            meeting_id=meeting_id,
            request_id="invalid-language-001",
            expected_generation_id=generation_id,
            expected_source_version=1,
            expected_metadata_version=1,
            worker_ready=True,
            output_language=language,
        )
    assert invalid.value.code == "invalid_output_language"


def test_existing_notes_omit_timestamp_references_in_reads_and_export(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")
    with sqlite3.connect(database_path(data_dir)) as connection:
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
    reference = (
        f"[00:00:01](/meetings/{meeting_id}?notes_source={source_id}&segment={segment_ids[0]})"
    )
    summary = "## Overview\n\n- Review at 10:30.\n- Keep [00:00:01](https://example.test).\n"
    legacy = summary.replace("Review at 10:30.", f"Review at 10:30. {reference} {reference}")
    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=legacy,
        elapsed_seconds=1.0,
    )
    assert revision is not None
    assert get_note(data_dir, revision["id"])["markdown"] == summary
    assert notes_state(data_dir, meeting_id)["revisions"][0]["markdown"] == summary
    assert export_note(data_dir, revision["id"]) == summary
    with sqlite3.connect(database_path(data_dir)) as connection:
        saved = connection.execute(
            "SELECT markdown, edit_version FROM notes_revisions WHERE id = ?", (revision["id"],)
        ).fetchone()
    assert saved == (legacy, 1)


def test_revision_edit_conflicts_export_and_source_staleness(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    task = claim_next_task(data_dir, owner_id="notes-worker")
    assert task is not None and task["id"] == accepted["id"]
    original_markdown = _markdown()
    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=original_markdown,
        elapsed_seconds=1.5,
    )
    assert revision is not None
    assert revision["source_state"] == "current"
    assert revision["provider"] == "chatgpt"
    assert revision["model"] == "gpt-6.1-sol"
    assert get_notes_source(data_dir, revision["source_snapshot_id"])["segments"][0]["text"] == (
        "We need a launch plan."
    )

    edited_markdown = _markdown("Morgan owns the draft.")
    saved = update_note(
        data_dir,
        revision["id"],
        expected_edit_version=1,
        markdown=edited_markdown,
    )
    assert saved["edit_version"] == 2
    assert saved["markdown"] == edited_markdown
    with pytest.raises(TaskError) as conflict:
        update_note(
            data_dir,
            revision["id"],
            expected_edit_version=1,
            markdown="A stale browser draft.",
        )
    assert conflict.value.code == "notes_edit_version_changed"
    assert export_note(data_dir, revision["id"]) == edited_markdown
    assert get_note(data_dir, revision["id"])["markdown"] == edited_markdown

    renamed = update_meeting(
        data_dir, meeting_id, expected_version=1, fields={"title": "New display title"}
    )
    assert renamed["notes_source_version"] == 1
    assert get_note(data_dir, revision["id"])["source_state"] == "current"
    update_meeting(
        data_dir,
        meeting_id,
        expected_version=2,
        fields={"meeting_date": "2026-10-20"},
    )
    assert get_note(data_dir, revision["id"])["source_state"] == "stale"
    update_current_generation_segment_text(
        data_dir,
        meeting_id,
        segment_ids[0],
        expected_generation_id=generation_id,
        expected_source_version=1,
        text="The transcript changed after publication.",
    )
    assert get_note(data_dir, revision["id"])["source_state"] == "stale"


def test_revision_becomes_historical_when_meeting_selects_another_generation(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")
    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=_markdown(),
        elapsed_seconds=1.0,
    )
    assert revision is not None

    next_task_id = "b" * 32
    next_generation_id = "c" * 32
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds) VALUES (?, ?, ?, ?, 'transcription', '/models/whisper', "
            "'whisper', 'cpu', 'int8', 'en', 'succeeded', 'complete', 'transcript_only', 30.0)",
            (next_task_id, "next-generation-task", "next-generation-hash", meeting_id),
        )
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, uncertainty_policy, segment_count) "
            "VALUES (?, ?, ?, 2, 'complete', 'en', 'whisper', 'test-policy', 0)",
            (next_generation_id, meeting_id, next_task_id),
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = ? WHERE id = ?",
            (next_generation_id, meeting_id),
        )

    assert get_note(data_dir, revision["id"])["source_state"] == "historical"


def test_completed_rerun_remains_current_when_followup_notes_fails(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    previous_markdown = _markdown("The original revision remains available.")
    previous_revision = _publish(data_dir, meeting_id, generation_id, markdown=previous_markdown)

    rerun_transcript(data_dir, meeting_id)
    newer = transcription_state(data_dir, meeting_id)["generation"]
    assert newer["id"] != generation_id
    assert newer["generation_number"] == 2

    accepted = _accept(
        data_dir,
        meeting_id,
        newer["id"],
        request_id="notes-after-rerun-fails",
    )
    claimed = claim_next_task(data_dir, owner_id="notes-failure-worker")
    assert claimed is not None and claimed["id"] == accepted["id"]
    assert fail_task(
        data_dir,
        accepted["id"],
        owner_id="notes-failure-worker",
        error_code="notes_provider_failed",
        message="Provider failed after the transcript was published.",
        elapsed_seconds=1.0,
    )

    state = transcription_state(data_dir, meeting_id)
    assert state["generation"]["id"] == newer["id"]
    assert state["generation_history"][0]["id"] == generation_id
    assert notes_state(data_dir, meeting_id)["task"]["status"] == "failed"
    assert get_note(data_dir, previous_revision["id"])["source_state"] == "historical"

    edited_markdown = _markdown("The historical revision was edited safely.")
    edited = update_note(
        data_dir,
        previous_revision["id"],
        expected_edit_version=1,
        markdown=edited_markdown,
    )
    assert edited["markdown"] == edited_markdown
    assert export_note(data_dir, previous_revision["id"]) == edited_markdown


def test_cancel_and_recovery_never_publish_late_result_or_remove_old_revisions(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    first = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker-1")
    previous_revision = publish_notes_revision(
        data_dir,
        first["id"],
        owner_id="notes-worker-1",
        markdown=_markdown("Keep this saved revision."),
        elapsed_seconds=1.0,
    )
    assert previous_revision is not None

    second = _accept(data_dir, meeting_id, generation_id, request_id="notes-req-0002")
    assert cancel_task(data_dir, second["id"])["status"] == "canceled"
    assert (
        publish_notes_revision(
            data_dir,
            second["id"],
            owner_id="notes-worker-2",
            markdown=_markdown("Late output must be discarded."),
            elapsed_seconds=2.0,
        )
        is None
    )
    assert [item["id"] for item in notes_state(data_dir, meeting_id)["revisions"]] == [
        previous_revision["id"]
    ]

    third = _accept(data_dir, meeting_id, generation_id, request_id="notes-req-0003")
    claim_next_task(data_dir, owner_id="notes-worker-3")
    lock_fd = acquire_worker_lock(data_dir)
    assert lock_fd is not None
    try:
        assert recover_abandoned_tasks(data_dir) == [third["id"]]
    finally:
        import os

        os.close(lock_fd)
    state = notes_state(data_dir, meeting_id)
    assert state["task"]["status"] == "interrupted"
    assert state["source_locked"] is False
    assert [item["id"] for item in state["revisions"]] == [previous_revision["id"]]
    assert export_note(data_dir, previous_revision["id"]) == _markdown("Keep this saved revision.")


def test_provider_mutations_are_denied_until_active_notes_task_is_terminal(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    task = _accept(data_dir, meeting_id, generation_id)
    provider = ChatGPTProvider(
        tmp_path / "provider",
        callback_url="",
        active_task_guard=lambda: assert_provider_mutation_allowed(data_dir, "chatgpt"),
    )
    provider.ensure()

    with pytest.raises(ChatGPTError) as guarded:
        provider.start_login()
    assert guarded.value.code == "provider_in_use"
    with pytest.raises(ChatGPTError) as checked:
        assert_provider_mutation_allowed(data_dir, "chatgpt")
    assert checked.value.code == "provider_in_use"
    # Only the provider pinned by the active task is blocked.
    assert_provider_mutation_allowed(data_dir, "antigravity")

    assert cancel_task(data_dir, task["id"])["status"] == "canceled"
    assert_provider_mutation_allowed(data_dir, "chatgpt")
    with provider.store.locked() as state:
        assert state["connection_status"] == "disconnected"
        assert state["connection_version"] == 0


def test_segment_over_one_request_budget_is_accepted_and_the_transcript_stays_whole(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    long_text = "x" * 60_000
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_segments SET text = ? WHERE id = ?", (long_text, segment_ids[0])
        )

    accepted = _accept(data_dir, meeting_id, generation_id)

    assert accepted["status"] == "accepted"
    with sqlite3.connect(database_path(data_dir)) as connection:
        stored = connection.execute(
            "SELECT text FROM transcript_segments WHERE id = ?", (segment_ids[0],)
        ).fetchone()
        snapshot_json = connection.execute("SELECT snapshot_json FROM notes_sources").fetchone()[0]
    assert stored == (long_text,)
    frozen = json.loads(snapshot_json)["segments"]
    assert [item["id"] for item in frozen] == segment_ids
    assert frozen[0]["text"] == long_text  # parts exist only in requests, never in the snapshot


def test_source_needing_more_parts_than_the_chunk_limit_is_rejected_without_a_task(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_segments SET text = ? WHERE id = ?",
            ("x" * 600_000, segment_ids[0]),
        )

    with pytest.raises(TaskError) as oversized:
        _accept(data_dir, meeting_id, generation_id)

    assert oversized.value.status_code == 413
    assert oversized.value.code == "notes_source_too_large"
    assert "maximum of 24 notes chunks" in oversized.value.message
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'notes'").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM notes_sources").fetchone() == (0,)
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)


def test_long_transcript_is_accepted_with_all_source_segments_frozen(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, initial_segment_ids = seed_completed_transcript(data_dir)
    additional_segments = [
        (
            uuid.uuid4().hex,
            generation_id,
            sequence,
            5.0 + (sequence - 2) * 0.025,
            5.01 + (sequence - 2) * 0.025,
            f"Long-source segment {sequence}: review the proposal and unresolved question.",
            "SPEAKER_00",
            "SPEAKER_00",
            0,
        )
        for sequence in range(2, 1_000)
    ]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, uncertain) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            additional_segments,
        )

    accepted = _accept(data_dir, meeting_id, generation_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
    snapshot = get_notes_source(data_dir, source_id)

    assert accepted["status"] == "accepted"
    assert len(snapshot["segments"]) == 1_000
    assert [segment["id"] for segment in snapshot["segments"][:2]] == initial_segment_ids
    assert [segment["id"] for segment in snapshot["segments"][2:]] == [
        row[0] for row in additional_segments
    ]


_COVERAGE = {
    "version": 1,
    "segment_count": 2,
    "chunks": [
        {"index": 1, "owned": [0, 2], "overlap": None, "parts": [], "provider_call_index": 1}
    ],
}


def _stored_coverage(data_dir: Path, revision_id: str) -> str | None:
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT coverage_json FROM notes_revisions WHERE id = ?", (revision_id,)
        ).fetchone()[0]


def test_publication_stores_the_coverage_manifest_with_the_revision(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")

    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=_markdown(),
        elapsed_seconds=1.0,
        coverage=_COVERAGE,
    )

    assert revision is not None
    assert json.loads(_stored_coverage(data_dir, revision["id"])) == _COVERAGE
    assert "coverage_json" not in revision  # the proof is stored, not part of the API payload


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        ["chunks"],
        {"chunks": []},
        {"chunks": [{"index": 1, "owned": [0, 2]}]},
        {"chunks": [{"provider_call_index": 0}]},
        {"chunks": [{"provider_call_index": True}]},
        {"chunks": [{"provider_call_index": 1}, {"index": 2}]},
        {"chunks": [{"provider_call_index": 1}], "bad": float("nan")},
        {"chunks": [{"provider_call_index": 1}], "padding": "x" * 70_000},
    ],
)
def test_publication_refuses_coverage_that_does_not_show_every_request_processed(
    tmp_path, coverage
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")

    with pytest.raises(TaskError) as refused:
        publish_notes_revision(
            data_dir,
            accepted["id"],
            owner_id="notes-worker",
            markdown=_markdown(),
            elapsed_seconds=1.0,
            coverage=coverage,
        )

    assert refused.value.code in {"invalid_coverage", "coverage_too_large"}
    state = notes_state(data_dir, meeting_id)
    assert state["revisions"] == [] and state["task"]["status"] == "running"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (accepted["id"],)


def _stored_citations(data_dir: Path, revision_id: str) -> str | None:
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT citations_json FROM notes_revisions WHERE id = ?", (revision_id,)
        ).fetchone()[0]


def _citations(segment_ids: list[str]) -> list[dict[str, object]]:
    """Two bullets, the second citing both seeded segments (they start at 1.0 s and 4.0 s)."""
    return [
        {
            "section": "Decisions",
            "text": r"Prepare the launch plan\.",
            "sources": [{"id": segment_ids[0], "start_seconds": 1.0}],
        },
        {
            "section": "Confirmed Action Items",
            "text": "Send the draft.",
            "sources": [
                {"id": segment_ids[1], "start_seconds": 4.0},
                {"id": segment_ids[0], "start_seconds": 1.0},
            ],
        },
    ]


def test_publication_stores_citations_apart_from_the_markdown_and_serves_them(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")
    citations = _citations(segment_ids)

    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=_markdown(),
        elapsed_seconds=1.0,
        citations=citations,
    )

    assert revision is not None and revision["citations"] == citations
    assert json.loads(_stored_citations(data_dir, revision["id"])) == citations
    assert "notes_source=" not in revision["markdown"]
    assert notes_state(data_dir, meeting_id)["revisions"][0]["citations"] == citations
    assert get_note(data_dir, revision["id"])["citations"] == citations

    # Editing the Markdown never touches the citations: a changed bullet just stops matching.
    edited = update_note(
        data_dir, revision["id"], expected_edit_version=1, markdown="## Decisions\n\n- Changed.\n"
    )
    assert edited["edit_version"] == 2 and edited["citations"] == citations
    assert json.loads(_stored_citations(data_dir, revision["id"])) == citations
    assert export_note(data_dir, revision["id"]) == "## Decisions\n\n- Changed.\n"


def test_a_revision_without_stored_citations_reads_an_empty_list(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")

    revision = publish_notes_revision(
        data_dir, accepted["id"], owner_id="notes-worker", markdown=_markdown(), elapsed_seconds=1
    )

    assert revision is not None and revision["citations"] == []
    assert _stored_citations(data_dir, revision["id"]) is None
    assert notes_state(data_dir, meeting_id)["revisions"][0]["citations"] == []
    assert (
        update_note(data_dir, revision["id"], expected_edit_version=1, markdown="x")["citations"]
        == []
    )


def _bad_citations(segment_ids: list[str]) -> dict[str, object]:
    first, second = segment_ids
    good = _citations(segment_ids)[0]
    return {
        "not a list": {"section": "Decisions"},
        "entry is not an object": ["Decisions"],
        "missing sources": [{"section": "Decisions", "text": "x"}],
        "extra field": [{**good, "extra": 1}],
        "section is not text": [{**good, "section": 1}],
        "text is not text": [{**good, "text": None}],
        "sources is not a list": [{**good, "sources": {"id": first, "start_seconds": 1.0}}],
        "source is not an object": [{**good, "sources": [first]}],
        "source has an extra field": [
            {**good, "sources": [{"id": first, "start_seconds": 1.0, "end": 2}]}
        ],
        "source id is not text": [{**good, "sources": [{"id": 1, "start_seconds": 1.0}]}],
        "source id is not in the snapshot": [
            {**good, "sources": [{"id": "f" * 32, "start_seconds": 1.0}]}
        ],
        "source id repeats": [
            {**good, "sources": [{"id": first, "start_seconds": 1.0}] * 2},
        ],
        "start differs from the snapshot": [
            {**good, "sources": [{"id": first, "start_seconds": 1.5}]}
        ],
        "start is a string": [{**good, "sources": [{"id": first, "start_seconds": "1.0"}]}],
        "start is a boolean": [{**good, "sources": [{"id": first, "start_seconds": True}]}],
        "start is not finite": [
            {**good, "sources": [{"id": first, "start_seconds": float("nan")}]}
        ],
        "too many entries": [{**good, "sources": []}] * 1_201,
        "too many sources": [{**good, "sources": [{"id": second, "start_seconds": 4.0}] * 201}],
        "too large": [{**good, "text": "x" * 1_100_000}],
    }


@pytest.mark.parametrize("case", tuple(_bad_citations(["a" * 32, "b" * 32])))  # names only
def test_publication_refuses_citations_the_saved_source_does_not_back(tmp_path, case):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")

    with pytest.raises(TaskError) as refused:
        publish_notes_revision(
            data_dir,
            accepted["id"],
            owner_id="notes-worker",
            markdown=_markdown(),
            elapsed_seconds=1.0,
            citations=_bad_citations(segment_ids)[case],
        )

    assert refused.value.code in {"invalid_citations", "citations_too_large"}
    state = notes_state(data_dir, meeting_id)
    assert state["revisions"] == [] and state["task"]["status"] == "running"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (accepted["id"],)


class _UntouchedProvider:
    """Any use means the replay went on to pin a provider, that is, to start another run."""

    store = property(lambda self: pytest.fail("A replay must not touch the provider."))

    def assert_notes_acceptable(self):
        pytest.fail("A replay must not check whether notes can start.")

    def notes_task_pin(self):
        pytest.fail("A replay must not pin the provider.")


def test_replayed_request_returns_the_finished_task_and_never_starts_another(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")
    assert (
        publish_notes_revision(
            data_dir,
            accepted["id"],
            owner_id="notes-worker",
            markdown=_markdown(),
            elapsed_seconds=1.0,
            coverage=_COVERAGE,
        )
        is not None
    )

    def replay(**changes):
        return accept_notes_task(
            data_dir,
            provider=_UntouchedProvider(),
            meeting_id=meeting_id,
            request_id="notes-req-0001",
            expected_generation_id=generation_id,
            expected_source_version=1,
            expected_metadata_version=1,
            worker_ready=False,
            **changes,
        )

    same = replay()
    assert same["id"] == accepted["id"] and same["status"] == "succeeded"
    with pytest.raises(TaskError) as conflict:
        replay(output_language="vi")
    assert conflict.value.code == "idempotency_conflict"
    # Nothing new was queued, so no worker can send another request to the model.
    assert claim_next_task(data_dir, owner_id="another-worker") is None
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'notes'").fetchone() == (
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM notes_sources").fetchone() == (1,)


def _publish(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    *,
    source_version: int = 1,
    markdown: str | None = None,
) -> dict[str, object]:
    """Accept, claim and publish one revision, as the worker does."""
    accepted = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="notes-req-0001",
        expected_generation_id=generation_id,
        expected_source_version=source_version,
        expected_metadata_version=1,
        worker_ready=True,
    )
    claimed = claim_next_task(data_dir, owner_id="notes-worker")
    assert claimed is not None and claimed["id"] == accepted["id"]
    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id="notes-worker",
        markdown=markdown or _markdown(),
        elapsed_seconds=1.0,
    )
    assert revision is not None
    return revision


def _stored(data_dir: Path, revision: dict[str, object]) -> tuple[str, str]:
    """The snapshot JSON and the Markdown exactly as the database holds them."""
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT sources.snapshot_json, revisions.markdown FROM notes_revisions AS revisions "
            "JOIN notes_sources AS sources ON sources.id = revisions.source_snapshot_id "
            "WHERE revisions.id = ?",
            (revision["id"],),
        ).fetchone()


def _versions(data_dir: Path, meeting_id: str) -> tuple[int, int, int]:
    """The transcript's source version, the notes source version and the metadata version."""
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT generations.source_version, meetings.notes_source_version, "
            "meetings.metadata_version FROM meetings "
            "JOIN transcript_generations AS generations "
            "ON generations.id = meetings.current_generation_id WHERE meetings.id = ?",
            (meeting_id,),
        ).fetchone()


def test_a_snapshot_keeps_both_speaker_assignments_names_and_date_through_every_later_edit(
    tmp_path,
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    riley = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        display_name="Riley",
    )
    reassigned = reassign_current_generation_speaker(
        data_dir,
        meeting_id,
        segment_ids[0],
        expected_generation_id=generation_id,
        expected_source_version=riley["source_version"],
        expected_assignment_version=1,
        speaker_id=riley["speaker_id"],
    )
    revision = _publish(
        data_dir, meeting_id, generation_id, source_version=reassigned["source_version"]
    )

    snapshot = get_notes_source(data_dir, revision["source_snapshot_id"])
    assert snapshot["segments"] == [
        {
            "id": segment_ids[0],
            "sequence": 0,
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text": "We need a launch plan.",
            "original_speaker_id": "SPEAKER_00",
            "speaker_id": riley["speaker_id"],
            "speaker_name": "Riley",
        },
        {
            "id": segment_ids[1],
            "sequence": 1,
            "start_seconds": 4.0,
            "end_seconds": 5.0,
            "text": "Morgan will send a draft.",
            "original_speaker_id": "SPEAKER_00",
            "speaker_id": "SPEAKER_00",
            "speaker_name": "Morgan",
        },
    ]
    assert {item["speaker_id"]: item["display_name"] for item in snapshot["speakers"]} == {
        "SPEAKER_00": "Morgan",
        riley["speaker_id"]: "Riley",
    }
    assert snapshot["meeting_date"] == "2026-10-19"

    before = _stored(data_dir, revision)
    transcript = {"expected_generation_id": generation_id}
    version = reassigned["source_version"]
    for edit in (
        lambda v: update_current_generation_segment_text(
            data_dir,
            meeting_id,
            segment_ids[1],
            **transcript,
            expected_source_version=v,
            text="Morgan sends it today.",
        ),
        lambda v: update_current_generation_speaker_name(
            data_dir,
            meeting_id,
            "SPEAKER_00",
            **transcript,
            expected_source_version=v,
            display_name="Dana",
        ),
        lambda v: add_current_generation_speaker(
            data_dir, meeting_id, **transcript, expected_source_version=v, display_name="Casey"
        ),
        lambda v: reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            segment_ids[1],
            **transcript,
            expected_source_version=v,
            expected_assignment_version=1,
            speaker_id=riley["speaker_id"],
        ),
    ):
        version = edit(version)["source_version"]
    metadata_version = 1
    for fields in (
        {"meeting_date": "2026-10-20"},
        {"meeting_date": None},
        {"meeting_date": "2026-10-21"},
        {"title": "Renamed"},
    ):
        meeting = update_meeting(
            data_dir, meeting_id, expected_version=metadata_version, fields=fields
        )
        metadata_version = meeting["metadata_version"]

    assert _stored(data_dir, revision) == before
    assert get_notes_source(data_dir, revision["source_snapshot_id"]) == snapshot
    assert get_note(data_dir, revision["id"])["source_state"] == "stale"
    with (
        sqlite3.connect(database_path(data_dir)) as connection,
        pytest.raises(sqlite3.IntegrityError, match="notes_sources_are_immutable"),
    ):
        connection.execute(
            "UPDATE notes_sources SET snapshot_json = '{}' WHERE id = ?",
            (revision["source_snapshot_id"],),
        )
    assert _stored(data_dir, revision) == before


# Each edit with the versions it leaves (transcript source, notes source, metadata) and the state
# it gives notes made before it. The seeded meeting starts at (2, 1, 1) after one speaker is added.
_SOURCE_EDITS = [
    ("title", (2, 1, 2), "current"),
    ("date added", (2, 2, 2), "stale"),
    ("date changed", (2, 2, 2), "stale"),
    ("date cleared", (2, 2, 2), "stale"),
    ("segment text", (3, 1, 1), "stale"),
    ("speaker name", (3, 1, 1), "stale"),
    ("speaker assignment", (3, 1, 1), "stale"),
    ("added speaker", (3, 1, 1), "stale"),
]


def _edit_source(data_dir, meeting_id, generation_id, segment_ids, riley_id, kind):
    transcript = {"expected_generation_id": generation_id, "expected_source_version": 2}
    if kind == "title":
        update_meeting(data_dir, meeting_id, expected_version=1, fields={"title": "Renamed"})
    elif kind.startswith("date"):
        date = None if kind == "date cleared" else "2026-10-20"
        update_meeting(data_dir, meeting_id, expected_version=1, fields={"meeting_date": date})
    elif kind == "segment text":
        update_current_generation_segment_text(
            data_dir, meeting_id, segment_ids[0], **transcript, text="Edited after notes."
        )
    elif kind == "speaker name":
        update_current_generation_speaker_name(
            data_dir, meeting_id, "SPEAKER_00", **transcript, display_name="Dana"
        )
    elif kind == "speaker assignment":
        reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            segment_ids[0],
            **transcript,
            expected_assignment_version=1,
            speaker_id=riley_id,
        )
    else:
        add_current_generation_speaker(data_dir, meeting_id, **transcript, display_name="Casey")


@pytest.mark.parametrize(
    ("kind", "versions", "state"), _SOURCE_EDITS, ids=[case[0] for case in _SOURCE_EDITS]
)
def test_each_edit_bumps_its_version_and_only_a_source_edit_marks_notes_stale(
    tmp_path, kind, versions, state
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(
        data_dir, meeting_date=None if kind == "date added" else "2026-10-19"
    )
    riley = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        display_name="Riley",
    )
    revision = _publish(data_dir, meeting_id, generation_id, source_version=riley["source_version"])
    assert revision["source_state"] == "current"
    assert _versions(data_dir, meeting_id) == (2, 1, 1)
    before = _stored(data_dir, revision)

    _edit_source(data_dir, meeting_id, generation_id, segment_ids, riley["speaker_id"], kind)

    assert _versions(data_dir, meeting_id) == versions
    assert notes_state(data_dir, meeting_id)["revisions"][0]["source_state"] == state
    saved = get_note(data_dir, revision["id"])
    assert saved["source_state"] == state
    assert (saved["markdown"], saved["edit_version"]) == (revision["markdown"], 1)
    assert _stored(data_dir, revision) == before


@pytest.mark.parametrize(
    ("meeting_date", "attempts"),
    [("2026-10-19", ("2026-10-20", None)), (None, ("2026-10-20",))],
    ids=["change and delete a date", "add a date"],
)
def test_source_edits_stay_locked_until_the_worker_has_stopped_but_a_title_does_not(
    tmp_path, meeting_date, attempts
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(
        data_dir, meeting_date=meeting_date
    )
    riley = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        display_name="Riley",
    )
    accepted = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="notes-req-0001",
        expected_generation_id=generation_id,
        expected_source_version=riley["source_version"],
        expected_metadata_version=1,
        worker_ready=True,
    )
    transcript = {"expected_generation_id": generation_id, "expected_source_version": 2}

    def assert_locked() -> None:
        assert notes_state(data_dir, meeting_id)["source_locked"] is True
        edits = [
            lambda: update_current_generation_segment_text(
                data_dir, meeting_id, segment_ids[0], **transcript, text="Locked out."
            ),
            lambda: update_current_generation_speaker_name(
                data_dir, meeting_id, "SPEAKER_00", **transcript, display_name="Dana"
            ),
            lambda: add_current_generation_speaker(
                data_dir, meeting_id, **transcript, display_name="Casey"
            ),
            lambda: reassign_current_generation_speaker(
                data_dir,
                meeting_id,
                segment_ids[0],
                **transcript,
                expected_assignment_version=1,
                speaker_id=riley["speaker_id"],
            ),
            *[
                lambda value=value: update_meeting(
                    data_dir, meeting_id, expected_version=1, fields={"meeting_date": value}
                )
                for value in attempts
            ],
        ]
        for edit in edits:
            with pytest.raises((RecordingError, TaskError)) as locked:
                edit()
            assert locked.value.code == "notes_source_locked"
        assert _versions(data_dir, meeting_id) == (2, 1, 1)

    assert_locked()  # accepted
    claim_next_task(data_dir, owner_id="notes-worker")
    assert_locked()  # running
    # Canceling a running task only asks its worker to stop.
    assert cancel_task(data_dir, accepted["id"])["status"] == "stopping"
    assert_locked()
    renamed = update_meeting(
        data_dir, meeting_id, expected_version=1, fields={"title": "Renamed while stopping"}
    )
    assert renamed["title"] == "Renamed while stopping"
    assert notes_state(data_dir, meeting_id)["source_locked"] is True

    assert finish_canceled_task(
        data_dir, accepted["id"], owner_id="notes-worker", elapsed_seconds=0.1
    )
    assert notes_state(data_dir, meeting_id)["source_locked"] is False
    edited = update_current_generation_segment_text(
        data_dir, meeting_id, segment_ids[0], **transcript, text="Free once the worker stopped."
    )
    assert edited["source_version"] == 3


def test_an_edited_revision_returns_the_typed_text_and_names_its_broken_source_links(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    revision = _publish(data_dir, meeting_id, generation_id)
    assert revision["warnings"] == []
    address = f"/meetings/{meeting_id}?notes_source={revision['source_snapshot_id']}&segment="
    unknown = address + "f" * 32
    other_source = f"/meetings/{meeting_id}?notes_source={'e' * 32}&segment={segment_ids[0]}"
    malformed = f"/meetings/{meeting_id}?notes_source={revision['source_snapshot_id']}"
    typed = (
        "## Notes\n\n"
        f"- exact shape, unknown segment [00:00:09]({unknown}) end\n"
        f"- another snapshot [00:00:09]({other_source}) end\n"
        f"- named link [see source]({unknown}) end\n"
        f"- no segment [00:00:09]({malformed}) end\n"
        # Typed by hand, but each opens a segment of this revision's own snapshot.
        f"- valid [00:00:01]({address}{segment_ids[0]}) and [again]({address}{segment_ids[1]}"
        "#transcript)\n"
        "- plain text that names notes_source= is not a link\n"
    )
    warnings = [
        {"code": "source_link_invalid", "reason": reason, "href": href}
        for reason, href in (
            ("unknown_segment", unknown),
            ("other_source", other_source),
            ("unknown_segment", unknown),
            ("other_source", malformed),
        )
    ]

    saved = update_note(data_dir, revision["id"], expected_edit_version=1, markdown=typed)

    assert saved["markdown"] == typed and saved["warnings"] == warnings
    for read in (
        get_note(data_dir, revision["id"]),
        notes_state(data_dir, meeting_id)["revisions"][0],
    ):
        assert read["markdown"] == typed and read["warnings"] == warnings
    assert export_note(data_dir, revision["id"]) == typed
    assert _stored(data_dir, revision)[1] == typed

    # Fixing the link clears its warning; the warnings follow the Markdown, they are not stored.
    fixed = update_note(
        data_dir, revision["id"], expected_edit_version=2, markdown=typed.replace(unknown, "/x")
    )
    assert [item["href"] for item in fixed["warnings"]] == [other_source, malformed]


def test_a_model_document_with_a_broken_source_is_refused_and_a_typed_link_is_not(tmp_path):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir)
    accepted = _accept(data_dir, meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="notes-worker")
    with sqlite3.connect(database_path(data_dir)) as connection:
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
    unknown = "f" * 32

    document = default_cited_document(segment_ids)
    document["decisions"][0]["source_ids"] = [unknown]
    with pytest.raises(NotesDocumentError, match="source IDs"):
        validate_document(document, get_notes_source(data_dir, source_id))
    with pytest.raises(TaskError) as refused:
        publish_notes_revision(
            data_dir,
            accepted["id"],
            owner_id="notes-worker",
            markdown=_markdown(),
            elapsed_seconds=1.0,
            citations=[
                {
                    "section": "Decisions",
                    "text": r"Prepare the launch plan\.",
                    "sources": [{"id": unknown, "start_seconds": 1.0}],
                }
            ],
        )
    assert refused.value.code == "invalid_citations"
    assert notes_state(data_dir, meeting_id)["revisions"] == []

    revision = publish_notes_revision(
        data_dir, accepted["id"], owner_id="notes-worker", markdown=_markdown(), elapsed_seconds=1
    )
    assert revision is not None and revision["warnings"] == []
    link = f"[00:00:01](/meetings/{meeting_id}?notes_source={source_id}&segment={unknown})"
    saved = update_note(
        data_dir, revision["id"], expected_edit_version=1, markdown=f"- Typed {link}\n"
    )
    assert saved["markdown"] == f"- Typed {link}\n"
    assert [item["reason"] for item in saved["warnings"]] == ["unknown_segment"]
