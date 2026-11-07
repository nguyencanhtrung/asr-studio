from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fake_chatgpt import FakeChatGPTAdapter
from httpx import ASGITransport, AsyncByteStream, AsyncClient

from asr_studio.app import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
async def provider_http(settings):
    adapter = FakeChatGPTAdapter()
    app = create_app(settings, chatgpt_adapter=adapter)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=settings.public_url) as client:
            yield client, settings, app, adapter


async def authenticate(client, settings, app):
    state = app.state.auth_store.load()
    return await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )


async def test_provider_routes_obey_auth_contract_and_never_expose_credentials(provider_http):
    client, settings, app, adapter = provider_http
    assert (await client.get("/api/providers")).status_code == 401
    for asset in ("/assets/connections.js", "/assets/connections.css"):
        response = await client.get(asset)
        assert response.status_code == 303
        assert response.headers["location"] == "/access"
    assert (
        await client.get(
            "/auth/chatgpt/callback?state=unused",
            headers={"Host": "127.0.0.1:9999"},
        )
    ).status_code == 400
    assert (await authenticate(client, settings, app)).status_code == 204

    assert (await client.get("/assets/connections.js")).status_code == 200
    assert (await client.get("/assets/connections.css")).status_code == 200
    providers = await client.get("/api/providers")
    assert providers.status_code == 200
    assert providers.json() == {
        "providers": [
            {
                "id": "chatgpt",
                "status": "disconnected",
                "account": None,
                "connection_version": 0,
                "verification": None,
                "in_use": False,
                "in_use_reason": None,
            },
            {
                "id": "antigravity",
                "status": "disconnected",
                "account": None,
                "connection_version": 0,
                "verification": None,
                "reasoning_effort": "high",
                "in_use": False,
                "in_use_reason": None,
            },
        ]
    }
    assert (await client.get("/api/notes-preferences")).json() == {
        "provider": None,
        "models": {},
        "reasoning_effort": "default",
        "reasoning_efforts": {"chatgpt": "default", "antigravity": "high"},
    }

    started = await client.post(
        "/api/providers/chatgpt/login-sessions",
        headers={"Origin": settings.public_url},
        json={},
    )
    assert started.status_code == 201
    started_body = started.json()
    assert set(started_body) == {"id", "status", "authorization_url", "expires_at"}
    assert started_body["status"] == "pending"
    auth_query = parse_qs(urlsplit(started_body["authorization_url"]).query)
    assert auth_query["client_id"] == ["dynamic_agent_client"]
    assert "id_token_hint" not in auth_query

    pending = await client.get(f"/api/providers/chatgpt/login-sessions/{started_body['id']}")
    assert pending.status_code == 200
    assert set(pending.json()) == {"id", "status", "expires_at", "error"}
    assert pending.json()["status"] == "pending"
    assert "verifier" not in pending.text and "nonce" not in pending.text
    assert "access-secret-for-tests" not in started.text
    assert "refresh-secret-for-tests" not in started.text

    # The callback can finish in a browser without the app cookie.
    no_cookie = AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url)
    try:
        callback = await no_cookie.get(
            "/auth/chatgpt/callback",
            params={
                "state": auth_query["state"][0],
                "code": "one-time-code",
                "client_id": "oaiapp_test-123456",
            },
        )
    finally:
        await no_cookie.aclose()
    assert callback.status_code == 200
    assert "Đăng nhập ChatGPT đã hoàn tất" in callback.text
    assert "one-time-code" not in callback.text

    connected = await client.get("/api/providers")
    account = connected.json()["providers"][0]
    assert account["status"] == "connected"
    assert account["account"] == {"label": "verified@example.test"}
    assert "access-secret-for-tests" not in connected.text
    assert "refresh-secret-for-tests" not in connected.text
    assert (await client.get(f"/api/providers/chatgpt/login-sessions/{started_body['id']}")).json()[
        "status"
    ] == "connected"

    catalog = await client.get("/api/providers/chatgpt/models")
    assert catalog.status_code == 200
    assert catalog.json() == {
        "models": [
            {
                "id": "gpt-6.1-sol",
                "display_name": "GPT 6.1 Sol",
                "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            },
            {
                "id": "gpt-6-luna",
                "display_name": "GPT 6 Luna",
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
            },
            {
                "id": "gpt-5.6-luna",
                "display_name": "GPT-5.6 Luna",
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
            },
        ],
        "selected_model": None,
    }
    unknown_provider = await client.put(
        "/api/notes-preferences",
        headers={"Origin": settings.public_url},
        json={"provider": "other", "model": "gpt-6.1-sol"},
    )
    assert unknown_provider.status_code == 400
    assert unknown_provider.json()["detail"]["code"] == "invalid_provider"
    selected = await client.put(
        "/api/notes-preferences",
        headers={"Origin": settings.public_url},
        json={"provider": "chatgpt", "model": "gpt-6.1-sol"},
    )
    assert selected.status_code == 200
    assert selected.json() == {
        "provider": "chatgpt",
        "models": {"chatgpt": "gpt-6.1-sol"},
        "reasoning_effort": "default",
        "reasoning_efforts": {"chatgpt": "default", "antigravity": "high"},
    }

    verification = await client.post(
        "/api/providers/chatgpt/verify",
        headers={"Origin": settings.public_url},
        json={"model": "gpt-6.1-sol"},
    )
    assert verification.status_code == 200
    assert set(verification.json()) == {"model", "reasoning_effort", "verified_at", "output"}
    assert verification.json()["output"] == {"ok": True}
    assert adapter.verify_calls == [
        {
            "access_token": "access-secret-for-tests",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "default",
        }
    ]
    verified_provider = (await client.get("/api/providers")).json()["providers"][0]
    assert verified_provider["verification"]["model"] == "gpt-6.1-sol"

    unsupported = await client.put(
        "/api/notes-preferences",
        headers={"Origin": settings.public_url},
        json={"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "none"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["detail"]["code"] == "invalid_reasoning_effort"
    effort_choice = await client.put(
        "/api/notes-preferences",
        headers={"Origin": settings.public_url},
        json={"provider": "chatgpt", "model": "gpt-5.6-luna", "reasoning_effort": "high"},
    )
    assert effort_choice.json() == {
        "provider": "chatgpt",
        "models": {"chatgpt": "gpt-5.6-luna"},
        "reasoning_effort": "high",
        "reasoning_efforts": {"chatgpt": "high", "antigravity": "high"},
    }
    verified_effort = await client.post(
        "/api/providers/chatgpt/verify",
        headers={"Origin": settings.public_url},
        json={"model": "gpt-5.6-luna"},
    )
    assert verified_effort.status_code == 200
    assert verified_effort.json()["reasoning_effort"] == "high"
    assert adapter.verify_calls[-1]["reasoning_effort"] == "high"
    invalid_effort = await client.post(
        "/api/providers/chatgpt/verify",
        headers={"Origin": settings.public_url},
        json={"model": "gpt-5.6-luna", "reasoning_effort": "ultra"},
    )
    assert invalid_effort.status_code == 400
    assert len(adapter.verify_calls) == 2

    disconnected = await client.delete(
        "/api/providers/chatgpt/connection",
        headers={"Origin": settings.public_url},
    )
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "status": "disconnected",
        "remote_revocation_confirmed": True,
    }
    assert (await client.get("/api/providers")).json()["providers"][0]["verification"] is None
    assert (await client.get("/api/notes-preferences")).json() == {
        "provider": "chatgpt",
        "models": {"chatgpt": "gpt-5.6-luna"},
        "reasoning_effort": "high",
        "reasoning_efforts": {"chatgpt": "high", "antigravity": "high"},
    }


async def test_login_start_replaces_pending_session_across_http_clients(provider_http):
    client, settings, app, _ = provider_http
    authenticated = await authenticate(client, settings, app)
    assert authenticated.status_code == 204
    other_client = AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url)
    try:
        other_auth = await authenticate(other_client, settings, app)
        assert other_auth.status_code == 204
        first = await client.post(
            "/api/providers/chatgpt/login-sessions",
            headers={"Origin": settings.public_url},
            json={},
        )
        assert first.status_code == 201
        assert set(first.json()) == {"id", "status", "authorization_url", "expires_at"}

        second = await other_client.post(
            "/api/providers/chatgpt/login-sessions",
            headers={"Origin": settings.public_url},
            json={},
        )

        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        assert second.json()["replaced_previous"] is True
        old_status = await client.get(f"/api/providers/chatgpt/login-sessions/{first.json()['id']}")
        assert old_status.json()["status"] == "cancelled"
    finally:
        await other_client.aclose()


async def test_provider_401_errors_are_not_confused_with_local_auth(provider_http):
    client, settings, app, adapter = provider_http
    await authenticate(client, settings, app)
    started = await client.post(
        "/api/providers/chatgpt/login-sessions",
        headers={"Origin": settings.public_url},
        json={},
    )
    auth_query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    callback = await client.get(
        "/auth/chatgpt/callback",
        params={
            "state": auth_query["state"][0],
            "code": "one-time-code",
            "client_id": "oaiapp_test-123456",
        },
    )
    assert callback.status_code == 200
    adapter.models_error = __import__("asr_studio.chatgpt", fromlist=["ChatGPTError"]).ChatGPTError(
        401, "subscription_sharing_invalid_user", "provider message"
    )

    response = await client.get("/api/providers/chatgpt/models")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "subscription_sharing_invalid_user"
    assert response.json()["detail"]["message"] != "provider message"


async def test_unconfigured_https_callback_rejects_untrusted_host(tmp_path):
    from asr_studio.config import Settings

    settings = Settings(
        data_dir=tmp_path / "data", auth_dir=tmp_path / "auth", public_url="https://localhost:8765"
    )
    app = create_app(settings, chatgpt_adapter=FakeChatGPTAdapter())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        response = await client.get(
            "/auth/chatgpt/callback?state=unused", headers={"Host": "evil.example"}
        )
    assert response.status_code == 400
    assert response.text == "Invalid host"


class OversizedJsonStream(AsyncByteStream):
    async def __aiter__(self):
        yield b"{" + b'"callback_url":"' + (b"x" * (16 * 1024)) + b'"}'


async def test_public_https_origin_imports_fake_callback_and_keeps_callback_loopback(tmp_path):
    from asr_studio.config import Settings

    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="https://gui.example.test",
        chatgpt_callback_url="http://127.0.0.1:8765/auth/chatgpt/callback",
    )
    adapter = FakeChatGPTAdapter()
    app = create_app(settings, chatgpt_adapter=adapter)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        session = await client.post(
            "/api/access/session",
            headers={"Origin": settings.public_url},
            json={"token": app.state.auth_store.load().token},
        )
        assert session.status_code == 204
        assert "Secure" in session.headers["set-cookie"]
        started = await client.post(
            "/api/providers/chatgpt/login-sessions",
            headers={"Origin": settings.public_url},
            json={},
        )
        assert started.status_code == 201
        auth_query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        pasted = (
            "http://127.0.0.1:8765/auth/chatgpt/callback?"
            f"state={auth_query['state'][0]}&code=one-time-code&client_id=oaiapp_test-123456"
        )
        imported = await client.post(
            f"/api/providers/chatgpt/login-sessions/{started.json()['id']}/input",
            headers={"Origin": settings.public_url},
            json={"callback_url": pasted},
        )
        assert imported.status_code == 200
        assert imported.json()["status"] == "connected"
        assert pasted not in imported.text
        assert adapter.exchange_calls[0]["code"] == "one-time-code"

        wrong_host = await client.post(
            "/api/providers/chatgpt/login-sessions/not-a-session/input",
            headers={"Host": "evil.example", "Origin": settings.public_url},
            json={"callback_url": pasted},
        )
        assert wrong_host.status_code == 400
        wrong_origin = await client.post(
            "/api/providers/chatgpt/login-sessions/not-a-session/input",
            headers={"Origin": "https://evil.example"},
            json={"callback_url": pasted},
        )
        assert wrong_origin.status_code == 403

        oversized = await client.post(
            "/api/providers/chatgpt/login-sessions/not-a-session/input",
            headers={"Origin": settings.public_url, "Content-Type": "application/json"},
            content=OversizedJsonStream(),
        )
        assert oversized.status_code == 413
