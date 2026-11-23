"""Native Chrome client against a disposable production ASR Studio server.

Synthetic tab and microphone tones exercise browser capture, not physical devices.
The optional permission dialog is approved only in this test's disposable Xvfb.
"""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
import uvicorn
from fake_chatgpt import FakeChatGPTAdapter
from fastapi.responses import JSONResponse
from native_processing_helpers import processing_settings, synthetic_processing_worker
from playwright.sync_api import expect, sync_playwright

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.extension_auth import EXTENSION_ID
from asr_studio.storage import database_path

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


class RequestLog(list):
    fail_create_responses = False
    cancel_not_found = False
    reject_recording_requests = False


def _approve_host_permission(page, server_url: str) -> None:
    """Accept Chrome's host-permission prompt, retrying presses that reached the page."""
    assert os.environ.get("ASR_EXTENSION_DISPOSABLE_XVFB") == "1"
    # A press sent before the prompt opens reaches the page; Tab then Return can activate a
    # page button and dismiss the prompt. The page swallows such presses and they are resent.
    page.evaluate("""() => {
        window.__promptKeys = 0;
        window.__swallowKey = (event) => {
            window.__promptKeys += 1;
            event.preventDefault();
            event.stopImmediatePropagation();
        };
        addEventListener('keydown', window.__swallowKey, true);
    }""")
    granted = "origin => chrome.permissions.contains({origins: [origin + '/*']})"
    try:
        for _ in range(10):
            time.sleep(1)
            seen = page.evaluate("window.__promptKeys")
            _press_tab_return()
            if page.evaluate("window.__promptKeys") == seen:
                break
        for _ in range(50):
            if page.evaluate(granted, server_url):
                return
            time.sleep(0.1)
        raise AssertionError(f"host permission for {server_url} was not granted")
    finally:
        page.evaluate("removeEventListener('keydown', window.__swallowKey, true)")


def _press_tab_return() -> None:
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XKeysymToKeycode.restype = ctypes.c_uint
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(os.environ["DISPLAY"].encode())
    assert display
    try:
        for key in (b"Tab", b"Return"):
            code = x11.XKeysymToKeycode(display, x11.XStringToKeysym(key))
            xtst.XTestFakeKeyEvent(display, code, 1, 0)
            xtst.XTestFakeKeyEvent(display, code, 0, 0)
            x11.XFlush(display)
            time.sleep(0.2)
    finally:
        x11.XCloseDisplay(display)


def _tone(path):
    rate = 48000
    samples = (np.sin(np.arange(rate * 30) * (2 * math.pi * 440 / rate)) * 7200).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())


@pytest.fixture(scope="module")
def capture_client(tmp_path_factory):
    if os.environ.get("ASR_RUN_EXTENSION_TESTS") != "1":
        pytest.skip("set ASR_RUN_EXTENSION_TESTS=1 for native extension capture")
    directory = tmp_path_factory.mktemp("production-client")
    bundle = directory / "client.zip"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/bundle-extension.py"), "--output", str(bundle)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    client = directory / "unpacked"
    with ZipFile(bundle) as archive:
        archive.extractall(client)
    return client


@pytest.fixture(params=["loopback", "https"])
def capture_server(tmp_path, request):
    if os.environ.get("ASR_RUN_EXTENSION_TESTS") != "1":
        pytest.skip("set ASR_RUN_EXTENSION_TESTS=1 for native extension capture")
    assert os.environ.get("ASR_EXTENSION_DISPOSABLE_XVFB") == "1", "use disposable xvfb-run"
    assert os.environ.get("DISPLAY"), "native extension test requires Xvfb"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    tls = request.param == "https"
    url = f"https://asr-native.test:{port}" if tls else f"http://127.0.0.1:{port}"
    failure = getattr(request.node, "callspec", None)
    quota = 128 * 1024 if failure and failure.params.get("failure") == "quota" else 512 * 1024**2
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url=url,
        extension_max_staging_bytes=quota,
    )
    adapter = FakeChatGPTAdapter()
    if request.node.name.startswith("test_native_capture_runs_transcript_and_notes"):
        settings = processing_settings(settings, tmp_path, adapter)
    app = create_app(settings, chatgpt_adapter=adapter)
    requests = RequestLog()
    transient_status = failure.params.get("transient_status") if failure else None
    activation_status = failure.params.get("activation_status") if failure else None
    activation_after = failure.params.get("activation_after", 0) if failure else 0
    activation_requests = 0
    # Chromium can transparently retry the first HTTP 408 before fetch sees it.
    transient_remaining = 2 if transient_status == 408 else 1

    @app.middleware("http")
    async def record_requests(request, call_next):
        nonlocal transient_remaining, activation_requests
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/extension/v1/session":
            activation_requests += 1
            if activation_status and activation_requests > activation_after:
                return JSONResponse({"error": {"code": "temporary"}}, status_code=activation_status)
        if (
            transient_status
            and transient_remaining > 0
            and request.method == "PUT"
            and "/chunks/" in request.url.path
        ):
            transient_remaining -= 1
            return JSONResponse({"error": {"code": "temporary"}}, status_code=transient_status)
        if requests.reject_recording_requests and request.url.path.startswith(
            "/api/extension/v1/recording-sessions"
        ):
            return JSONResponse({"error": {"code": "conflict"}}, status_code=409)
        if requests.cancel_not_found and request.url.path.endswith("/cancel"):
            # The server hides a session from another device's owner behind this 404.
            return JSONResponse({"error": {"code": "recording_not_found"}}, status_code=404)
        response = await call_next(request)
        if (
            requests.fail_create_responses
            and request.method == "POST"
            and request.url.path == "/api/extension/v1/recording-sessions"
        ):
            return JSONResponse({"error": {"code": "temporary"}}, status_code=503)
        return response

    ssl = {}
    if tls:
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
                "/CN=asr-native.test",
                "-addext",
                "subjectAltName=DNS:asr-native.test",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        ssl = {"ssl_keyfile": str(key), "ssl_certfile": str(cert)}
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning", **ssl))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.025)
    assert server.started
    try:
        yield settings, url, requests, tls
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def _status(page):
    return page.evaluate(
        "async () => (await chrome.runtime.sendMessage({target:'worker',type:'GET_STATUS'})).status"
    )


def _pair_again_code(operator, record_id):
    return operator.evaluate(
        """async recordId => {
            const response = await fetch('/api/extension/pairing-codes', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_lifetime: 'never', device_record_id: recordId})
            });
            if (!response.ok) throw new Error(`pair-again ${response.status}`);
            return (await response.json()).code;
        }""",
        record_id,
    )


