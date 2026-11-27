from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from asr_studio.extension_auth import ExtensionSessionIdentity
from asr_studio.extension_ingest import (
    ExtensionIngestError,
    create_recording_session,
    publish_ready_derivative,
)
from asr_studio.processing import (
    ProcessingError,
    _connect,
    admit_automatic_transcription,
    apply_current_settings,
    processing_state,
    reconcile_stage_policy_gate,
    retry_notes,
    use_current_source,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    accept_transcription_task,
    claim_next_task,
    finish_interrupted_task,
    publish_complete_generation,
)

_MISSING = object()


def _identity(owner: str) -> ExtensionSessionIdentity:
    return ExtensionSessionIdentity(
        device_record_id=f"device-{owner}",
        owner_id=owner,
        device_id=f"extension-{owner}",
        server_url="http://127.0.0.1:8765",
        generation=1,
        scope="ingest",
    )


def _payload(client_id: str, *, stage_policy: object = _MISSING) -> dict[str, object]:
    payload: dict[str, object] = {
        "client_recording_id": client_id,
        "capture_epoch": f"capture-{client_id}",
        "mime_type": "audio/webm;codecs=opus",
        "title": "Stage policy capture",
        "metadata": {},
    }
    if stage_policy is not _MISSING:
        payload["stage_policy"] = stage_policy
    return payload


def test_capable_create_pins_only_format_intent_and_keeps_replay_fingerprint(settings):
    initialize(settings.data_dir)
    owner = _identity("owner-stage-policy-0001")
    legacy, legacy_status = create_recording_session(
        settings.data_dir,
        identity=_identity("owner-stage-policy-legacy"),
        payload=_payload("legacy-shape"),
        backend_id="b" * 32,
        settings=settings,
    )
    assert legacy_status == 201
    assert "stage_policy" not in legacy
    created, status = create_recording_session(
        settings.data_dir,
        identity=owner,
        payload=_payload("capable-1", stage_policy={"format_version": 1}),
        backend_id="b" * 32,
        settings=settings,
    )
    assert status == 201
    assert created["stage_policy"] == {"format_version": 1, "intent": {"format_version": 1}}

    replay, replay_status = create_recording_session(
        settings.data_dir,
        identity=owner,
        payload=_payload("capable-1", stage_policy={"format_version": 1}),
        backend_id="b" * 32,
        settings=settings,
    )
    assert replay_status == 200
    assert replay["id"] == created["id"]

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute(
            "SELECT stage_policy_format, stage_policy_intent_json "
            "FROM extension_recording_sessions WHERE id = ?",
            (created["id"],),
        ).fetchone() == (1, json.dumps({"format_version": 1}, separators=(",", ":")))


def test_unknown_or_malformed_create_intent_never_downgrades_to_legacy(settings):
    initialize(settings.data_dir)
    owner = _identity("owner-stage-policy-0002")
    for value, code in (
        ({"format_version": 99}, "unsupported_stage_policy_format"),
        ({"format_version": 1, "asr": True}, "invalid_stage_policy_intent"),
        (None, "invalid_stage_policy_intent"),
    ):
        with pytest.raises(ExtensionIngestError) as raised:
            create_recording_session(
                settings.data_dir,
                identity=owner,
                payload=_payload(f"invalid-{code}", stage_policy=value),
                backend_id="b" * 32,
                settings=settings,
            )
        assert raised.value.code == code


def _ready_capable_workflow(tmp_path, *, asr: bool, diarization: bool, notes: bool):
    data_dir = tmp_path / f"data-{int(asr)}-{int(diarization)}-{int(notes)}"
    initialize(data_dir)
    meeting_id = f"meeting-stage-{int(asr)}{int(diarization)}{int(notes)}"
    session_id = f"session-stage-{int(asr)}{int(diarization)}{int(notes)}".ljust(32, "s")
    recording_id = f"recording-stage-{int(asr)}{int(diarization)}{int(notes)}".ljust(32, "r")
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_preferences SET stage_policy_format = 1, "
            "stage_asr_enabled = ?, stage_diarization_enabled = ?, stage_notes_enabled = ? "
            "WHERE id = 1",
            (int(asr), int(diarization), int(notes)),
        )
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES (?, 'Stage fixture', 'vi')",
            (meeting_id,),
        )
        connection.execute(
            "INSERT INTO extension_recording_sessions("
            "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, language_hint, "
            "language_hint_omitted, mime_type, state, epoch, next_sequence, next_offset, "
            "stage_policy_format, stage_policy_intent_json) "
            "VALUES (?, ?, ?, ?, ?, 'vi', 1, 'audio/webm;codecs=opus', 'archived', 1, 0, 0, 1, ?)",
            (
                session_id,
                "o" * 16,
                f"client-{session_id}",
                f"capture-{session_id}",
                "f" * 64,
                json.dumps({"format_version": 1}, separators=(",", ":")),
            ),
        )
        connection.execute(
            "INSERT INTO recordings(id, meeting_id, extension_session_id, storage_key, "
            "content_type, duration_seconds) "
            "VALUES (?, ?, ?, NULL, NULL, NULL)",
            (recording_id, meeting_id, session_id),
        )
        connection.commit()
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"recordings/{session_id}.m4a",
            duration_seconds=12.0,
        )
    return data_dir, meeting_id


