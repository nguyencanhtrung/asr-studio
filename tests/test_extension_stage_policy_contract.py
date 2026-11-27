from __future__ import annotations

import pytest

from asr_studio.auth import AuthStore
from asr_studio.storage import database_path, initialize


def _origin(settings):
    return {"Origin": settings.public_url}


async def _login(client, settings):
    token = AuthStore(settings.auth_dir).load().token
    response = await client.post(
        "/api/access/session", headers=_origin(settings), json={"token": token}
    )
    assert response.status_code == 204, response.text


@pytest.mark.anyio
async def test_stage_policy_route_is_authenticated_versioned_and_strict(client, settings):
    unauthenticated = await client.get("/api/processing-stage-policy/v1")
    assert unauthenticated.status_code == 401

    await _login(client, settings)
    initial = await client.get("/api/processing-stage-policy/v1")
    assert initial.status_code == 200, initial.text
    body = initial.json()
    assert body["contract_version"] == 1
    assert body["format_version"] == 1
    assert body["preference_version"] == 1
    assert body["policy"] == {"asr": True, "diarization": True, "notes": True}
    assert body["admission"] == {
        "state": "disabled",
        "reason": "execution_not_ready",
        "format_version": 1,
    }
    assert isinstance(body["updated_at"], str)

    updated = await client.put(
        "/api/processing-stage-policy/v1",
        headers=_origin(settings),
        json={
            "expected_preference_version": body["preference_version"],
            "format_version": 1,
            "asr": True,
            "diarization": False,
            "notes": False,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["preference_version"] == 2
    assert updated.json()["policy"] == {"asr": True, "diarization": False, "notes": False}
    assert updated.json()["admission"]["state"] == "disabled"

    stale = await client.put(
        "/api/processing-stage-policy/v1",
        headers=_origin(settings),
        json={
            "expected_preference_version": body["preference_version"],
            "format_version": 1,
            "asr": False,
            "diarization": False,
            "notes": False,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stage_policy_changed"

    malformed = await client.put(
        "/api/processing-stage-policy/v1",
        headers=_origin(settings),
        json={
            "expected_preference_version": 2,
            "format_version": 1,
            "asr": True,
            "diarization": False,
            "notes": False,
            "settings": {"asr": False},
        },
    )
    assert malformed.status_code == 400
    assert malformed.json()["detail"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_extension_capabilities_separate_format_from_admission(client):
    response = await client.get("/api/extension/v1/capabilities")
    assert response.status_code == 200, response.text
    stage = response.json()["features"]["stage_policy"]
    assert stage == {
        "contract_version": 1,
        "format_version": 1,
        "supported": True,
        "admission": {
            "state": "disabled",
            "reason": "execution_not_ready",
            "format_version": 1,
        },
    }
    assert "asr" not in stage
    assert "diarization" not in stage
    assert "notes" not in stage


def test_stage_policy_schema_is_additive_and_persisted(settings):
    initialize(settings.data_dir)
    connection = __import__("sqlite3").connect(database_path(settings.data_dir))
    try:
        preference_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(processing_preferences)")
        }
        assert {
            "stage_policy_format",
            "stage_asr_enabled",
            "stage_diarization_enabled",
            "stage_notes_enabled",
        } <= preference_columns
        gate = connection.execute(
            "SELECT format_version, admission, reason FROM processing_cutover_gate WHERE id = 1"
        ).fetchone()
        assert gate == (1, "disabled", "execution_not_ready")
    finally:
        connection.close()
