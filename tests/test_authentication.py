from __future__ import annotations

import json
from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.routes import FONT_FILES, WEB_DIR

pytestmark = pytest.mark.anyio


async def test_access_page_is_minimal_and_security_headers_are_set(client):
    response = await client.get("/access")

    assert response.status_code == 200
    assert "ASR Studio" in response.text
    assert "demo-local" not in response.text
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "media-src 'self'" in response.headers["Content-Security-Policy"]


async def test_shared_design_assets_and_fonts_are_public_and_local(client):
    design_css = await client.get("/assets/design-system.css")
    theme_js = await client.get("/assets/theme.js")

    assert design_css.status_code == 200
    assert design_css.headers["content-type"].startswith("text/css")
    assert "Newsreader Variable" in design_css.text
    assert "Inter Variable" in design_css.text
    assert "JetBrains Mono" in design_css.text
    assert "https://" not in design_css.text
    assert "http://" not in design_css.text
    assert theme_js.status_code == 200
    assert "asr-studio:theme" in theme_js.text

    for filename in FONT_FILES:
        response = await client.get(f"/assets/fonts/{filename}")
        expected_type = "font/woff2" if filename.endswith(".woff2") else "font/woff"
        assert response.status_code == 200, filename
        assert response.headers["content-type"].startswith(expected_type), filename
        assert response.content, filename

    unknown = await client.get("/assets/fonts/not-bundled.woff2")
    assert unknown.status_code == 404
    license_files = sorted((WEB_DIR / "fonts").glob("LICENSE-*.txt"))
    assert len(license_files) == 3
    assert all("SIL OPEN FONT LICENSE" in path.read_text() for path in license_files)


async def test_fragment_token_exchange_sets_generation_bound_cookie(client, settings):
    state = AuthStore(settings.auth_dir).load()
    response = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    assert "Max-Age" not in cookie
    assert "Expires" not in cookie
    assert "Domain" not in cookie
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/assets/shell.css")).status_code == 200


def test_public_gui_origin_requires_exact_https_origin_and_separate_callback(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="https://gui.example.test:8443",
        chatgpt_callback_url="http://127.0.0.1:8765/auth/chatgpt/callback",
    )
    assert settings.public_url == "https://gui.example.test:8443"
    assert settings.origin == ("https", "gui.example.test", 8443)
    assert settings.chatgpt_callback_url == "http://127.0.0.1:8765/auth/chatgpt/callback"

    for public_url in (
        "http://gui.example.test",
        "https://gui.example.test/path",
        "https://gui.example.test?query=1",
        "https://user:pass@gui.example.test",
        "https://*.example.test",
    ):
        with pytest.raises(ValueError):
            Settings(tmp_path / "bad-data", tmp_path / "bad-auth", public_url=public_url)

    loopback = Settings(tmp_path / "loop-data", tmp_path / "loop-auth")
    assert loopback.public_url == "http://127.0.0.1:8765"
    assert loopback.chatgpt_callback_url == "http://127.0.0.1:8765/auth/chatgpt/callback"


def test_configured_gui_origins_are_normalized_bounded_and_host_selectable(tmp_path):
    settings = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        public_url="HTTPS://Primary.example.test:443/",
        web_origins=("https://Alias.example.test:443/", "http://localhost:80"),
    )

    assert settings.public_url == "https://primary.example.test"
    assert settings.web_origins == ("https://alias.example.test", "http://localhost")
    assert settings.web_server_origins == (
        "https://primary.example.test",
        "https://alias.example.test",
        "http://localhost",
    )
    assert settings.web_origin_for_host("alias.example.test") == "https://alias.example.test"
    assert settings.web_origin_for_host("localhost:80") == "http://localhost"
    assert settings.web_origin_for_host("unconfigured.example.test") is None
    assert settings.select_web_origin("HTTPS://ALIAS.example.test:443") == (
        "https://alias.example.test"
    )

    for invalid in (
        "http://remote.example.test",
        "https://user:pass@alias.example.test",
        "https://*.example.test",
        "https://alias.example.test/path",
        "https://alias.example.test?query=1",
        "https://alias.example.test#fragment",
        "https://alias.example.test:65536",
        "https://alias.example.test\n",
    ):
        with pytest.raises(ValueError):
            Settings(tmp_path / "invalid-data", tmp_path / "invalid-auth", web_origins=(invalid,))

    with pytest.raises(ValueError, match="duplicate"):
        Settings(
            tmp_path / "duplicate-data",
            tmp_path / "duplicate-auth",
            public_url="https://primary.example.test",
            web_origins=("https://PRIMARY.example.test:443",),
        )
    for primary, alias in (
        ("http://localhost:8765", "https://localhost:8765"),
        ("http://localhost:443", "https://localhost"),
        ("http://localhost", "https://localhost:80"),
    ):
        with pytest.raises(ValueError, match="ambiguous"):
            Settings(
                tmp_path / "ambiguous-data",
                tmp_path / "ambiguous-auth",
                public_url=primary,
                web_origins=(alias,),
            )
    with pytest.raises(ValueError, match="at most"):
        Settings(
            tmp_path / "bounded-data",
            tmp_path / "bounded-auth",
            web_origins=tuple(f"https://alias-{index}.example.test" for index in range(9)),
        )


