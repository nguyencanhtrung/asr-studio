from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.extension_auth import (
    CODE_LIFETIME,
    EXTENSION_ID,
    MAX_ACTIVE_SESSIONS_PER_DEVICE,
    MAX_PAIRING_ATTEMPTS,
    MAX_PENDING_DEVICE_RECORDS,
    ExtensionAuthError,
    ExtensionAuthService,
    authorize_extension_session,
)
from asr_studio.storage import database_path, initialize

EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
pytestmark = pytest.mark.anyio


async def _authenticate_operator(client, settings):
    state = AuthStore(settings.auth_dir).load()
    response = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )
    assert response.status_code == 204


async def test_capabilities_is_public_and_operator_routes_keep_cookie_guard(client, settings):
    capabilities = await client.get(
        "/api/extension/v1/capabilities",
        headers={"Origin": EXTENSION_ORIGIN},
    )

    assert capabilities.status_code == 200
    assert capabilities.json()["protocol"] == "asr-extension-v1"
    assert capabilities.json()["api_version"] == 1
    assert capabilities.json()["prototype"] is False
    assert capabilities.json()["extension_id"] == EXTENSION_ID
    assert "device_token" not in capabilities.text
    assert "hmac" not in capabilities.text.lower()

    unauthenticated = await client.get("/api/extension/devices")
    assert unauthenticated.status_code == 401

    await _authenticate_operator(client, settings)
    devices = await client.get("/api/extension/devices")
    assert devices.status_code == 200
    assert devices.json() == {
        "backend_id": capabilities.json()["backend_id"],
        "extension_id": EXTENSION_ID,
        "server_urls": [settings.public_url],
        "devices": [],
    }


async def _create_code(client, settings, *, lifetime="30d", record_id=None):
    payload = {"device_lifetime": lifetime}
    if record_id is not None:
        payload["device_record_id"] = record_id
    response = await client.post(
        "/api/extension/pairing-codes",
        headers={"Origin": settings.public_url},
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _pair(client, code, device_id):
    return await client.post(
        "/api/extension/v1/pair",
        headers={"Origin": EXTENSION_ORIGIN},
        json={"code": code, "device_id": device_id},
    )


async def test_pairing_code_is_atomic_one_use_and_session_requires_device_bearer(client, settings):
    await _authenticate_operator(client, settings)
    issued = await _create_code(client, settings, lifetime="7d")

    paired = await _pair(client, issued["code"], "client-device-a")

    assert paired.status_code == 200
    credential = paired.json()
    assert credential["device_token"]
    assert credential["device_lifetime"] == "7d"
    assert credential["device_expires_at"] is not None
    assert issued["code"] not in paired.text

    replay = await _pair(client, issued["code"], "client-device-b")
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "pairing_code_replayed"
    assert issued["code"] not in replay.text

    without_bearer = await client.post(
        "/api/extension/v1/session",
        headers={"Origin": EXTENSION_ORIGIN},
        json={},
    )
    assert without_bearer.status_code == 401

    session = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {credential['device_token']}",
        },
        json={},
    )
    assert session.status_code == 200
    assert session.json()["session_token"]
    assert credential["device_token"] not in session.text


async def test_repair_requires_operator_record_id_and_rotates_generation(client, settings):
    await _authenticate_operator(client, settings)
    first_code = await _create_code(client, settings, lifetime="never")
    first_pair = await _pair(client, first_code["code"], "diagnostic-a")
    first_token = first_pair.json()["device_token"]

    devices = (await client.get("/api/extension/devices")).json()["devices"]
    record_id = devices[0]["id"]
    assert devices[0]["status"] == "pending"
    assert devices[0]["device_expires_at"] is None

    activated = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {first_token}",
        },
        json={},
    )
    assert activated.status_code == 200
    assert (await client.get("/api/extension/devices")).json()["devices"][0]["status"] == "active"

    second_code = await _create_code(
        client,
        settings,
        lifetime="30d",
        record_id=record_id,
    )
    second_pair = await _pair(client, second_code["code"], "diagnostic-b")
    assert second_pair.status_code == 200
    assert second_pair.json()["device_token"] != first_token

    old_device_session = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {first_token}",
        },
        json={},
    )
    assert old_device_session.status_code == 401

    devices_after = (await client.get("/api/extension/devices")).json()["devices"]
    assert len(devices_after) == 1
    assert devices_after[0]["id"] == record_id
    assert devices_after[0]["device_id"] == "diagnostic-b"
    assert devices_after[0]["device_lifetime"] == "30d"