class NativePopup:
    """Attach to Chrome's action popup, which Playwright does not expose as a Page."""

    def __init__(self, context, tab):
        self.tab = tab
        tab.bring_to_front()
        self.cdp = context.browser.new_browser_cdp_session()
        targets = self.cdp.send(
            "Target.getTargets", {"filter": [{"type": "tab", "exclude": False}]}
        )["targetInfos"]
        target = next(item for item in targets if item["type"] == "tab" and item["url"] == tab.url)
        self.cdp.send(
            "Extensions.triggerAction", {"id": EXTENSION_ID, "targetId": target["targetId"]}
        )
        deadline = time.monotonic() + 10
        popup = None
        while popup is None and time.monotonic() < deadline:
            popup = next(
                (
                    item
                    for item in self.cdp.send("Target.getTargets")["targetInfos"]
                    if item["url"] == f"chrome-extension://{EXTENSION_ID}/popup.html"
                ),
                None,
            )
            tab.wait_for_timeout(50)
        assert popup is not None, "native action popup did not open"
        self.target_id = popup["targetId"]
        self.session_id = self.cdp.send(
            "Target.attachToTarget", {"targetId": self.target_id, "flatten": False}
        )["sessionId"]
        self.responses = {}
        self.sequence = 0
        self.cdp.on("Target.receivedMessageFromTarget", self._receive)

    def _receive(self, event):
        if event["sessionId"] == self.session_id:
            message = json.loads(event["message"])
            if "id" in message:
                self.responses[message["id"]] = message

    def send(self, method, params):
        self.sequence += 1
        sequence = self.sequence
        self.cdp.send(
            "Target.sendMessageToTarget",
            {
                "sessionId": self.session_id,
                "message": json.dumps({"id": sequence, "method": method, "params": params}),
            },
        )
        deadline = time.monotonic() + 10
        while sequence not in self.responses and time.monotonic() < deadline:
            self.tab.wait_for_timeout(25)
        response = self.responses.pop(sequence)
        assert "error" not in response, response
        return response["result"]

    def evaluate(self, expression):
        result = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def wait_text(self, selector, text):
        deadline = time.monotonic() + 20
        expression = f"document.querySelector({json.dumps(selector)})?.textContent.trim()"
        while time.monotonic() < deadline:
            if self.evaluate(expression) == text:
                return
            self.tab.wait_for_timeout(50)
        raise AssertionError(self.evaluate("document.body.innerText"))

    def click(self, selector):
        rect = self.evaluate(f"""(() => {{
            const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();
            return {{x:r.x+r.width/2,y:r.y+r.height/2}};
        }})()""")
        for event in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": event, "button": "left", "clickCount": 1, **rect},
            )

    def screenshot(self, path):
        image = self.send("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(image["data"]))

    def close(self):
        self.cdp.send("Target.closeTarget", {"targetId": self.target_id})
        self.cdp.detach()


@pytest.fixture
def paired_capture(capture_server, capture_client, tmp_path, request):
    settings, url, requests, tls = capture_server
    mic = tmp_path / "mic.wav"
    _tone(mic)
    extension = capture_client
    args = [
        f"--disable-extensions-except={extension}",
        f"--load-extension={extension}",
        "--enable-unsafe-extension-debugging",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={mic}",
        "--auto-accept-camera-and-microphone-capture",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-proxy-server",
    ]
    if tls:
        # Disposable profile and fixture certificate only; no product TLS exception.
        args += [
            "--host-resolver-rules=MAP asr-native.test 127.0.0.1",
            "--ignore-certificate-errors",
        ]
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            tmp_path / "profile",
            channel="chromium",
            headless=False,
            args=args,
            viewport={"width": 1280, "height": 900},
        )
        try:
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker")
            )
            assert worker.url == f"chrome-extension://{EXTENSION_ID}/sw.js"
            operator = context.new_page()
            operator.goto(f"{url}/access#{AuthStore(settings.auth_dir).load().token}")
            operator.wait_for_url(f"{url}/")
            code = operator.evaluate("""async () => {
                const response = await fetch('/api/extension/pairing-codes', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({device_lifetime:'never'})});
                if (!response.ok) throw new Error(`pairing ${response.status}`);
                return (await response.json()).code;
            }""")
            control = context.new_page()
            control.goto(f"chrome-extension://{EXTENSION_ID}/control.html")
            control.evaluate("localStorage.setItem('asr-extension-production-v1:lang','en')")
            control.reload()
            control.locator("#mic-button").click()
            expect(control.locator("#mic-state")).to_contain_text("Permission granted")
            control.locator("#server-url").fill(url)
            control.locator("#pair-code").fill(code)
            control.locator("#pair-button").click()
            _approve_host_permission(control, url)
            callspec = getattr(request.node, "callspec", None)
            if (
                callspec
                and callspec.params.get("activation_status")
                and not callspec.params.get("activation_after", 0)
            ):
                expect(control.locator("#global-error")).to_have_text(
                    "The server is temporarily unavailable.", timeout=20000
                )
            else:
                expect(control.locator("#connection-dot")).to_have_class(
                    "status-dot is-ready", timeout=20000
                )
                expect(control.locator("#connection-label")).to_have_text(url)
            tab = context.new_page()
            tab.goto(url + "?synthetic-tab=1")
            tab.set_content('<button id="tone">Synthetic tab</button>')
            tab.evaluate("""() => {
                document.querySelector('#tone').onclick = () => {
                    const context = new AudioContext(), oscillator = context.createOscillator();
                    const gain = context.createGain(); oscillator.frequency.value = 880;
                    gain.gain.value = .18; oscillator.connect(gain).connect(context.destination);
                    oscillator.start(); window.audio = {context,oscillator,gain};
                };
            }""")
            tab.locator("#tone").click()
            assert tab.evaluate("window.audio.context.state") == "running"
            yield context, worker, operator, control, tab
        finally:
            context.close()


