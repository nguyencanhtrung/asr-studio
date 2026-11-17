from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from asr_studio.app import create_app
from asr_studio.auth import AuthStore, create_session_cookie, session_is_valid
from asr_studio.config import Settings

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://[::1]:8765",
        "https://192.168.10.20:8443",
        "https://100.101.102.103:9443",
        "https://studio.example-tailnet.ts.net:9443",
        "https://another-installation.example.test",
    ],
)
async def test_configured_network_origins_allow_login_and_protected_api(tmp_path, origin):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="https://custom-domain.example.test",
        web_origins=(origin,),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=origin) as http,
    ):
        assert (await http.get("/access")).status_code == 200
        assert (await http.get("/api/uploads/policy")).status_code == 401
        response = await http.post(
            "/api/access/session",
            headers={"Origin": origin},
            json={"token": AuthStore(settings.auth_dir).load().token},
        )
        assert response.status_code == 204
        assert (await http.get("/api/uploads/policy")).status_code == 200
        assert (await http.get("/")).status_code == 200


async def test_callback_keeps_exact_host_even_when_other_gui_origins_are_allowed(
    tmp_path, monkeypatch
):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="https://custom-domain.example.test",
        web_origins=("https://alias.example.test",),
        chatgpt_callback_url="http://127.0.0.1:8765/auth/chatgpt/callback",
    )
    app = create_app(settings)
    calls = []
    monkeypatch.setattr(
        app.state.chatgpt_provider, "handle_callback", lambda params: calls.append(params)
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as http,
    ):
        for host in ("custom-domain.example.test", "alias.example.test", "unknown.example.test"):
            response = await http.get("/auth/chatgpt/callback", headers={"Host": host})
            assert response.status_code == 400
            assert calls == []
        response = await http.get("/auth/chatgpt/callback", headers={"Host": "127.0.0.1:8765"})
        assert response.status_code == 200
        assert len(calls) == 1
        assert (await http.get("/access", headers={"Host": "127.0.0.1:8765"})).status_code == 400


@pytest.mark.parametrize("force_secure", [False, True])
async def test_http_alias_cookie_respects_explicit_secure_override(tmp_path, force_secure):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="https://custom-domain.example.test",
        web_origins=("http://localhost:8765",),
        cookie_secure=force_secure,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8765") as http,
    ):
        response = await http.post(
            "/api/access/session",
            headers={"Origin": "http://localhost:8765"},
            json={"token": AuthStore(settings.auth_dir).load().token},
        )
        assert response.status_code == 204
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        assert bool(next(iter(cookie.values()))["secure"]) is force_secure


async def test_application_restricts_legacy_sessions_and_rotates_all_origins(tmp_path):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="https://custom-domain.example.test",
        web_origins=("https://alias.example.test",),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as http,
    ):
        store = AuthStore(settings.auth_dir)
        state = store.load()
        alias = "https://alias.example.test"
        sessions = []
        for origin in (settings.public_url, alias):
            response = await http.post(
                f"{origin}/api/access/session",
                headers={"Origin": origin},
                json={"token": state.token},
            )
            assert response.status_code == 204
            cookie = SimpleCookie()
            cookie.load(response.headers["set-cookie"])
            name = next(iter(cookie))
            sessions.append((origin, name, cookie[name].value))
            assert (await http.get(f"{origin}/api/uploads/policy")).status_code == 200

        legacy = create_session_cookie(state)
        primary_name = sessions[0][1]
        alias_name = sessions[1][1]
        response = await http.get(
            "/api/uploads/policy", headers={"Cookie": f"{primary_name}={legacy}"}
        )
        assert response.status_code == 200
        response = await http.get(
            f"{alias}/api/uploads/policy", headers={"Cookie": f"{alias_name}={legacy}"}
        )
        assert response.status_code == 401
        store.rotate()
        for origin, name, value in (*sessions, (settings.public_url, primary_name, legacy)):
            response = await http.get(
                f"{origin}/api/uploads/policy", headers={"Cookie": f"{name}={value}"}
            )
            assert response.status_code == 401


@pytest.mark.parametrize(
    "source,target",
    [
        ("https://localhost:8765", "http://localhost:8765"),
        ("https://localhost:8765", "https://localhost:9443"),
        ("https://primary.example.test", "https://alias.example.test"),
    ],
)
def test_signed_session_binding_covers_scheme_host_and_port(tmp_path, source, target):
    state = AuthStore(tmp_path / "auth").ensure()
    cookie = create_session_cookie(state, source)
    assert session_is_valid(cookie, state, source)
    assert not session_is_valid(cookie, state, target)