async def test_repair_does_not_adopt_owner_from_client_device_id(client, settings):
    await _authenticate_operator(client, settings)
    first_code = await _create_code(client, settings)
    first_pair = await _pair(client, first_code["code"], "shared-diagnostic-id")
    first_session = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {first_pair.json()['device_token']}",
        },
        json={},
    )
    assert first_session.status_code == 200

    first_identity = authorize_extension_session(
        settings.data_dir,
        first_session.json()["session_token"],
        settings.public_url,
    )

    second_code = await _create_code(client, settings)
    second_pair = await _pair(client, second_code["code"], "shared-diagnostic-id")
    second_session = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {second_pair.json()['device_token']}",
        },
        json={},
    )
    assert second_session.status_code == 200
    second_identity = authorize_extension_session(
        settings.data_dir,
        second_session.json()["session_token"],
        settings.public_url,
    )

    assert second_identity.device_record_id != first_identity.device_record_id
    assert second_identity.owner_id != first_identity.owner_id
    assert second_identity.device_id == first_identity.device_id == "shared-diagnostic-id"


def test_extension_state_is_private_and_schema_migration_is_restart_safe(settings):
    database = initialize(settings.data_dir)
    service = ExtensionAuthService(
        settings.data_dir,
        settings.extension_server_urls,
        auth_dir=settings.auth_dir,
    )
    backend_id = service.ensure()
    private_state = settings.auth_dir / "extension-auth.json"

    assert private_state.is_file()
    assert private_state.stat().st_mode & 0o777 == 0o600
    assert backend_id == service.backend_id()
    private_text = private_state.read_text(encoding="utf-8")
    assert "hmac_secret" in private_text

    with sqlite3.connect(database) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name LIKE 'extension_%'"
        ).fetchall()
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (17,)
        assert all("hmac_secret" not in (row[0] or "") for row in schema)

    initialize(settings.data_dir)
    restarted = ExtensionAuthService(
        settings.data_dir,
        settings.extension_server_urls,
        auth_dir=settings.auth_dir,
    )
    assert restarted.ensure() == backend_id


def test_extension_url_validation_rejects_ambiguous_aliases(tmp_path):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
        extension_urls=("https://alias.example.test",),
    )
    assert settings.extension_server_urls == (
        "http://127.0.0.1:8765",
        "https://alias.example.test",
    )

    for alias in (
        "http://remote.example.test",
        "https://alias.example.test/path",
        "https://alias.example.test/.",
        "https://user:pass@alias.example.test",
        "https://alias.example.test\\evil",
    ):
        with pytest.raises(ValueError):
            Settings(
                tmp_path / "bad-data",
                tmp_path / "bad-auth",
                public_url="http://127.0.0.1:8765",
                extension_urls=(alias,),
            )
    with pytest.raises(ValueError):
        Settings(
            tmp_path / "duplicate-data",
            tmp_path / "duplicate-auth",
            public_url="http://127.0.0.1:8765",
            extension_urls=("https://alias.example.test", "https://alias.example.test:443"),
        )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
@pytest.mark.parametrize("explicit_default_ports", [False, True])
def test_extension_default_port_aliases_cannot_share_host_header(
    tmp_path, host, explicit_default_ports
):
    http_port = ":80" if explicit_default_ports else ""
    https_port = ":443" if explicit_default_ports else ""
    with pytest.raises(ValueError, match="ambiguous endpoint authority"):
        Settings(
            tmp_path / "data",
            tmp_path / "auth",
            public_url=f"http://{host}{http_port}",
            extension_urls=(f"https://{host}{https_port}",),
        )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_extension_distinct_host_ports_select_exact_endpoint(tmp_path, host):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url=f"http://{host}",
        extension_urls=(f"https://{host}:8443",),
    )
    assert settings.extension_endpoint_for_host(host) == f"http://{host}"
    assert settings.extension_endpoint_for_host(f"{host}:80") == f"http://{host}"
    assert settings.extension_endpoint_for_host(f"{host}:8443") == f"https://{host}:8443"
    assert settings.extension_endpoint_for_host(f"{host}:443") is None