def test_native_capture_keeps_recording_without_controls_and_finishes_at_raw_receipt(
    capture_server, paired_capture, tmp_path
):
    settings, url, requests, _tls = capture_server
    context, worker, operator, control, tab = paired_capture
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    assert popup.evaluate("document.documentElement.scrollWidth") <= 400
    assert popup.evaluate('document.getElementById("release-panel").hidden')
    popup.screenshot(tmp_path / "popup-recording.png")
    popup.close()
    control.close()
    # No control page remains; the native offscreen owner continues persisting audio.
    tab.wait_for_timeout(7500)
    popup = NativePopup(context, tab)
    popup.wait_text("#capture-state", "Recording")
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        server_row = db.execute("SELECT next_sequence FROM extension_recording_sessions").fetchone()
        assert server_row and server_row[0] > 0, server_row
    requests_before_stop = list(requests)
    assert any(method == "PUT" and "/chunks/" in path for method, path in requests_before_stop)
    popup.click("#stop-button")
    stopping_seen = False
    stopping_deadline = time.monotonic() + 5
    while time.monotonic() < stopping_deadline:
        if (
            popup.evaluate('document.getElementById("capture-state")?.textContent.trim()')
            == "Stopping recording"
        ):
            stopping_seen = True
            break
        tab.wait_for_timeout(25)
    assert stopping_seen
    popup.close()
    control = context.new_page()
    control.goto(f"chrome-extension://{EXTENSION_ID}/control.html")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = _status(control)
        if status["sessions"] and status["sessions"][0]["upload"]["receipt"] is None:
            row = control.locator(".session-row").first
            if row.count():
                row_text = row.inner_text()
                assert "Uploaded" not in row_text
                assert "Saved" not in row_text
        if status["sessions"] and status["sessions"][0]["upload"]["cleaned"]:
            break
        control.wait_for_timeout(150)
    assert status["sessions"][0]["upload"]["cleaned"], status
    (tmp_path / "status.json").write_text(json.dumps(status, indent=2))
    assert len(status["sessions"]) == 1
    session = status["sessions"][0]
    assert session["capture"]["captureComplete"]
    assert session["upload"]["receipt"]["recording_id"] == session["summary"]["recordingId"]
    receipt_row = control.locator(".session-row").first
    expect(receipt_row.locator(".session-title")).to_have_text("Server saved the raw audio")
    expect(receipt_row).to_contain_text("View processing status in ASR Studio.")
    expect(receipt_row).not_to_contain_text("Uploaded")
    assert (
        sum(method == "PUT" and "/chunks/" in path for method, path in requests)
        == session["capture"]["chunkCount"]
    )
    chunks = control.evaluate("""async () => {
        const db = await new Promise((resolve,reject) => {
            const req=indexedDB.open('asr-extension-production-v1');
            req.onsuccess=()=>resolve(req.result); req.onerror=()=>reject(req.error);
        });
        return new Promise(resolve => {
            const req=db.transaction('chunks').objectStore('chunks').count();
            req.onsuccess=()=>{ db.close(); resolve(req.result); };
        });
    }""")
    assert chunks == 0
    extension_requests = [item for item in requests if item[1].startswith("/api/extension/v1/")]
    control.wait_for_timeout(2500)
    assert [
        item for item in requests if item[1].startswith("/api/extension/v1/")
    ] == extension_requests
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        row = db.execute("SELECT raw_key, receipt_at FROM extension_recording_sessions").fetchone()
        assert row and row[1]
        assert db.execute("SELECT count(*) FROM meetings").fetchone()[0] == 1
    raw = settings.data_dir / row[0]
    samples = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-fflags",
            "+noparse",
            "-i",
            str(raw),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout
    values = np.frombuffer(samples, dtype="<f4")
    frequencies = np.fft.rfftfreq(len(values), 1 / 16000)
    spectrum = np.abs(np.fft.rfft(values))
    peaks = {
        frequency: float(spectrum[np.abs(frequencies - frequency) < 5].max())
        for frequency in (440, 880)
    }
    assert min(peaks.values()) > float(np.median(spectrum)) * 100, peaks
    (tmp_path / "signals.json").write_text(json.dumps(peaks))
    control.screenshot(path=str(tmp_path / "receipt.png"))
    with context.expect_page() as opened:
        control.locator('[data-action="result"]').click()
    meeting = opened.value
    meeting.wait_for_url(session["upload"]["meetingUrl"])
    assert meeting.url.startswith(url + "/meetings/")
    meeting.close()

    # A second capture has real retained bytes when server authority is revoked.
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    tab.wait_for_timeout(1500)
    operator.evaluate("""async () => {
        const {devices} = await (await fetch('/api/extension/devices')).json();
        const path = `/api/extension/devices/${devices[0].id}/revoke`;
        const response = await fetch(path, {method:'POST'});
        if (!response.ok) throw new Error(`revoke ${response.status}`);
    }""")
    control.locator("#stop-button").click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        failed = next((s for s in _status(control)["sessions"] if not s["upload"]["cleaned"]), None)
        if failed and failed["upload"]["error"]:
            break
        control.wait_for_timeout(100)
    assert failed and failed["capture"]["captureComplete"] and failed["upload"]["error"], failed
    assert failed["capture"]["chunkCount"] > 0
    assert failed["upload"]["receipt"] is None
    create_requests_before_repair = sum(
        method == "POST" and path == "/api/extension/v1/recording-sessions"
        for method, path in requests
    )
    record_id = operator.evaluate(
        "async () => (await (await fetch('/api/extension/devices')).json()).devices[0].id"
    )
    repair_code = _pair_again_code(operator, record_id)
    cdp = context.browser.new_browser_cdp_session()
    try:
        worker_target = next(
            item
            for item in cdp.send("Target.getTargets")["targetInfos"]
            if item["type"] == "service_worker" and item["url"] == worker.url
        )
        cdp.send("Target.closeTarget", {"targetId": worker_target["targetId"]})
    finally:
        cdp.detach()
    control.reload()
    expect(control.locator(".session-row")).to_have_count(2)
    recovered = next(s for s in _status(control)["sessions"] if s["id"] == failed["id"])
    assert recovered["capture"]["totalBytes"] == failed["capture"]["totalBytes"]
    assert not recovered["upload"]["cleaned"] and recovered["upload"]["receipt"] is None
    first = control.locator(".session-row").first
    expect(first).to_have_attribute("data-session-id", failed["id"])
    for language in ("en", "vi"):
        for theme in ("light", "dark"):
            control.evaluate(
                """([lang, theme]) => {
                localStorage.setItem('asr-extension-production-v1:lang',lang);
                localStorage.setItem('asr-extension-production-v1:theme',theme);
            }""",
                [language, theme],
            )
            control.reload()
            expect(control.locator("html")).to_have_attribute("lang", language)
            expect(control.locator("html")).to_have_attribute("data-theme", theme)
            retry = control.locator('[data-action="retry"]').first
            retry.focus()
            control.keyboard.press("Enter")
            expect(retry).to_be_enabled()
            assert control.evaluate("document.activeElement.dataset.action") == "retry"
            expect(first.locator(".error")).not_to_be_empty()
            control.screenshot(path=str(tmp_path / f"revoked-{language}-{theme}.png"))

    control.locator("#setup-toggle").click()
    control.locator("#pair-code").fill(repair_code)
    control.locator("#pair-button").click()
    expect(control.locator("#setup-panel")).to_be_hidden(timeout=20000)
    deadline = time.monotonic() + 45
    recovered = None
    while time.monotonic() < deadline:
        recovered = next(
            (item for item in _status(control)["sessions"] if item["id"] == failed["id"]),
            None,
        )
        if recovered and recovered["upload"]["cleaned"]:
            break
        control.wait_for_timeout(150)
    assert recovered and recovered["upload"]["cleaned"] and recovered["upload"]["receipt"], (
        recovered
    )
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT count(*) FROM meetings").fetchone()[0] == 2
    assert (
        sum(
            method == "POST" and path == "/api/extension/v1/recording-sessions"
            for method, path in requests
        )
        == create_requests_before_repair
    )
    assert len(_status(control)["sessions"]) == 2


