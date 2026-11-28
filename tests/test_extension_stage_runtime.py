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
    processing_stage_policy,
    processing_stage_policy_status,
    processing_state,
    reconcile_stage_policy_gate,
    retry_notes,
    set_processing_stage_gate,
    use_current_source,
)
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    accept_transcription_task,
    claim_next_task,
    finish_interrupted_task,
    publish_complete_generation,
    recover_abandoned_tasks,
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


def _ready_capable_workflow(
    tmp_path,
    *,
    asr: bool,
    diarization: bool,
    notes: bool,
    finalize_capture_complete: bool = True,
    source_changed: bool = False,
    gate_admission: str = "enabled",
    preference_format: int = 1,
    session_format: int = 1,
):
    data_dir = tmp_path / f"data-{int(asr)}-{int(diarization)}-{int(notes)}"
    initialize(data_dir)
    meeting_id = f"meeting-stage-{int(asr)}{int(diarization)}{int(notes)}"
    session_id = f"session-stage-{int(asr)}{int(diarization)}{int(notes)}".ljust(32, "s")
    recording_id = f"recording-stage-{int(asr)}{int(diarization)}{int(notes)}".ljust(32, "r")
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_preferences SET stage_policy_format = ?, "
            "stage_asr_enabled = ?, stage_diarization_enabled = ?, stage_notes_enabled = ? "
            "WHERE id = 1",
            (preference_format, int(asr), int(diarization), int(notes)),
        )
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = ?, reason = ? WHERE id = 1",
            (gate_admission, None if gate_admission == "enabled" else "maintenance"),
        )
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES (?, 'Stage fixture', 'vi')",
            (meeting_id,),
        )
        if source_changed:
            connection.execute(
                "UPDATE meetings SET metadata_version = 2, notes_source_version = 2 WHERE id = ?",
                (meeting_id,),
            )
        connection.execute(
            "INSERT INTO extension_recording_sessions("
            "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, language_hint, "
            "language_hint_omitted, mime_type, state, epoch, next_sequence, next_offset, "
            "stage_policy_format, stage_policy_intent_json) "
            "VALUES (?, ?, ?, ?, ?, 'vi', 1, 'audio/webm;codecs=opus', 'archived', 1, 0, 0, ?, ?)",
            (
                session_id,
                "o" * 16,
                f"client-{session_id}",
                f"capture-{session_id}",
                "f" * 64,
                session_format,
                None
                if session_format == 0
                else json.dumps({"format_version": 1}, separators=(",", ":")),
            ),
        )
        connection.execute(
            "UPDATE extension_recording_sessions SET finalize_capture_complete = ? WHERE id = ?",
            (int(finalize_capture_complete), session_id),
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


def _processing_storage_snapshot(data_dir, meeting_id):
    with sqlite3.connect(database_path(data_dir)) as connection:
        workflow = connection.execute(
            "SELECT id, state, epoch, last_error_code, last_error_message, updated_at "
            "FROM processing_workflows WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
        assert workflow is not None
        workflow_id = workflow[0]
        steps = connection.execute(
            "SELECT kind, state, waiting_reason, task_id, current_attempt_id, "
            "source_snapshot_id, source_generation_id, source_version, "
            "notes_source_version, metadata_version, recipe_version, updated_at "
            "FROM processing_steps WHERE workflow_id = ? ORDER BY kind",
            (workflow_id,),
        ).fetchall()
        attempts = connection.execute(
            "SELECT step_id, attempt_number, state, task_id, error_code, updated_at "
            "FROM processing_attempts WHERE step_id IN "
            "(SELECT id FROM processing_steps WHERE workflow_id = ?) "
            "ORDER BY step_id, attempt_number",
            (workflow_id,),
        ).fetchall()
        outbox = connection.execute(
            "SELECT event_key, event_type, payload_json, processed_at "
            "FROM processing_outbox WHERE workflow_id = ? ORDER BY event_key",
            (workflow_id,),
        ).fetchall()
        notes_sources = connection.execute(
            "SELECT id, generation_id, source_version, notes_source_version, snapshot_json "
            "FROM notes_sources WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
    return workflow, steps, attempts, outbox, notes_sources


def _add_notes_source_marker(data_dir, meeting_id):
    source_snapshot_id = "s" * 32
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_steps SET source_snapshot_id = ?, recipe_version = 1 "
            "WHERE workflow_id = (SELECT id FROM processing_workflows WHERE meeting_id = ?) "
            "AND kind = 'notes'",
            (source_snapshot_id, meeting_id),
        )
        connection.commit()
    return source_snapshot_id


def _assert_gate_rejected_actions(data_dir, meeting_id, state, source_snapshot_id, expected_reason):
    with pytest.raises(ProcessingError) as retry_error:
        retry_notes(
            data_dir,
            meeting_id,
            request_id="retry-gate-projection-01",
            expected_epoch=state["epoch"],
            source_snapshot_id=source_snapshot_id,
            confirmed_recipe_version=1,
        )
    assert retry_error.value.code == expected_reason

    with pytest.raises(ProcessingError) as settings_error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=state["epoch"],
            expected_preferences_version=state["preferences_version"],
        )
    assert settings_error.value.code == expected_reason

    with pytest.raises(ProcessingError) as source_error:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=state["epoch"],
            expected_generation_id=None,
            expected_source_version=None,
            expected_metadata_version=1,
            expected_notes_source_version=1,
        )
    assert source_error.value.code == expected_reason