async def test_alias_pairing_binds_consuming_endpoint_and_explicit_repair_rebinds(tmp_path):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="https://primary.example.test",
        extension_urls=("https://alias.example.test",),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=settings.public_url) as client:
            await _authenticate_operator(client, settings)
            issued = await _create_code(client, settings)

            alias_pair = await client.post(
                "/api/extension/v1/pair",
                headers={"Host": "alias.example.test", "Origin": EXTENSION_ORIGIN},
                json={"code": issued["code"], "device_id": "alias-device"},
            )
            assert alias_pair.status_code == 200
            alias_token = alias_pair.json()["device_token"]
            devices = (await client.get("/api/extension/devices")).json()["devices"]
            record_id = devices[0]["id"]
            assert devices[0]["server_url"] == "https://alias.example.test"
            assert devices[0]["status"] == "pending"

            wrong_endpoint_session = await client.post(
                "/api/extension/v1/session",
                headers={
                    "Origin": EXTENSION_ORIGIN,
                    "Authorization": f"Bearer {alias_token}",
                },
                json={},
            )
            assert wrong_endpoint_session.status_code == 401

            alias_session = await client.post(
                "/api/extension/v1/session",
                headers={
                    "Host": "alias.example.test",
                    "Origin": EXTENSION_ORIGIN,
                    "Authorization": f"Bearer {alias_token}",
                },
                json={},
            )
            assert alias_session.status_code == 200
            before_repair = authorize_extension_session(
                settings.data_dir,
                alias_session.json()["session_token"],
                "https://alias.example.test",
                auth_dir=settings.auth_dir,
            )

            repair_code = await _create_code(client, settings, record_id=record_id)
            repaired = await client.post(
                "/api/extension/v1/pair",
                headers={"Origin": EXTENSION_ORIGIN},
                json={"code": repair_code["code"], "device_id": "primary-device"},
            )
            assert repaired.status_code == 200
            assert repaired.json()["device_token"] != alias_token

            revoked_session = await client.post(
                "/api/extension/v1/session",
                headers={
                    "Host": "alias.example.test",
                    "Origin": EXTENSION_ORIGIN,
                    "Authorization": f"Bearer {alias_token}",
                },
                json={},
            )
            assert revoked_session.status_code == 401
            repaired_identity = before_repair
            assert repaired_identity.server_url == "https://alias.example.test"

            primary_session = await client.post(
                "/api/extension/v1/session",
                headers={
                    "Origin": EXTENSION_ORIGIN,
                    "Authorization": f"Bearer {repaired.json()['device_token']}",
                },
                json={},
            )
            assert primary_session.status_code == 200
            final_device = (await client.get("/api/extension/devices")).json()["devices"][0]
            assert final_device["id"] == record_id
            assert final_device["server_url"] == "https://primary.example.test"
            assert final_device["device_id"] == "primary-device"


async def test_unsuccessful_pairing_attempts_are_committed_and_bounded(client, settings):
    await _authenticate_operator(client, settings)
    invalid = {
        "Origin": EXTENSION_ORIGIN,
    }
    for _ in range(MAX_PAIRING_ATTEMPTS):
        response = await client.post(
            "/api/extension/v1/pair",
            headers=invalid,
            json={"code": "not-the-code", "device_id": "guess-device"},
        )
        assert response.status_code == 401

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute(
            "SELECT attempt_count FROM extension_pairing_rate WHERE id = 1"
        ).fetchone() == (MAX_PAIRING_ATTEMPTS,)

    limited = await client.post(
        "/api/extension/v1/pair",
        headers=invalid,
        json={"code": "another-guess", "device_id": "guess-device"},
    )
    assert limited.status_code == 429
    assert "another-guess" not in limited.text


def test_pending_credential_expires_before_first_session(settings):
    initialize(settings.data_dir)
    service = ExtensionAuthService(
        settings.data_dir,
        settings.extension_server_urls,
        auth_dir=settings.auth_dir,
    )
    service.ensure()
    started = datetime(2035, 1, 1, tzinfo=UTC)
    issued = service.create_pairing_code(device_lifetime="90d", device_record_id=None, now=started)
    credential = service.pair(
        code=issued.code,
        device_id="pending-device",
        server_url=settings.extension_server_urls[0],
        now=started + timedelta(seconds=1),
    )
    assert service.list_devices(now=started + timedelta(seconds=2))[0]["status"] == "pending"

    with pytest.raises(ExtensionAuthError) as error:
        service.create_session(
            device_token=credential.token,
            server_url=settings.extension_server_urls[0],
            now=started + CODE_LIFETIME,
        )
    assert error.value.code == "invalid_device_credential"
    assert service.list_devices(now=started + CODE_LIFETIME)[0]["status"] == "expired"