def _cookie_value(set_cookie: str, name: str) -> str:
    cookies = SimpleCookie()
    cookies.load(set_cookie)
    return cookies[name].value


async def test_gui_aliases_bind_origin_checks_and_isolate_sessions(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="https://primary.example.test",
        web_origins=("https://alias.example.test",),
        extension_urls=("https://extension.example.test",),
        chatgpt_callback_url="http://127.0.0.1:8765/auth/chatgpt/callback",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as http,
    ):
        state = AuthStore(settings.auth_dir).load()
        primary_login = await http.post(
            "/api/access/session",
            headers={"Origin": settings.public_url},
            json={"token": state.token},
        )
        assert primary_login.status_code == 204
        primary_set_cookie = primary_login.headers["set-cookie"]
        assert "asr_studio_session=" in primary_set_cookie
        assert "HttpOnly" in primary_set_cookie
        assert "SameSite=strict" in primary_set_cookie
        assert "Secure" in primary_set_cookie
        assert "Domain" not in primary_set_cookie

        assert (await http.get("/")).status_code == 200
        alias_without_login = await http.get("/", headers={"Host": "alias.example.test"})
        assert alias_without_login.status_code == 303

        alias_login = await http.post(
            "/api/access/session",
            headers={"Host": "alias.example.test", "Origin": "https://alias.example.test"},
            json={"token": state.token},
        )
        assert alias_login.status_code == 204
        alias_set_cookie = alias_login.headers["set-cookie"]
        alias_cookie = SimpleCookie()
        alias_cookie.load(alias_set_cookie)
        alias_names = [name for name in alias_cookie if name.startswith("asr_studio_session_")]
        assert len(alias_names) == 1
        assert "Secure" in alias_set_cookie
        assert "HttpOnly" in alias_set_cookie
        assert "SameSite=strict" in alias_set_cookie
        assert "Domain" not in alias_set_cookie
        alias_name = alias_names[0]

        assert (await http.get("/", headers={"Host": "alias.example.test"})).status_code == 200
        wrong_destination_origin = await http.post(
            "/api/meetings",
            headers={
                "Host": "alias.example.test",
                "Origin": settings.public_url,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert wrong_destination_origin.status_code == 403

        forwarded_alias = await http.post(
            "/api/access/session",
            headers={
                "Origin": "https://alias.example.test",
                "X-Forwarded-Host": "alias.example.test",
            },
            json={"token": state.token},
        )
        assert forwarded_alias.status_code == 403

        primary_value = _cookie_value(primary_set_cookie, "asr_studio_session")
        alias_value = alias_cookie[alias_name].value
        renamed_v1 = await http.get(
            "/",
            headers={
                "Host": "alias.example.test",
                "Cookie": f"{alias_name}={primary_value}",
            },
        )
        assert renamed_v1.status_code == 303
        renamed_v2 = await http.get(
            "/",
            headers={"Cookie": f"asr_studio_session={alias_value}"},
        )
        assert renamed_v2.status_code == 303

        assert (
            await http.get("/access", headers={"Host": "extension.example.test"})
        ).status_code == 400
        assert (
            await http.get("/access", headers={"Host": "unknown.example.test"})
        ).status_code == 400


async def test_invalid_token_and_invalid_request_never_echo_submitted_value(client):
    invalid_token = "definitely-not-a-token-" + "x" * 43
    response = await client.post(
        "/api/access/session",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={"token": invalid_token},
    )
    too_long = "secret-value-" * 100
    invalid_shape = await client.post(
        "/api/access/session",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={"token": too_long},
    )

    assert response.status_code == 401
    assert invalid_token not in response.text
    assert response.headers.get("set-cookie") is None
    assert invalid_shape.status_code == 400
    assert too_long not in invalid_shape.text


async def test_same_origin_and_host_checks_reject_cross_site_requests(client):
    cross_origin = await client.post(
        "/api/access/session",
        headers={"Origin": "http://evil.example"},
        json={"token": "x" * 43},
    )
    pathful_origin = await client.post(
        "/api/access/session",
        headers={"Origin": "http://127.0.0.1:8765/other"},
        json={"token": "x" * 43},
    )
    wrong_host = await client.get("/access", headers={"Host": "evil.example"})

    assert cross_origin.status_code == 403
    assert pathful_origin.status_code == 403
    assert wrong_host.status_code == 400


async def test_unsafe_requests_require_origin_fetch_metadata_and_auth(client, settings):
    no_origin = await client.post("/api/meetings")
    null_origin = await client.post("/api/meetings", headers={"Origin": "null"})
    cross_site = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url, "Sec-Fetch-Site": "cross-site"},
    )
    unauthenticated = await client.post(
        "/api/meetings",
        headers={"Origin": settings.public_url, "Sec-Fetch-Site": "same-origin"},
    )

    assert no_origin.status_code == 403
    assert null_origin.status_code == 403
    assert cross_site.status_code == 403
    assert unauthenticated.status_code == 401