def test_disabled_gate_projection_reports_hold_and_fences_three_mutation_actions(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
        gate_admission="disabled",
    )
    source_snapshot_id = _add_notes_source_marker(data_dir, meeting_id)
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["state"] == "needs_attention"
    assert state["action_blocked_reason"] == "stage_policy_admission_disabled"
    assert state["actions"] == {
        "cancel": True,
        "retry_notes": False,
        "apply_current_settings": False,
        "use_current_source": False,
    }

    before = _processing_storage_snapshot(data_dir, meeting_id)
    _assert_gate_rejected_actions(
        data_dir,
        meeting_id,
        state,
        source_snapshot_id,
        "stage_policy_admission_disabled",
    )
    assert _processing_storage_snapshot(data_dir, meeting_id) == before


def test_live_gate_projection_overrides_stale_source_reason_without_get_mutation(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
        source_changed=True,
    )
    _add_notes_source_marker(data_dir, meeting_id)
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    assert initial["action_blocked_reason"] is None
    assert initial["actions"] == {
        "cancel": True,
        "retry_notes": True,
        "apply_current_settings": True,
        "use_current_source": True,
    }
    assert all(
        step["waiting_reason"] == "source_confirmation_required" for step in initial["steps"]
    )

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()
    before = _processing_storage_snapshot(data_dir, meeting_id)

    held = processing_state(data_dir, meeting_id)
    assert held is not None
    assert held["state"] == "needs_attention"
    assert held["action_blocked_reason"] == "stage_policy_admission_disabled"
    assert held["actions"] == {
        "cancel": True,
        "retry_notes": False,
        "apply_current_settings": False,
        "use_current_source": False,
    }
    assert all(step["waiting_reason"] == "source_confirmation_required" for step in held["steps"])
    assert _processing_storage_snapshot(data_dir, meeting_id) == before