async def test_extension_origin_guards_and_preflight_never_grant_authority(client, settings):
    no_origin = await client.post(
        "/api/extension/v1/session",
        json={},
    )
    wrong_origin = await client.post(
        "/api/extension/v1/session",
        headers={"Origin": "chrome-extension://wrong"},
        json={},
    )
    assert no_origin.status_code == 403
    assert wrong_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "origin_required"

    preflight = await client.options(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == EXTENSION_ORIGIN
    assert "session_token" not in preflight.text

    too_large = await client.post(
        "/api/extension/v1/pair",
        headers={"Origin": EXTENSION_ORIGIN},
        json={"code": "x" * 5000, "device_id": "bounded"},
    )
    assert too_large.status_code == 413


async def test_revoke_invalidates_pending_code_and_fresh_explicit_repair_reactivates(
    client, settings
):
    await _authenticate_operator(client, settings)
    issued = await _create_code(client, settings)
    record_id = (await client.get("/api/extension/devices")).json()["devices"][0]["id"]

    revoked = await client.post(
        f"/api/extension/devices/{record_id}/revoke",
        headers={"Origin": settings.public_url},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"id": record_id, "revoked": True}

    stale_pair = await _pair(client, issued["code"], "stale-pending-device")
    assert stale_pair.status_code == 401
    assert (await client.get("/api/extension/devices")).json()["devices"][0]["status"] == "revoked"

    fresh_code = await _create_code(client, settings, record_id=record_id)
    fresh_pair = await _pair(client, fresh_code["code"], "reactivated-device")
    assert fresh_pair.status_code == 200
    assert (await client.get("/api/extension/devices")).json()["devices"][0]["status"] == "pending"

    activated = await client.post(
        "/api/extension/v1/session",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Authorization": f"Bearer {fresh_pair.json()['device_token']}",
        },
        json={},
    )
    assert activated.status_code == 200
    assert (await client.get("/api/extension/devices")).json()["devices"][0]["status"] == "active"


async def test_concurrent_pair_consumes_one_code_once(client, settings):
    await _authenticate_operator(client, settings)
    issued = await _create_code(client, settings)
    responses = await asyncio.gather(
        _pair(client, issued["code"], "race-a"),
        _pair(client, issued["code"], "race-b"),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    devices = (await client.get("/api/extension/devices")).json()["devices"]
    assert len(devices) == 1
    assert devices[0]["device_id"] in {"race-a", "race-b"}


async def test_session_rows_are_pruned_and_capped_per_device(client, settings):
    await _authenticate_operator(client, settings)
    issued = await _create_code(client, settings)
    paired = await _pair(client, issued["code"], "session-cap-device")
    token = paired.json()["device_token"]

    sessions = []
    for _ in range(MAX_ACTIVE_SESSIONS_PER_DEVICE + 2):
        response = await client.post(
            "/api/extension/v1/session",
            headers={
                "Origin": EXTENSION_ORIGIN,
                "Authorization": f"Bearer {token}",
            },
            json={},
        )
        assert response.status_code == 200
        sessions.append(response.json()["session_token"])

    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM extension_sessions").fetchone() == (
            MAX_ACTIVE_SESSIONS_PER_DEVICE,
        )
    assert all(sessions)


def test_pending_device_limit_is_explicit(settings):
    initialize(settings.data_dir)
    service = ExtensionAuthService(
        settings.data_dir,
        settings.extension_server_urls,
        auth_dir=settings.auth_dir,
    )
    service.ensure()
    for _ in range(MAX_PENDING_DEVICE_RECORDS):
        service.create_pairing_code(device_lifetime="7d", device_record_id=None)

    with pytest.raises(ExtensionAuthError) as error:
        service.create_pairing_code(device_lifetime="7d", device_record_id=None)
    assert error.value.code == "pending_limit"
