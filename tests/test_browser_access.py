from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.storage import initialize


@pytest.mark.browser
def test_access_fragment_is_removed_before_http_exchange_and_cookie_is_httponly(tmp_path, caplog):
    if os.environ.get("ASR_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set ASR_RUN_BROWSER_TESTS=1 to run the real-browser access test")
    chrome_path = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    port = server_socket.getsockname()[1]
    public_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url=public_url,
    )
    caplog.set_level(logging.INFO)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            log_config=None,
            log_level="info",
            access_log=True,
        )
    )
    server_thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        daemon=True,
    )
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.025)
        assert server.started, "test server did not start"

        state = AuthStore(settings.auth_dir).load()
        link = f"{public_url}/access#{state.token}"
        exchange_requests: list[tuple[str, str]] = []
        with sync_playwright() as playwright:
            launch_options = {"executable_path": chrome_path} if chrome_path else {}
            browser = playwright.chromium.launch(headless=True, **launch_options)
            context = browser.new_context()
            page = context.new_page()
            page.on(
                "request",
                lambda request: (
                    exchange_requests.append((request.url, request.post_data or ""))
                    if request.url.endswith("/api/access/session")
                    else None
                ),
            )
            page.goto(link, wait_until="domcontentloaded")
            page.wait_for_url(f"{public_url}/", timeout=10_000)

            assert page.url == f"{public_url}/"
            assert awaitable_evaluate(page, "window.location.hash") == ""
            assert awaitable_evaluate(page, "document.cookie") == ""
            assert awaitable_evaluate(page, "Object.keys(localStorage).length") == 0
            assert awaitable_evaluate(page, "Object.keys(sessionStorage).length") == 0
            assert len(exchange_requests) == 1
            request_url, request_body = exchange_requests[0]
            assert state.token not in request_url
            assert state.token in request_body
            cookie = next(
                cookie for cookie in context.cookies() if cookie["name"] == "asr_studio_session"
            )
            assert cookie["httpOnly"] is True
            assert cookie["sameSite"] == "Strict"
            assert cookie["expires"] == -1
            assert "Không có cuộc họp nào được lưu" in page.locator("main").inner_text()
            assert "/api/access/session" in caplog.text
            assert state.token not in caplog.text
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        server_socket.close()


def awaitable_evaluate(page, expression: str):
    """Keep the browser assertions synchronous while evaluating page state."""
    return page.evaluate(expression)


@pytest.mark.browser
@pytest.mark.parametrize("primary_https", [False, True])
def test_rendered_http_and_https_aliases_have_isolated_login_sessions(tmp_path, primary_https):
    if os.environ.get("ASR_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set ASR_RUN_BROWSER_TESTS=1 to run the real-browser origin test")
    chrome_path = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")

    http_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    https_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_socket.bind(("127.0.0.1", 0))
    https_socket.bind(("127.0.0.1", 0))
    http_socket.listen()
    https_socket.listen()
    http_port = http_socket.getsockname()[1]
    https_port = https_socket.getsockname()[1]
    http_url = f"http://127.0.0.1:{http_port}"
    https_url = f"https://127.0.0.1:{https_port}"
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url=https_url if primary_https else http_url,
        web_origins=(http_url if primary_https else https_url,),
    )
    state = AuthStore(settings.auth_dir).ensure()
    initialize(settings.data_dir)
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    http_server = uvicorn.Server(
        uvicorn.Config(create_app(settings), lifespan="off", log_config=None, log_level="warning")
    )
    https_server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            lifespan="off",
            log_config=None,
            log_level="warning",
            ssl_keyfile=str(key),
            ssl_certfile=str(cert),
        )
    )
    threads = [
        threading.Thread(target=http_server.run, kwargs={"sockets": [http_socket]}, daemon=True),
        threading.Thread(target=https_server.run, kwargs={"sockets": [https_socket]}, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 10
        while (not http_server.started or not https_server.started) and time.monotonic() < deadline:
            time.sleep(0.025)
        assert http_server.started and https_server.started, "test servers did not start"

        with sync_playwright() as playwright:
            launch_options = {"executable_path": chrome_path} if chrome_path else {}
            browser = playwright.chromium.launch(headless=True, **launch_options)
            context = browser.new_context(ignore_https_errors=True)
            http_page = context.new_page()
            https_page = context.new_page()
            https_login_page = context.new_page()
            http_page.goto(f"{http_url}/access#{state.token}", wait_until="domcontentloaded")
            http_page.wait_for_url(f"{http_url}/", timeout=10_000)
            assert http_page.locator("main").is_visible()
            assert awaitable_evaluate(http_page, "window.location.hash") == ""

            https_page.goto(f"{https_url}/", wait_until="domcontentloaded")
            https_page.wait_for_url(f"{https_url}/access", timeout=10_000)
            assert https_page.locator("#access-form").is_visible()

            with https_login_page.expect_response(
                lambda response: response.url.endswith("/api/access/session")
            ) as exchange:
                https_login_page.goto(
                    f"{https_url}/access#{state.token}", wait_until="domcontentloaded"
                )
            assert exchange.value.status == 204
            https_login_page.wait_for_url(f"{https_url}/", timeout=10_000)
            assert https_login_page.locator("main").is_visible()
            assert awaitable_evaluate(https_login_page, "window.location.hash") == ""

            cookies = context.cookies()
            cookie_names = {cookie["name"] for cookie in cookies}
            assert "asr_studio_session" in cookie_names
            alias_names = {name for name in cookie_names if name.startswith("asr_studio_session_")}
            assert len(alias_names) == 1
            assert all(cookie["httpOnly"] for cookie in cookies)
            assert all(cookie["sameSite"] == "Strict" for cookie in cookies)
            assert all(cookie["domain"] == "127.0.0.1" for cookie in cookies)
            assert all(
                cookie["secure"] is (not primary_https)
                for cookie in cookies
                if cookie["name"] in alias_names
            )
            assert (
                next(cookie for cookie in cookies if cookie["name"] == "asr_studio_session")[
                    "secure"
                ]
                is primary_https
            )
            for page, origin in ((http_page, http_url), (https_login_page, https_url)):
                page.reload(wait_until="domcontentloaded")
                assert page.url == f"{origin}/"
                assert page.locator("main").is_visible()
                assert page.evaluate("fetch('/api/uploads/policy').then(r => r.status)") == 200
            browser.close()
    finally:
        http_server.should_exit = True
        https_server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)
        http_socket.close()
        https_socket.close()