@pytest.mark.parametrize(
    ("quarantine_format", "expected_reason"),
    [(1, "unknown_stage_policy"), (99, "unknown_stage_policy_format")],
)
def test_quarantined_policy_projection_uses_specific_reason_and_blocks_actions(
    tmp_path, quarantine_format, expected_reason
):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
        source_changed=True,
    )
    _add_notes_source_marker(data_dir, meeting_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_workflows SET stage_policy_quarantine_format = ? "
            "WHERE meeting_id = ?",
            (quarantine_format, meeting_id),
        )
        connection.commit()

    held = processing_state(data_dir, meeting_id)
    assert held is not None
    assert held["action_blocked_reason"] == expected_reason
    assert held["actions"] == {
        "cancel": True,
        "retry_notes": False,
        "apply_current_settings": False,
        "use_current_source": False,
    }
    assert all(step["waiting_reason"] == "source_confirmation_required" for step in held["steps"])


def test_unknown_gate_projection_uses_guard_reason_and_preserves_cancel(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
        source_changed=True,
    )
    source_snapshot_id = _add_notes_source_marker(data_dir, meeting_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE processing_cutover_gate SET format_version = 99 WHERE id = 1")
        connection.commit()

    held = processing_state(data_dir, meeting_id)
    assert held is not None
    assert held["action_blocked_reason"] == "unknown_stage_policy_gate"
    assert held["actions"] == {
        "cancel": True,
        "retry_notes": False,
        "apply_current_settings": False,
        "use_current_source": False,
    }
    _assert_gate_rejected_actions(
        data_dir, meeting_id, held, source_snapshot_id, "unknown_stage_policy_gate"
    )


def test_legacy_format_zero_keeps_actions_when_capable_gate_is_disabled(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=True,
        notes=True,
        gate_admission="disabled",
        session_format=0,
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["state"] == "waiting"
    assert state["stage_policy"] == {
        "format_version": 0,
        "asr": True,
        "diarization": True,
        "notes": True,
    }
    assert state["action_blocked_reason"] is None
    assert state["actions"] == {
        "cancel": True,
        "retry_notes": False,
        "apply_current_settings": True,
        "use_current_source": False,
    }


def test_terminal_skipped_projection_keeps_state_when_gate_changes(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=False,
        diarization=False,
        notes=True,
    )
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    assert initial["state"] == "succeeded"
    assert all(step["state"] == "skipped" for step in initial["steps"])

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()

    terminal = processing_state(data_dir, meeting_id)
    assert terminal is not None
    assert terminal["state"] == "succeeded"
    assert terminal["action_blocked_reason"] == "stage_policy_admission_disabled"
    assert all(step["state"] == "skipped" for step in terminal["steps"])
    assert terminal["actions"] == {
        "cancel": False,
        "retry_notes": False,
        "apply_current_settings": False,
        "use_current_source": False,
    }


def test_diarization_rollback_fence_does_not_block_notes_or_source_actions(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
    )
    _add_notes_source_marker(data_dir, meeting_id)
    with sqlite3.connect(database_path(data_dir)) as connection:
        workflow_id = connection.execute(
            "SELECT id FROM processing_workflows WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE processing_workflows SET state = 'needs_attention', "
            "last_error_code = 'diarization_rollback_fence' WHERE id = ?",
            (workflow_id,),
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'needs_attention', "
            "waiting_reason = 'diarization_rollback_fence' "
            "WHERE workflow_id = ? AND kind = 'transcription'",
            (workflow_id,),
        )
        connection.execute(
            "UPDATE processing_steps SET state = 'needs_attention', "
            "waiting_reason = 'provider_unavailable' "
            "WHERE workflow_id = ? AND kind = 'notes'",
            (workflow_id,),
        )
        connection.commit()

    state = processing_state(data_dir, meeting_id)
    assert state is not None
    assert state["action_blocked_reason"] is None
    assert state["actions"] == {
        "cancel": True,
        "retry_notes": True,
        "apply_current_settings": True,
        "use_current_source": True,
    }
    assert (
        next(step for step in state["steps"] if step["kind"] == "transcription")["waiting_reason"]
        == "diarization_rollback_fence"
    )


def test_claim_next_task_preserves_specific_unknown_format_quarantine_reason(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=False,
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(item for item in state["attempts"] if item["step_id"] == transcription["id"])
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="q" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_workflows SET stage_policy_quarantine_format = 99 WHERE id = ?",
            (state["id"],),
        )
        connection.commit()

    assert (
        claim_next_task(data_dir, owner_id="quarantine-specific-owner", role="transcription")
        is None
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM tasks WHERE id = ?", (admitted["task_id"],)
        ).fetchone() == ("canceled", "unknown_stage_policy_format")


@pytest.mark.parametrize(
    ("finalize_capture_complete", "source_changed", "attention_reason"),
    [
        (False, False, "interrupted_recording"),
        (True, True, "source_confirmation_required"),
    ],
)
def test_gate_toggle_preserves_non_gate_attention_holds(
    tmp_path, finalize_capture_complete, source_changed, attention_reason
):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=True,
        diarization=False,
        notes=True,
        finalize_capture_complete=finalize_capture_complete,
        source_changed=source_changed,
    )
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    assert initial["state"] == "needs_attention"
    assert all(step["waiting_reason"] == attention_reason for step in initial["steps"])
    initial_step_ids = {step["kind"]: step["id"] for step in initial["steps"]}
    initial_attempt_ids = {attempt["step_id"]: attempt["id"] for attempt in initial["attempts"]}

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()

    for _ in range(2):
        assert reconcile_stage_policy_gate(data_dir) == 1
        held = processing_state(data_dir, meeting_id)
        assert held is not None
        assert held["state"] == "needs_attention"
        assert all(step["waiting_reason"] == attention_reason for step in held["steps"])

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.commit()

    reconcile_stage_policy_gate(data_dir)
    reopened = processing_state(data_dir, meeting_id)
    assert reopened is not None
    assert reopened["state"] == "needs_attention"
    assert all(step["waiting_reason"] == attention_reason for step in reopened["steps"])
    assert {step["kind"]: step["id"] for step in reopened["steps"]} == initial_step_ids
    assert {
        attempt["step_id"]: attempt["id"] for attempt in reopened["attempts"]
    } == initial_attempt_ids


@pytest.mark.parametrize("preference_format", [0, 99])
def test_invalid_preference_format_remains_quarantined_across_gate_and_actions(
    tmp_path, preference_format
):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=False,
        diarization=False,
        notes=False,
        preference_format=preference_format,
    )
    stage_api = processing_stage_policy(data_dir)
    assert stage_api["admission"] == {
        "state": "disabled",
        "reason": "unknown_stage_policy_format",
        "format_version": preference_format,
    }
    initial = processing_state(data_dir, meeting_id)
    assert initial is not None
    assert initial["state"] == "needs_attention"
    assert initial["stage_policy"] == {
        "format_version": 1,
        "known": False,
        "quarantined_format": preference_format,
    }
    assert all(step["waiting_reason"] == "unknown_stage_policy_format" for step in initial["steps"])
    assert initial["attempts"] == []
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT stage_policy_format, stage_policy_quarantine_format "
            "FROM processing_workflows WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone() == (1, preference_format)

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()
    reconcile_stage_policy_gate(data_dir)
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.commit()
    reconcile_stage_policy_gate(data_dir)

    held = processing_state(data_dir, meeting_id)
    assert held is not None
    assert held["state"] == "needs_attention"
    assert held["stage_policy"] == initial["stage_policy"]
    assert all(step["waiting_reason"] == "unknown_stage_policy_format" for step in held["steps"])
    assert held["attempts"] == []

    with pytest.raises(ProcessingError) as retry_error:
        retry_notes(
            data_dir,
            meeting_id,
            request_id="quarantine-retry-01",
            expected_epoch=held["epoch"],
            source_snapshot_id="s" * 32,
            confirmed_recipe_version=1,
        )
    assert retry_error.value.code == "unknown_stage_policy_format"
    with pytest.raises(ProcessingError) as settings_error:
        apply_current_settings(
            data_dir,
            meeting_id,
            expected_epoch=held["epoch"],
            expected_preferences_version=held["preferences_version"],
        )
    assert settings_error.value.code == "unknown_stage_policy_format"
    with pytest.raises(ProcessingError) as source_error:
        use_current_source(
            data_dir,
            meeting_id,
            expected_epoch=held["epoch"],
            expected_generation_id=None,
            expected_source_version=None,
            expected_metadata_version=1,
            expected_notes_source_version=1,
        )
    assert source_error.value.code == "unknown_stage_policy_format"
    assert claim_next_task(data_dir, owner_id="quarantine-owner", role="transcription") is None
    assert recover_abandoned_tasks(data_dir, role="transcription") == []
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)