@pytest.mark.parametrize("failure", ["permission", "quota"])
def test_native_failures_retain_audio(capture_server, paired_capture, failure):
    _settings, url, requests, _tls = capture_server
    context, _worker, operator, control, tab = paired_capture
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    tab.wait_for_timeout(1500)
    if failure == "permission":
        assert control.evaluate("origin => chrome.permissions.remove({origins:[origin+'/*']})", url)
        control.locator("#stop-button").click()
    deadline = time.monotonic() + 15
    session = None
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        session = sessions[0] if sessions else None
        if session and (session["upload"]["error"] or session["capture"]["interrupted"]):
            break
        control.wait_for_timeout(100)
    assert session and session["capture"]["chunkCount"] > 0, session
    assert not session["upload"]["cleaned"] and session["upload"]["receipt"] is None
    if failure == "quota":
        assert session["capture"]["interrupted"] and not session["capture"]["captureComplete"]
        assert any(path == "/api/extension/v1/recording-sessions" for _method, path in requests)
        # An interruption waits for the user's Save/Cancel choice; it never auto-cancels.
        assert session["upload"]["awaitingChoice"]
        assert not any(path.endswith("/cancel") for _method, path in requests)
        record_id = operator.evaluate(
            "async () => (await (await fetch('/api/extension/devices')).json()).devices[0].id"
        )
        repair_code = _pair_again_code(operator, record_id)
        control.locator("#setup-toggle").click()
        control.locator("#pair-code").fill(repair_code)
        control.locator("#pair-button").click()
        expect(control.locator("#setup-panel")).to_be_hidden(timeout=20000)
        retained_after_repair = next(
            item for item in _status(control)["sessions"] if item["id"] == session["id"]
        )
        assert retained_after_repair["capture"]["interrupted"]
        assert retained_after_repair["capture"]["totalBytes"] == session["capture"]["totalBytes"]
    else:
        assert session["capture"]["captureComplete"] and session["upload"]["error"]
        assert session["upload"]["error"]["code"] == "host_permission"
        assert any(path == "/api/extension/v1/recording-sessions" for _method, path in requests)
        retry = control.locator('[data-action="retry"]')
        retry.focus()
        control.keyboard.press("Enter")
        _approve_host_permission(control, url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            restored = _status(control)["sessions"][0]
            if restored["upload"]["cleaned"]:
                break
            control.wait_for_timeout(100)
        assert restored["upload"]["cleaned"] and restored["upload"]["receipt"]
        expect(control.locator('[data-action="result"]')).to_be_focused()
        return
    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="discard"]').click()
    expect(control.locator(".session-row")).to_have_count(0)


def test_native_uncertain_create_waits_for_choice_then_cancel_reconciles(
    capture_server, paired_capture
):
    settings, url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    requests.fail_create_responses = True

    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()

    session = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        session = sessions[0] if sessions else None
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            server_row = db.execute(
                "SELECT client_recording_id, state FROM extension_recording_sessions"
            ).fetchone()
        if session and session["capture"]["chunkCount"] > 0 and server_row:
            break
        control.wait_for_timeout(150)
    assert session and session["capture"]["chunkCount"] > 0, session
    assert server_row == (session["id"], "receiving"), server_row
    assert session["upload"]["receipt"] is None
    assert any(
        method == "POST" and path == "/api/extension/v1/recording-sessions"
        for method, path in requests
    )

    cdp = context.browser.new_browser_cdp_session()
    try:
        targets = cdp.send("Target.getTargets")["targetInfos"]
        offscreen = next(
            (
                item
                for item in targets
                if item["url"] == f"chrome-extension://{EXTENSION_ID}/offscreen.html"
            ),
            None,
        )
        assert offscreen, targets
        cdp.send("Target.closeTarget", {"targetId": offscreen["targetId"]})
        requests.fail_create_responses = False
        targets = cdp.send("Target.getTargets")["targetInfos"]
        service_worker = next(
            (item for item in targets if item["type"] == "service_worker"),
            None,
        )
        if service_worker:
            cdp.send("Target.closeTarget", {"targetId": service_worker["targetId"]})
    finally:
        cdp.detach()

    control.reload()
    recovered = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        recovered = next((item for item in sessions if item["id"] == session["id"]), None)
        if recovered and recovered["upload"]["awaitingChoice"]:
            break
        control.wait_for_timeout(200)
    assert recovered and recovered["upload"]["awaitingChoice"], recovered
    assert recovered["capture"]["interruptionCode"] == "owner_restarted"
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute(
            "SELECT client_recording_id, state FROM extension_recording_sessions"
        ).fetchall() == [(session["id"], "receiving")]
    assert not any(path.endswith("/cancel") for _method, path in requests)

    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="cancel-remote"]').click()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        recovered = next((item for item in sessions if item["id"] == session["id"]), None)
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            server_rows = db.execute(
                "SELECT client_recording_id, state FROM extension_recording_sessions"
            ).fetchall()
        if recovered and server_rows == [(session["id"], "canceled")]:
            break
        control.wait_for_timeout(200)

    assert recovered and recovered["capture"]["interrupted"], recovered
    assert recovered["capture"]["chunkCount"] > 0
    assert recovered["capture"]["totalBytes"] > 0
    assert not recovered["upload"]["cleaned"] and recovered["upload"]["receipt"] is None
    assert server_rows == [(session["id"], "canceled")]
    local_chunks = control.evaluate("""async () => {
        const db = await new Promise((resolve, reject) => {
            const req = indexedDB.open('asr-extension-production-v1');
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
        return new Promise((resolve, reject) => {
            const req = db.transaction('chunks').objectStore('chunks').count();
            req.onsuccess = () => { db.close(); resolve(req.result); };
            req.onerror = () => { db.close(); reject(req.error); };
        });
    }""")
    assert local_chunks == recovered["capture"]["chunkCount"]
    assert (
        sum(
            method == "POST" and path == "/api/extension/v1/recording-sessions"
            for method, path in requests
        )
        >= 2
    )
    assert any(path.endswith("/cancel") for _method, path in requests)
    assert not any(path.endswith("/finalize") for _method, path in requests)


def _record_then_close_source_tab(settings, context, control, tab):
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            row = db.execute("SELECT next_sequence FROM extension_recording_sessions").fetchone()
        if row and row[0] >= 2:
            break
        control.wait_for_timeout(200)
    assert row and row[0] >= 2, row
    # Closing the meeting tab ends the tab audio track before any Stop.
    tab.close()
    session = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        session = sessions[0] if sessions else None
        if session and session["upload"]["awaitingChoice"]:
            break
        control.wait_for_timeout(200)
    assert session and session["upload"]["awaitingChoice"], session
    assert session["capture"]["interrupted"] and not session["capture"]["captureComplete"]
    assert session["capture"]["interruptionCode"] in {"source_ended", "source_inactive"}
    return session


