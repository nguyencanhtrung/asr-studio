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
from playwright.sync_api import expect, sync_playwright

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.extension_auth import EXTENSION_ID
from asr_studio.storage import database_path

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def _approve_host_permission():
    assert os.environ.get("ASR_EXTENSION_DISPOSABLE_XVFB") == "1"
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
    quota = 32768 if failure and failure.params.get("failure") == "quota" else 512 * 1024**2
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url=url,
        extension_max_staging_bytes=quota,
    )
    app = create_app(settings)
    requests = []

    @app.middleware("http")
    async def record_requests(request, call_next):
        requests.append((request.method, request.url.path))
        return await call_next(request)

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
def paired_capture(capture_server, capture_client, tmp_path):
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
            control.wait_for_timeout(500)
            _approve_host_permission()
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
    popup.screenshot(tmp_path / "popup-recording.png")
    popup.close()
    control.close()
    # No control page remains; the native offscreen owner continues persisting audio.
    tab.wait_for_timeout(3500)
    popup = NativePopup(context, tab)
    popup.wait_text("#capture-state", "Recording")
    popup.click("#stop-button")
    popup.close()
    control = context.new_page()
    control.goto(f"chrome-extension://{EXTENSION_ID}/control.html")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = _status(control)
        if status["sessions"] and status["sessions"][0]["upload"]["cleaned"]:
            break
        control.wait_for_timeout(150)
    assert status["sessions"][0]["upload"]["cleaned"], status
    (tmp_path / "status.json").write_text(json.dumps(status, indent=2))
    assert len(status["sessions"]) == 1
    session = status["sessions"][0]
    assert session["capture"]["captureComplete"]
    assert session["upload"]["receipt"]["recording_id"] == session["summary"]["recordingId"]
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
    control.once("dialog", lambda dialog: dialog.accept())
    control.locator('[data-action="discard"]').first.click()
    expect(control.locator(".session-row")).to_have_count(1)
    retained = _status(control)["sessions"]
    assert len(retained) == 1 and retained[0]["id"] == session["id"]


@pytest.mark.parametrize("failure", ["permission", "quota"])
def test_native_failures_retain_audio(capture_server, paired_capture, failure):
    _settings, url, requests, _tls = capture_server
    context, _worker, _operator, control, tab = paired_capture
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
        assert not any("/recording-sessions" in path for _method, path in requests)
    else:
        assert session["capture"]["captureComplete"] and session["upload"]["error"]
        assert session["upload"]["error"]["code"] == "host_permission"
        assert not any("/recording-sessions" in path for _method, path in requests)
        retry = control.locator('[data-action="retry"]')
        retry.focus()
        control.keyboard.press("Enter")
        control.wait_for_timeout(500)
        _approve_host_permission()
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