def test_stage_policy_status_reports_own_slot_until_disable_reconciles_accepted_work(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=False
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(item for item in state["attempts"] if item["step_id"] == transcription["id"])
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="s" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    accepted = processing_stage_policy_status(data_dir)
    assert accepted["active"]["accepted"] == 1
    assert accepted["active"]["running"] == 0
    assert accepted["active"]["stopping"] == 0
    assert accepted["slot"]["active_task_id"] == admitted["task_id"]
    assert accepted["slot"]["owned_by_stage_policy"] is True
    assert accepted["drained"] is False

    persisted = set_processing_stage_gate(data_dir, admission="disabled", reason="operator-drain")
    assert persisted["admission"]["state"] == "disabled"
    assert persisted["drained"] is False
    assert persisted["active"]["accepted"] == 1

    assert reconcile_stage_policy_gate(data_dir) == 1
    drained = processing_stage_policy_status(data_dir)
    assert drained["active"] == {"accepted": 0, "running": 0, "stopping": 0, "tasks": []}
    assert drained["slot"] == {
        "active_task_id": None,
        "free": True,
        "owned_by_stage_policy": False,
    }
    assert drained["drained"] is True


@pytest.mark.parametrize(
    ("diarization", "notes"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_disabled_gate_fences_asr_off_and_reenable_is_truthful_media_only(
    tmp_path, diarization, notes
):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path,
        asr=False,
        diarization=diarization,
        notes=notes,
        gate_admission="disabled",
    )
    fenced = processing_state(data_dir, meeting_id)
    assert fenced is not None
    assert fenced["state"] == "needs_attention"
    assert all(
        step["state"] == "skipped" and step["waiting_reason"].startswith("skipped_")
        for step in fenced["steps"]
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        recording = connection.execute(
            "SELECT storage_key, content_type, duration_seconds "
            "FROM recordings WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
        assert recording[0] is not None and recording[1] is not None and recording[2] == 12.0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.commit()

    reconcile_stage_policy_gate(data_dir)
    enabled = processing_state(data_dir, meeting_id)
    assert enabled is not None
    assert enabled["state"] == "succeeded"
    assert all(step["state"] == "skipped" for step in enabled["steps"])
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)


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


def test_automatic_diarization_off_waits_for_rollback_fence_without_claiming_slot(
    tmp_path, monkeypatch
):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=False
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "1")

    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        result = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=attempt["request_id"],
            request_hash="r" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert result is None

    waiting = processing_state(data_dir, meeting_id)
    assert waiting is not None
    transcription = next(step for step in waiting["steps"] if step["kind"] == "transcription")
    assert transcription["state"] == "waiting"
    assert transcription["waiting_reason"] == "diarization_rollback_fence"
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)


def test_automatic_diarization_off_claim_releases_its_slot_when_fence_appears(
    tmp_path, monkeypatch
):
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
            request_hash="c" * 64,
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
    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "1")

    assert claim_next_task(data_dir, owner_id="rollback-owner", role="transcription") is None

    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM tasks WHERE id = ?", (task_id,)
        ).fetchone() == ("canceled", "diarization_rollback_fence")
        assert connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone() == (None,)
    settled = processing_state(data_dir, meeting_id)
    assert settled is not None
    transcription = next(step for step in settled["steps"] if step["kind"] == "transcription")
    assert transcription["state"] == "waiting"
    assert transcription["waiting_reason"] == "diarization_rollback_fence"
    assert settled["state"] == "waiting"
    assert any(
        attempt["state"] == "waiting" and attempt["attempt_number"] == 2
        for attempt in settled["attempts"]
    )


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