@pytest.mark.parametrize(
    "asr,diarization,notes",
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
def test_ready_handoff_pins_all_eight_stage_combinations(tmp_path, asr, diarization, notes):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=asr, diarization=diarization, notes=notes
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["stage_policy"] == {
        "format_version": 1,
        "asr": asr,
        "diarization": diarization,
        "notes": notes,
    }
    steps = {step["kind"]: step for step in state["steps"]}
    if not asr:
        assert state["state"] == "succeeded"
        assert steps["transcription"]["state"] == "skipped"
        assert steps["notes"]["state"] == "skipped"
        assert state["attempts"] == []
    else:
        assert steps["transcription"]["state"] == "waiting"
        if notes:
            assert steps["notes"]["state"] == "waiting"
            assert len(state["attempts"]) == 2
        else:
            assert steps["notes"]["state"] == "skipped"
            assert len(state["attempts"]) == 1


def test_automatic_diarization_off_pins_asr_only_task_without_community_dependency(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=False
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        result = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="h" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert result is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT diarization_mode, diarization_model_path FROM tasks WHERE id = ?",
            (result["task_id"],),
        ).fetchone() == ("off", "")


def test_ready_fence_retains_b_snapshot_until_gate_reopens_and_later_defaults_are_future_only(
    settings,
):
    initialize(settings.data_dir)
    owner = _identity("owner-stage-policy-0003")
    payload = _payload("fenced-1", stage_policy={"format_version": 1})
    created, status = create_recording_session(
        settings.data_dir,
        identity=owner,
        payload=payload,
        backend_id="b" * 32,
        settings=settings,
    )
    assert status == 201
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        session = connection.execute(
            "SELECT meeting_id FROM recordings WHERE extension_session_id = ?",
            (created["id"],),
        ).fetchone()
        assert session is not None
        meeting_id = session[0]
        connection.execute(
            "UPDATE processing_preferences SET stage_policy_format = 1, "
            "stage_asr_enabled = 1, stage_diarization_enabled = 0, stage_notes_enabled = 1 "
            "WHERE id = 1"
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'archived' WHERE id = ?",
            (created["id"],),
        )
        connection.execute(
            "UPDATE recordings SET storage_key = ?, content_type = 'audio/mp4', "
            "duration_seconds = 12 "
            "WHERE extension_session_id = ?",
            (f"recordings/{created['id']}.m4a", created["id"]),
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET state = 'ready' WHERE id = ?",
            (created["id"],),
        )
        connection.commit()
    # Use the real ready handoff helper so receipt/derivative/workflow are one transaction.
    with _connect(settings.data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        from asr_studio.processing import ready_handoff_tx

        assert ready_handoff_tx(connection, created["id"]) is not None
    state = processing_state(settings.data_dir, meeting_id)
    assert state is not None
    assert state["state"] == "needs_attention"
    assert state["stage_policy"] == {
        "format_version": 1,
        "asr": True,
        "diarization": False,
        "notes": True,
    }
    assert (
        next(step for step in state["steps"] if step["kind"] == "transcription")["waiting_reason"]
        == "stage_policy_admission_disabled"
    )
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        connection.execute(
            "UPDATE processing_preferences SET stage_asr_enabled = 0, "
            "stage_diarization_enabled = 0, stage_notes_enabled = 0 WHERE id = 1"
        )
        connection.commit()
    unchanged = processing_state(settings.data_dir, meeting_id)
    assert unchanged is not None
    assert unchanged["stage_policy"] == state["stage_policy"]

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.commit()
    assert reconcile_stage_policy_gate(settings.data_dir) == 1
    reopened = processing_state(settings.data_dir, meeting_id)
    assert reopened is not None
    assert reopened["state"] == "waiting"
    assert reopened["stage_policy"] == state["stage_policy"]


def test_notes_off_keeps_transcript_and_never_admits_automatic_notes(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=False
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="n" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    claimed = claim_next_task(data_dir, owner_id="stage-off-owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    generation = publish_complete_generation(
        data_dir,
        claimed["id"],
        owner_id="stage-off-owner",
        segments=[{"id": "a" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Transcript."}],
        alignment_policy=None,
        diarization_model_name=None,
        language_probability=None,
        elapsed_seconds=0.1,
    )
    assert generation is not None
    final_state = processing_state(data_dir, meeting_id)
    assert final_state is not None
    assert final_state["state"] == "succeeded"
    assert (
        next(step for step in final_state["steps"] if step["kind"] == "notes")["state"] == "skipped"
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE processing_workflow_id = ? AND kind = 'notes'",
            (state["id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM notes_sources WHERE meeting_id = ?", (meeting_id,)
        ).fetchone() == (0,)


def test_asr_off_preserves_skipped_workflow_after_later_manual_transcript(tmp_path, monkeypatch):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=False, diarization=False, notes=True
    )
    monkeypatch.setattr("asr_studio.tasks.prepared_engine_directory", lambda *_args: None)
    monkeypatch.setattr(
        "asr_studio.tasks.prepared_diarization_directory",
        lambda path: path,
    )
    accepted = accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id="manual-after-off-01",
        model_path=Path("/models/whisper-small"),
        language_hint="vi",
        device="cpu",
        compute_type="int8",
        worker_ready=True,
        asr_model="whisper-small",
        asr_choice="whisper-small",
    )
    claimed = claim_next_task(data_dir, owner_id="manual-after-off-owner", role="transcription")
    assert claimed is not None and claimed["id"] == accepted["id"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    assert publish_complete_generation(
        data_dir,
        claimed["id"],
        owner_id="manual-after-off-owner",
        segments=[{"id": "c" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Manual."}],
        language_probability=None,
        elapsed_seconds=0.1,
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["state"] == "succeeded"
    assert all(step["state"] == "skipped" for step in state["steps"])


def test_disabled_gate_fences_retry_settings_and_source_adoption_before_rearming(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=True
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    notes_step = next(step for step in state["steps"] if step["kind"] == "notes")
    source_snapshot_id = "s" * 32
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'failed', "
            "waiting_reason = 'provider_unavailable', "
            "source_snapshot_id = ?, recipe_version = 1 WHERE id = ?",
            (source_snapshot_id, notes_step["id"]),
        )
        connection.commit()

    with pytest.raises(ProcessingError) as retry_error:
        retry_notes(
            data_dir,
            meeting_id,
            request_id="retry-gate-fence-01",
            expected_epoch=state["epoch"],
            source_snapshot_id=source_snapshot_id,
            confirmed_recipe_version=1,
        )
    assert retry_error.value.code == "stage_policy_admission_disabled"

    with pytest.raises(ProcessingError) as settings_error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=state["epoch"],
            expected_preferences_version=state["preferences_version"],
        )
    assert settings_error.value.code == "stage_policy_admission_disabled"

    current_source = state["current_source"]
    assert current_source is not None
    with pytest.raises(ProcessingError) as source_error:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=state["epoch"],
            expected_generation_id=current_source["generation_id"],
            expected_source_version=current_source["source_version"],
            expected_metadata_version=current_source["metadata_version"],
            expected_notes_source_version=current_source["notes_source_version"],
        )
    assert source_error.value.code == "stage_policy_admission_disabled"

    after = processing_state(data_dir, meeting_id)
    assert after is not None
    assert after["epoch"] == state["epoch"]
    assert len(after["attempts"]) == len(state["attempts"])


@pytest.mark.parametrize("claim_running", [False, True])
def test_gate_reconcile_drains_accepted_or_running_capable_task(tmp_path, claim_running):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=False
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="g" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    task_id = admitted["task_id"]
    owner_id = "gate-drain-owner"
    if claim_running:
        claimed = claim_next_task(data_dir, owner_id=owner_id, role="transcription")
        assert claimed is not None and claimed["id"] == task_id
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()

    assert reconcile_stage_policy_gate(data_dir) == 1
    with sqlite3.connect(database_path(data_dir)) as connection:
        task = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        slot = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()
    assert task == (("stopping" if claim_running else "canceled"),)
    assert slot == ((task_id if claim_running else None),)
    fenced_state = processing_state(data_dir, meeting_id)
    assert fenced_state is not None
    assert (
        next(step for step in fenced_state["steps"] if step["kind"] == "notes")["state"]
        == "skipped"
    )

    if claim_running:
        assert finish_interrupted_task(
            data_dir,
            task_id,
            owner_id=owner_id,
            elapsed_seconds=0.1,
            message="Stage policy gate disabled.",
        )
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone() == ("canceled",)
            assert connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone() == (None,)