def test_native_source_end_then_save_creates_labeled_meeting(
    capture_server, paired_capture, tmp_path
):
    settings, _url, requests, _tls = capture_server
    context, _worker, operator, control, tab = paired_capture
    session = _record_then_close_source_tab(settings, context, control, tab)
    labels = {"en": "Save recorded part", "vi": "Lưu phần đã ghi"}
    for language in ("vi", "en"):
        for theme in ("light", "dark"):
            control.evaluate(
                """([lang, theme]) => {
                localStorage.setItem('asr-extension-production-v1:lang',lang);
                localStorage.setItem('asr-extension-production-v1:theme',theme);
            }""",
                [language, theme],
            )
            control.reload()
            expect(control.locator("html")).to_have_attribute("lang", language)
            expect(control.locator("html")).to_have_attribute("data-theme", theme)
            save = control.locator('[data-action="save-partial"]')
            expect(save).to_have_text(labels[language])
            save.focus()
            expect(save).to_be_focused()
            control.screenshot(path=str(tmp_path / f"interrupted-{language}-{theme}.png"))
    popup = NativePopup(context, control)
    popup.wait_text("#save-partial-button", "Save recorded part")
    assert popup.evaluate("document.querySelector('#recovery-panel').hidden") is False
    assert popup.evaluate("document.documentElement.scrollWidth") <= 400
    popup.screenshot(tmp_path / "popup-interrupted.png")
    popup.close()
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT state FROM extension_recording_sessions").fetchone() == (
            "receiving",
        )
    assert not any(path.endswith("/cancel") for _method, path in requests)
    # The server recorded the interruption before the user chose; Save depends on it.
    assert any(path.endswith("/interrupt") for _method, path in requests)
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute(
            "SELECT capture_interruption_code FROM extension_recording_sessions"
        ).fetchone() == (session["capture"]["interruptionCode"],)
    expect(control.locator('[data-action="save-partial"]')).to_be_visible()
    expect(control.locator('[data-role="interruption"]')).to_contain_text("recorded")

    control.locator('[data-action="save-partial"]').click()
    saved = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        saved = next(
            (item for item in _status(control)["sessions"] if item["id"] == session["id"]), None
        )
        if saved and saved["upload"]["cleaned"]:
            break
        control.wait_for_timeout(200)
    assert saved and saved["upload"]["cleaned"] and saved["upload"]["receipt"], saved
    assert saved["upload"]["receipt"]["chunk_count"] == session["capture"]["chunkCount"]
    control.reload()
    partial_row = control.locator(".session-row").first
    expect(partial_row.locator(".session-title")).to_have_text("Server saved the partial recording")
    expect(partial_row).to_contain_text("raw audio saved")
    # The receipt follows the raw archive; media normalization finishes afterwards.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            row = db.execute(
                "SELECT state, finalize_capture_complete, capture_interruption_code "
                "FROM extension_recording_sessions"
            ).fetchone()
            workflow = db.execute("SELECT state FROM processing_workflows").fetchone()
        if row[0] in {"ready", "failed"}:
            break
        control.wait_for_timeout(200)
    assert row == ("ready", 0, session["capture"]["interruptionCode"]), row
    assert workflow == ("needs_attention",)
    meetings = operator.evaluate(
        "async () => (await (await fetch('/api/meetings')).json()).meetings"
    )
    assert meetings[0]["recording_interruption_code"] == session["capture"]["interruptionCode"]
    detail = operator.evaluate(
        "async id => (await (await fetch('/api/meetings/' + id)).json())", meetings[0]["id"]
    )
    assert detail["recording"]["interrupted"] == {"code": session["capture"]["interruptionCode"]}
    assert detail["processing"]["actions"]["use_current_source"] is True
    finalize = [path for method, path in requests if path.endswith("/finalize")]
    assert len(finalize) == 1
    operator.goto(f"{_url}/meetings/{meetings[0]['id']}")
    expect(operator.locator("#recording-status")).to_contain_text("gián đoạn", timeout=20000)
    start = operator.locator("#automatic-processing-use-source")
    expect(start).to_be_visible()
    expect(start).to_have_text("Chép lời phần đã ghi")


def test_native_stop_overlapping_source_end_keeps_the_reason_and_save_reaches_ready(
    capture_server, paired_capture
):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    session = _record_then_close_source_tab(settings, context, control, tab)
    reason = session["capture"]["interruptionCode"]
    # Stop's late write used to turn the interrupted row back into `stopping`.
    control.evaluate(
        """async () => {
        const db = await new Promise((resolve, reject) => {
            const req = indexedDB.open('asr-extension-production-v1');
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
        await new Promise((resolve, reject) => {
            const tx = db.transaction('sessions', 'readwrite');
            const store = tx.objectStore('sessions');
            const req = store.getAll();
            req.onsuccess = () => {
                for (const row of req.result) store.put({
                    ...row, capture: { ...row.capture, status: 'stopping' }
                });
            };
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
        });
        db.close();
    }"""
    )
    # A status read reconciles the row with no capture owner; the reason must survive.
    reconciled = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        reconciled = _status(control)["sessions"][0]
        if reconciled["capture"]["status"] == "interrupted":
            break
        control.wait_for_timeout(200)
    assert reconciled["capture"]["status"] == "interrupted", reconciled["capture"]
    assert reconciled["capture"]["interruptionCode"] == reason
    control.locator('[data-action="save-partial"]').click()
    deadline = time.monotonic() + 60
    row = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            row = db.execute(
                "SELECT state, capture_interruption_code FROM extension_recording_sessions"
            ).fetchone()
        if row[0] in {"ready", "failed"}:
            break
        control.wait_for_timeout(200)
    assert row == ("ready", reason), row
    assert len([path for _method, path in requests if path.endswith("/finalize")]) == 1


@pytest.mark.parametrize("activation_status,activation_after", [(503, 1)])
def test_native_popup_keeps_recovery_after_failed_same_server_repair(
    capture_server, paired_capture, activation_status, activation_after
):
    settings, url, _requests, _tls = capture_server
    context, _worker, operator, control, tab = paired_capture
    assert (activation_status, activation_after) == (503, 1)
    _record_then_close_source_tab(settings, context, control, tab)
    devices = operator.evaluate(
        "async () => (await (await fetch('/api/extension/devices')).json()).devices"
    )
    code = _pair_again_code(operator, devices[0]["id"])
    control.locator("#setup-toggle").click()
    control.locator("#pair-code").fill(code)
    control.locator("#pair-button").click()
    expect(control.locator("#global-error")).to_have_text(
        "The server is temporarily unavailable.", timeout=20000
    )
    connection = _status(control)["connection"]
    assert not connection.get("paired") and not connection.get("backendId")
    page = context.new_page()
    page.goto(url + "?popup-after-failed-repair=1")
    popup = NativePopup(context, page)
    popup.wait_text("#save-partial-button", "Save recorded part")
    assert popup.evaluate("document.querySelector('#recovery-panel').hidden") is False
    assert popup.evaluate("document.querySelector('#recovery-duration').textContent").strip()
    assert popup.evaluate("document.querySelector('#recovery-reason').textContent").strip()
    assert popup.evaluate("document.querySelector('#save-partial-button').disabled") is False
    assert popup.evaluate("document.querySelector('#cancel-remote-button').disabled") is False
    popup.close()


def test_native_source_end_then_cancel_keeps_local_audio(capture_server, paired_capture):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    session = _record_then_close_source_tab(settings, context, control, tab)
    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="cancel-remote"]').click()
    canceled = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        canceled = next(
            (item for item in _status(control)["sessions"] if item["id"] == session["id"]), None
        )
        if canceled and canceled["upload"]["cancelSettled"]:
            break
        control.wait_for_timeout(200)
    assert canceled and canceled["upload"]["cancelSettled"], canceled
    assert not canceled["upload"]["awaitingChoice"] and not canceled["upload"]["cleaned"]
    assert canceled["capture"]["chunkCount"] == session["capture"]["chunkCount"]
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT state FROM extension_recording_sessions").fetchone() == (
            "canceled",
        )
        assert db.execute("SELECT count(*) FROM processing_workflows").fetchone() == (0,)
    assert any(path.endswith("/cancel") for _method, path in requests)
    assert not any(path.endswith("/finalize") for _method, path in requests)
    expect(control.locator('[data-action="save-partial"]')).to_have_count(0)