async def test_protected_by_default_paths_deny_missing_and_invalid_auth(client):
    protected_paths = [
        "/api/meetings",
        "/api/meetings/meeting-1/audio",
        "/api/uploads/policy",
        "/api/uploads/upload-1",
        "/api/uploads/upload-1/content",
        "/meetings/meeting-1",
        "/api/notes/note-1/export",
        "/recordings/private.wav",
        "/assets/shell.css",
        "/assets/shell.js",
        "/assets/shell-core.js",
        "/assets/store.js",
        "/assets/channel.js",
        "/assets/notes-core.js",
        "/assets/notes.js",
        "/assets/provider-core.js",
        "/assets/processing-stage-policy-core.js",
        "/assets/connections.js",
        "/assets/antigravity.js",
        "/assets/history.css",
        "/assets/history.js",
        "/assets/history-core.js",
        "/assets/task-core.js",
        "/assets/transcript-core.js",
        "/assets/playback-core.js",
    ]

    for path in protected_paths:
        response = await client.get(path)
        assert response.status_code in {401, 303}, (path, response.status_code)
        assert "meeting" not in response.text.lower()

    client.cookies.set("asr_studio_session", "not-a-session")
    bad_cookie = await client.get("/api/meetings")
    assert bad_cookie.status_code == 401
    assert "meeting" not in bad_cookie.text.lower()


async def test_liveness_is_public_and_does_not_reveal_auth_state(client, settings):
    response = await client.get("/healthz")
    head_response = await client.head("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert AuthStore(settings.auth_dir).load().token not in response.text


async def test_missing_auth_state_fails_closed_without_issuing_a_replacement(client, settings):
    store = AuthStore(settings.auth_dir)
    original = store.load()
    exchange = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": original.token},
    )
    assert exchange.status_code == 204

    store.state_path.unlink()
    response = await client.get("/")
    exchange_after_loss = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": original.token},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication unavailable"}
    assert exchange_after_loss.status_code == 503
    assert exchange_after_loss.json() == {"detail": "Authentication unavailable"}
    assert not store.state_path.exists()


async def test_exchange_does_not_log_token_or_include_it_in_errors(client, settings, caplog):
    token = AuthStore(settings.auth_dir).load().token
    response = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url, "Content-Type": "application/json"},
        content=json.dumps({"token": "bad-" + token}),
    )

    assert response.status_code == 401
    assert token not in caplog.text