def test_gate_toggle_preserves_completed_transcript_and_notes_waiting(tmp_path):
    data_dir, meeting_id = _ready_capable_workflow(
        tmp_path, asr=True, diarization=False, notes=True
    )
    state = processing_state(data_dir, meeting_id)
    assert state is not None
    transcription = next(step for step in state["steps"] if step["kind"] == "transcription")
    transcription_attempt = next(
        attempt for attempt in state["attempts"] if attempt["step_id"] == transcription["id"]
    )
    with _connect(data_dir) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admitted = admit_automatic_transcription(
            connection,
            workflow_id=state["id"],
            epoch=state["epoch"],
            request_id=transcription_attempt["request_id"],
            request_hash="c" * 64,
            model_path=Path("/models/whisper-small"),
            asr_model="whisper-small",
            language_hint="vi",
            device="cpu",
            compute_type="int8",
            diarization_model_path=Path("/missing/community-1"),
            duration_seconds=12.0,
        )
    assert admitted is not None
    claimed = claim_next_task(data_dir, owner_id="gate-complete-owner", role="transcription")
    assert claimed is not None
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE tasks SET stage = 'publishing' WHERE id = ?", (claimed["id"],))
    assert publish_complete_generation(
        data_dir,
        claimed["id"],
        owner_id="gate-complete-owner",
        segments=[{"id": "d" * 32, "start_seconds": 0, "end_seconds": 1, "text": "Complete."}],
        alignment_policy=None,
        diarization_model_name=None,
        language_probability=None,
        elapsed_seconds=0.1,
    )
    complete = processing_state(data_dir, meeting_id)
    assert complete is not None
    notes = next(step for step in complete["steps"] if step["kind"] == "notes")
    assert complete["state"] == "waiting"
    assert next(step for step in complete["steps"] if step["kind"] == "transcription")["state"] == (
        "succeeded"
    )
    assert notes["state"] == "waiting"
    assert notes["waiting_reason"] == "provider_unconfigured"
    attempt_ids = {attempt["step_id"]: attempt["id"] for attempt in complete["attempts"]}

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'disabled', reason = 'maintenance' "
            "WHERE id = 1"
        )
        connection.commit()
    assert reconcile_stage_policy_gate(data_dir) == 1
    disabled = processing_state(data_dir, meeting_id)
    assert disabled is not None
    assert disabled["state"] == "needs_attention"
    assert next(step for step in disabled["steps"] if step["kind"] == "transcription")["state"] == (
        "succeeded"
    )
    disabled_notes = next(step for step in disabled["steps"] if step["kind"] == "notes")
    assert disabled_notes["waiting_reason"] == "provider_unconfigured"

    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute(
            "UPDATE processing_cutover_gate SET admission = 'enabled', reason = NULL WHERE id = 1"
        )
        connection.commit()
    assert reconcile_stage_policy_gate(data_dir) == 1
    reopened = processing_state(data_dir, meeting_id)
    assert reopened is not None
    assert reopened["state"] == "waiting"
    assert next(step for step in reopened["steps"] if step["kind"] == "transcription")["state"] == (
        "succeeded"
    )
    reopened_notes = next(step for step in reopened["steps"] if step["kind"] == "notes")
    assert reopened_notes["waiting_reason"] == "provider_unconfigured"
    assert {attempt["step_id"]: attempt["id"] for attempt in reopened["attempts"]} == attempt_ids


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