def test_native_stale_cancel_cannot_override_a_committed_save(capture_server, paired_capture):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    session = _record_then_close_source_tab(settings, context, control, tab)
    # Two stale views choose in the same instant: the first choice wins, the other is refused.
    results = control.evaluate(
        """async id => {
            const send = type => chrome.runtime.sendMessage({target:'worker', type, sessionId:id});
            const save = send('SAVE_INTERRUPTED');
            const cancel = send('CANCEL_INTERRUPTED');
            return Promise.all([save, cancel]);
        }""",
        session["id"],
    )
    assert results[0]["ok"] is True, results
    assert results[1]["ok"] is False and results[1]["error"]["code"] == "already_saving", results
    saved = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        saved = next(
            (item for item in _status(control)["sessions"] if item["id"] == session["id"]), None
        )
        if saved and saved["upload"]["cleaned"]:
            break
        control.wait_for_timeout(200)
    assert saved and saved["upload"]["receipt"] and not saved["upload"]["cancelSettled"], saved
    assert not any(path.endswith("/cancel") for _method, path in requests)
    assert sum(path.endswith("/finalize") for _method, path in requests) == 1


def _interrupt_before_first_chunk(settings, context, control, tab):
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    deadline = time.monotonic() + 20
    row = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            row = db.execute(
                "SELECT state, next_sequence FROM extension_recording_sessions"
            ).fetchone()
        if row:
            break
        control.wait_for_timeout(50)
    # The first chunk arrives one timeslice (5 s) after start; close the source before it.
    assert row == ("receiving", 0), row
    tab.close()
    session = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        session = sessions[0] if sessions else None
        if session and session["capture"]["interrupted"]:
            break
        control.wait_for_timeout(200)
    assert session and session["capture"]["interrupted"], session
    assert session["capture"]["chunkCount"] == 0
    assert session["upload"]["awaitingChoice"] is True, session
    return session


def test_native_interruption_before_the_first_chunk_still_offers_cancel(
    capture_server, paired_capture
):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    _interrupt_before_first_chunk(settings, context, control, tab)
    control.reload()
    expect(control.locator('[data-role="interruption"]')).to_contain_text("00:00")
    expect(control.locator('[data-action="save-partial"]')).to_be_disabled()
    cancel = control.locator('[data-action="cancel-remote"]')
    expect(cancel).to_be_enabled()
    control.once("dialog", lambda dialog: dialog.accept())
    cancel.click()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            state = db.execute("SELECT state FROM extension_recording_sessions").fetchone()
        if state == ("canceled",):
            break
        control.wait_for_timeout(200)
    assert state == ("canceled",), state


def test_native_server_url_is_locked_while_an_empty_interrupted_session_awaits_cancel(
    capture_server, paired_capture
):
    settings, url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    session = _interrupt_before_first_chunk(settings, context, control, tab)
    other = "http://127.0.0.1:9" if url.startswith("https") else "https://asr-other.test"
    refused = control.evaluate(
        """async serverUrl => chrome.runtime.sendMessage(
            {target:'worker', type:'SET_SERVER_URL', serverUrl})""",
        other,
    )
    assert refused["ok"] is False and refused["error"]["code"] == "url_locked", refused
    # The original context survives, so Cancel still reaches the receiving server session.
    control.reload()
    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="cancel-remote"]').click()
    deadline = time.monotonic() + 30
    state = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            state = db.execute("SELECT state FROM extension_recording_sessions").fetchone()
        if state == ("canceled",):
            break
        control.wait_for_timeout(200)
    assert state == ("canceled",), (state, session)
    assert any(path.endswith("/cancel") for _method, path in requests)


def test_native_cancel_404_stays_unresolved_until_the_original_device_returns(
    capture_server, paired_capture
):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    session = _interrupt_before_first_chunk(settings, context, control, tab)
    requests.cancel_not_found = True
    control.reload()
    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="cancel-remote"]').click()
    deadline = time.monotonic() + 30
    row = None
    while time.monotonic() < deadline:
        row = next(
            (item for item in _status(control)["sessions"] if item["id"] == session["id"]), None
        )
        if row and row["upload"]["error"]:
            break
        control.wait_for_timeout(200)
    assert row and row["upload"]["error"]["code"] == "not_found", row
    assert not row["upload"]["cancelSettled"], row
    expect(control.locator(".session-row")).to_contain_text("Pair again")
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT state FROM extension_recording_sessions").fetchone() == (
            "receiving",
        )
    # Restoring the original device lets the same Cancel go through.
    requests.cancel_not_found = False
    retried = control.evaluate(
        """async id => chrome.runtime.sendMessage(
            {target:'worker', type:'RETRY_UPLOAD', sessionId:id})""",
        session["id"],
    )
    assert retried["ok"] is True, retried
    deadline = time.monotonic() + 30
    state = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            state = db.execute("SELECT state FROM extension_recording_sessions").fetchone()
        if state == ("canceled",):
            break
        control.wait_for_timeout(200)
    assert state == ("canceled",), state


def test_native_offscreen_loss_with_a_live_worker_is_reported_as_interrupted(
    capture_server, paired_capture
):
    settings, _url, _requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    deadline = time.monotonic() + 30
    chunks = 0
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        chunks = sessions[0]["capture"]["chunkCount"] if sessions else 0
        if chunks > 0:
            break
        control.wait_for_timeout(200)
    assert chunks > 0
    cdp = context.browser.new_browser_cdp_session()
    try:
        targets = cdp.send("Target.getTargets")["targetInfos"]
        offscreen = next(
            item
            for item in targets
            if item["url"] == f"chrome-extension://{EXTENSION_ID}/offscreen.html"
        )
        # Only the offscreen owner closes; the service worker keeps running.
        cdp.send("Target.closeTarget", {"targetId": offscreen["targetId"]})
    finally:
        cdp.detach()
    status = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = _status(control)
        if status["capture"] and status["capture"]["status"] == "interrupted":
            break
        control.wait_for_timeout(200)
    assert status["capture"]["status"] == "interrupted", status["capture"]
    # Closing the owner may end its tracks first; the first committed reason wins.
    assert status["capture"]["interruptionCode"] in {"owner_restarted", "source_ended"}
    session = status["sessions"][0]
    assert session["upload"]["awaitingChoice"] is True, session
    # The same worker can start a new capture; the lost owner no longer blocks Start.
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()


@pytest.mark.parametrize("transient_status", [408, 429])
def test_native_temporary_http_failure_retries_retained_audio(
    capture_server, paired_capture, transient_status
):
    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    tab.wait_for_timeout(1500)
    deadline = time.monotonic() + 15
    session = None
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        session = sessions[0] if sessions else None
        if session and session["upload"]["error"]:
            break
        control.wait_for_timeout(100)
    assert session and session["capture"]["status"] in {"recording", "stopping"}, session
    control.locator("#stop-button").click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        session = _status(control)["sessions"][0]
        if session["upload"]["error"] and session["capture"]["captureComplete"]:
            break
        control.wait_for_timeout(100)
    assert session and session["upload"]["state"] == "retryable", session
    assert not session["upload"]["cleaned"] and session["upload"]["receipt"] is None
    assert session["capture"]["captureComplete"] and session["capture"]["chunkCount"] > 0
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        session = _status(control)["sessions"][0]
        if session["upload"]["cleaned"]:
            break
        control.wait_for_timeout(100)
    assert session["upload"]["cleaned"] and session["upload"]["receipt"], session
    assert sum(method == "PUT" and "/chunks/" in path for method, path in requests) >= 2
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT count(*) FROM meetings").fetchone()[0] == 1


def test_paired_extension_retains_and_retries_audio_after_server_api_change(
    capture_server, paired_capture, monkeypatch
):
    from asr_studio import extension_routes

    settings, _url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
    connection = _status(control)["connection"]
    assert connection["paired"]
    version = extension_routes.EXTENSION_API_VERSION
    monkeypatch.setattr(extension_routes, "EXTENSION_API_VERSION", version + 1)
    requests.reject_recording_requests = True

    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    # Pairing already minted a token, so the rejected request must trigger a fresh probe.
    warning = "Incompatible API. Check the extension and server versions."
    popup.wait_text("#error-copy", warning)
    assert popup.evaluate("!document.querySelector('#error-copy').parentElement.hidden")
    tab.wait_for_timeout(1500)
    popup.click("#stop-button")
    popup.close()

    expect(control.locator("#global-error")).to_have_text(warning)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = _status(control)
        session = status["sessions"][0]
        if session["capture"]["captureComplete"] and session["upload"]["error"]:
            break
        control.wait_for_timeout(100)
    assert status["connection"] == connection
    assert session["capture"]["captureComplete"] and session["capture"]["chunkCount"] > 0
    assert session["upload"]["state"] == "retryable"
    assert session["upload"]["error"]["code"] == "api_version_mismatch"
    assert not session["upload"]["cleaned"] and not session["upload"]["receipt"]
    assert sum(path.endswith("/capabilities") for _method, path in requests) >= 2
    assert ("POST", "/api/extension/v1/recording-sessions") in requests
    assert not any(path.endswith("/finalize") for _method, path in requests)

    monkeypatch.setattr(extension_routes, "EXTENSION_API_VERSION", version)
    requests.reject_recording_requests = False
    expect(control.locator("#global-error")).to_have_text("", timeout=20000)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        status = _status(control)
        session = status["sessions"][0]
        if session["upload"]["cleaned"]:
            break
        control.wait_for_timeout(100)
    assert session["upload"]["cleaned"] and session["upload"]["receipt"]["recording_id"]
    assert status["connection"] == connection
    assert sum(path.endswith("/finalize") for _method, path in requests) == 1
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        assert db.execute("SELECT count(*) FROM recordings").fetchone()[0] == 1


def test_native_capture_runs_transcript_and_notes_after_client_receipt(
    capture_server, paired_capture, tmp_path, monkeypatch
):
    settings, url, requests, _tls = capture_server
    context, _worker, operator, control, tab = paired_capture
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    popup.close()
    tab.wait_for_timeout(1600)
    control.locator("#stop-button").click()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        if sessions and sessions[0]["upload"]["cleaned"]:
            break
        control.wait_for_timeout(100)
    assert sessions[0]["upload"]["cleaned"]
    session = sessions[0]
    with sqlite3.connect(database_path(settings.data_dir)) as db:
        meeting_id = db.execute("SELECT id FROM meetings").fetchone()[0]
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    extension_requests = [item for item in requests if item[1].startswith("/api/extension/v1/")]

    def processing():
        return operator.evaluate(
            "async id => (await (await fetch(`/api/meetings/${id}`)).json()).processing",
            meeting_id,
        )

    with synthetic_processing_worker(settings, tmp_path, monkeypatch):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = processing()
            if state and any(
                s["kind"] == "transcription" and s["state"] == "succeeded" for s in state["steps"]
            ):
                break
            operator.wait_for_timeout(100)
        assert state, "ready ingest never created processing workflow"
        assert (
            next(s for s in state["steps"] if s["kind"] == "transcription")["state"] == "succeeded"
        ), state
        assert (
            next(s for s in state["steps"] if s["kind"] == "notes")["waiting_reason"]
            == "provider_unconfigured"
        ), state
        preferences = operator.evaluate("""async () => {
            const response = await fetch('/api/processing-preferences', {method:'PUT',
              headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                asr_choice:'auto', asr_language:'vi', notes_provider:'chatgpt',
                notes_model:'gpt-6.1-sol', notes_reasoning_effort:'high',
                notes_output_language:'en'})});
            if (!response.ok) throw new Error(await response.text());
            return await response.json();
        }""")
        operator.evaluate(
            """async ({id, epoch, version}) => {
            const response = await fetch(`/api/meetings/${id}/processing/apply-current-settings`, {
              method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                expected_pipeline_epoch:epoch, expected_preferences_version:version})});
            if (!response.ok) throw new Error(await response.text());
        }""",
            {"id": meeting_id, "epoch": state["epoch"], "version": preferences["version"]},
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = processing()
            if state and all(s["state"] == "succeeded" for s in state["steps"]):
                break
            operator.wait_for_timeout(100)
        (tmp_path / "processing.json").write_text(json.dumps(state, indent=2))
        assert all(s["state"] == "succeeded" for s in state["steps"]), state
        with sqlite3.connect(database_path(settings.data_dir)) as db:
            assert db.execute(
                "SELECT kind, count(*) FROM tasks GROUP BY kind ORDER BY kind"
            ).fetchall() == [("notes", 1), ("transcription", 1)]
            assert db.execute("SELECT count(*) FROM transcript_generations").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM notes_revisions").fetchone()[0] == 1
        assert [
            item for item in requests if item[1].startswith("/api/extension/v1/")
        ] == extension_requests
        with context.expect_page() as opened:
            control.locator('[data-action="result"]').click()
        meeting = opened.value
        meeting.wait_for_url(session["upload"]["meetingUrl"])
        meeting.locator("#meeting-tab-transcript").click()
        expect(
            meeting.get_by_text("Synthetic meeting transcript.", exact=True).first
        ).to_be_visible()
        meeting.locator("#meeting-tab-notes").click()
        expect(meeting.locator("#notes-preview")).to_contain_text("Synthetic meeting notes.")
        expect(meeting.locator("#notes-preview")).to_be_visible()
        meeting.screenshot(path=str(tmp_path / "transcript-and-notes.png"))
        (tmp_path / "processing.json").write_text(json.dumps(state, indent=2))


def test_native_pairing_success_activates_device_before_any_recording(
    capture_server, paired_capture
):
    _settings, _url, requests, _tls = capture_server
    _context, _worker, operator, control, _tab = paired_capture
    devices = operator.evaluate(
        "async () => (await (await fetch('/api/extension/devices')).json()).devices"
    )
    assert len(devices) == 1
    assert devices[0]["status"] == "active", (
        "Extension reports connected but server still reports pending"
    )
    assert devices[0]["last_seen_at"] is not None
    assert not any(path == "/api/extension/v1/recording-sessions" for _method, path in requests)
    assert not _status(control)["sessions"]


@pytest.mark.parametrize("activation_status", [503])
def test_native_pairing_session_failure_does_not_report_connected(
    capture_server, paired_capture, activation_status
):
    _settings, _url, requests, _tls = capture_server
    _context, _worker, operator, control, _tab = paired_capture
    assert activation_status == 503
    expect(control.locator("#connection-dot")).not_to_have_class("status-dot is-ready")
    expect(control.locator("#pair-button")).to_be_enabled()
    assert not _status(control)["connection"].get("paired")
    devices = operator.evaluate(
        "async () => (await (await fetch('/api/extension/devices')).json()).devices"
    )
    assert devices[0]["status"] == "pending"
    assert devices[0]["last_seen_at"] is None
    assert ("POST", "/api/extension/v1/session") in requests


@pytest.mark.parametrize("activation_status,activation_after", [(503, 1)])
def test_native_repair_session_failure_clears_previous_connected_state(
    capture_server, paired_capture, activation_status, activation_after
):
    _settings, _url, _requests, _tls = capture_server
    _context, _worker, operator, control, _tab = paired_capture
    assert (activation_status, activation_after) == (503, 1)
    devices = operator.evaluate(
        "async () => (await (await fetch('/api/extension/devices')).json()).devices"
    )
    code = _pair_again_code(operator, devices[0]["id"])
    control.locator("#setup-toggle").click()
    control.locator("#pair-code").fill(code)
    control.locator("#pair-button").click()
    expect(control.locator("#global-error")).to_have_text(
        "The server is temporarily unavailable.", timeout=20000
    )
    expect(control.locator("#pair-button")).to_be_enabled()
    assert not _status(control)["connection"].get("paired")
    expect(control.locator("#connection-dot")).not_to_have_class("status-dot is-ready")


def test_release_opt_in_checks_only_github_and_disabling_clears_the_notice(paired_capture):
    # REST fixture follows https://docs.github.com/en/rest/releases/assets#list-release-assets.
    context, worker, _, _, _ = paired_capture
    # The real Chrome permission dialog and alarms use a disposable Xvfb profile.
    # Stub only the external release feed, inside its calling service worker.
    worker.evaluate("""() => {
        const original = self.fetch;
        self.releaseCalls = [];
        self.fetch = (resource, init) => {
            if (!String(resource).startsWith('https://api.github.com/')) {
                return original(resource, init);
            }
            self.releaseCalls.push({url:String(resource), credentials:init.credentials});
            const name = 'asr-studio-extension-v99.0.0.zip';
            const base = 'https://github.com/nguyencanhtrung/asr-studio/releases/download/v99.0.0/';
            const assets = ['', '.sha256', '.metadata.json'].map(suffix => ({
                name:name+suffix, browser_download_url:base+name+suffix, state:'uploaded', size:32
            }));
            return Promise.resolve(Response.json({tag_name:'v99.0.0',draft:false,
                prerelease:false,assets}));
        };
    }""")
    popup = context.new_page()
    popup.goto(f"chrome-extension://{EXTENSION_ID}/popup.html")
    toggle = popup.locator("#release-checks")
    expect(toggle).to_be_visible()
    expect(toggle).not_to_be_checked()
    expect(popup.locator("#release-links")).to_be_hidden()
    assert worker.evaluate("self.releaseCalls.length") == 0
    toggle.click()
    _approve_host_permission(popup, "https://api.github.com")
    expect(toggle).to_be_checked()
    expect(popup.locator("#release-zip")).to_have_text("ZIP v99.0.0")
    popup.wait_for_function("async () => await chrome.action.getBadgeText({}) === 'NEW'")
    assert worker.evaluate("self.releaseCalls") == [
        {
            "url": "https://api.github.com/repos/nguyencanhtrung/asr-studio/releases/latest",
            "credentials": "omit",
        }
    ]
    alarm_name = "asr-extension-production-v1:release-check"
    alarm = worker.evaluate("name => chrome.alarms.get(name)", alarm_name)
    assert alarm["periodInMinutes"] == 1440
    toggle.click()
    expect(toggle).not_to_be_checked()
    expect(popup.locator("#release-links")).to_be_hidden()
    popup.wait_for_function("async () => await chrome.action.getBadgeText({}) === ''")
    assert worker.evaluate("name => chrome.alarms.get(name)", alarm_name) is None
    toggle.click()
    expect(toggle).to_be_checked()
    expect(popup.locator("#release-links")).to_be_visible()
    # Removing host access in Chrome must revoke the preference and its daily alarm.
    assert popup.evaluate("chrome.permissions.remove({origins:['https://api.github.com/*']})")
    expect(toggle).not_to_be_checked()
    expect(popup.locator("#release-links")).to_be_hidden()
    popup.wait_for_function("async name => !(await chrome.alarms.get(name))", arg=alarm_name)


def test_capture_survives_release_permission_denial_and_failed_daily_check(paired_capture):
    context, worker, _, control, tab = paired_capture
    options = context.new_page()
    options.goto(f"chrome-extension://{EXTENSION_ID}/popup.html")
    toggle = options.locator("#release-checks")
    expect(toggle).to_be_visible()
    # Simulate the user's denial at the browser permission boundary.
    options.evaluate("chrome.permissions.request = async () => false")
    toggle.click()
    expect(options.locator("#release-permission")).to_be_visible()
    expect(toggle).not_to_be_checked()
    assert not _status(control)["checksEnabled"]
    options.reload()
    worker.evaluate("""() => {
        const original = self.fetch;
        self.failedReleaseChecks = 0;
        self.fetch = (url, init) => {
            if (String(url).startsWith('https://api.github.com/')) {
                self.failedReleaseChecks++;
                return Promise.reject(new TypeError('network unavailable'));
            }
            return original(url, init);
        };
    }""")
    toggle.click()
    _approve_host_permission(options, "https://api.github.com")
    expect(toggle).to_be_checked()
    assert worker.evaluate("self.failedReleaseChecks") == 1
    options.close()
    popup = NativePopup(context, tab)
    popup.click("#start-button")
    popup.wait_text("#capture-state", "Recording")
    assert popup.evaluate("!document.querySelector('#release-panel').hidden")
    assert popup.evaluate("document.querySelector('#release-links').hidden")
    tab.wait_for_timeout(2200)
    popup.click("#stop-button")
    popup.close()
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        sessions = _status(control)["sessions"]
        if sessions and sessions[0]["upload"]["cleaned"]:
            break
        control.wait_for_timeout(150)
    assert sessions[0]["upload"]["cleaned"]
    assert sessions[0]["upload"]["receipt"]["recording_id"]

    # Chrome may clear an alarm at startup. Remove the now-unused server permission
    # to invoke the same reconciliation function, leaving the GitHub grant in place.
    alarm = "asr-extension-production-v1:release-check"
    server = _status(control)["connection"]["serverUrl"]
    assert control.evaluate(
        """async ([name, server]) => {
        await chrome.alarms.clear(name);
        await chrome.storage.local.set({'asr-extension-production-v1:release-last-attempt': 1});
        return chrome.permissions.remove({origins: [server + '/*']});
    }""",
        [alarm, server],
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and worker.evaluate("self.failedReleaseChecks") < 2:
        control.wait_for_timeout(100)
    assert worker.evaluate("self.failedReleaseChecks") == 2
    assert worker.evaluate("name => chrome.alarms.get(name)", alarm)["periodInMinutes"] == 1440
