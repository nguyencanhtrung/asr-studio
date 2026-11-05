from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from notes_helpers import StubNotesProvider, seed_completed_transcript
from playwright.sync_api import expect, sync_playwright
from seeding import prepare_diarization_model, seed_meeting

from asr_studio import routes as routes_module
from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings
from asr_studio.notes import accept_notes_task
from asr_studio.runtime import RuntimeProfile, write_runtime_profile
from asr_studio.storage import database_path
from asr_studio.tasks import (
    accept_transcription_task,
    claim_next_task,
    publish_complete_generation,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser

# Far ahead of UTC so any date arithmetic done in local time would drift a day.
TIMEZONE = "Pacific/Kiritimati"
# Keep the UTC offset explicit (+14:00 is Kiritimati's, i.e. 2026-10-17 10:00 local):
# Chromium versions parse a timezone-naive string differently (UTC vs context-local),
# which shifted "today" by a day on newer Chrome.
FIXED_NOW = "2026-10-17T10:00:00+14:00"


class LiveServer:
    def __init__(self, tmp_path, *, upload_chunk_bytes=None):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.url = f"http://127.0.0.1:{self.port}"
        settings = {
            "data_dir": tmp_path / "data",
            "auth_dir": tmp_path / "auth",
            "public_url": self.url,
        }
        if upload_chunk_bytes is not None:
            settings["upload_chunk_bytes"] = upload_chunk_bytes
        self.settings = Settings(**settings)
        self.server = None
        self.thread = None

    def start(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen()
        self.sock = sock
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.settings), log_config=None, log_level="warning", access_log=False
            )
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.025)
        assert self.server.started, "test server did not start"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.sock.close()

    @property
    def access_link(self):
        return f"{self.url}/access#{AuthStore(self.settings.auth_dir).load().token}"


@pytest.fixture
def live(tmp_path, request):
    if os.environ.get("ASR_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set ASR_RUN_BROWSER_TESTS=1 to run the real-browser history tests")
    marker = request.node.get_closest_marker("upload_chunk_bytes")
    chunk_bytes = marker.args[0] if marker and marker.args else None
    server = LiveServer(tmp_path, upload_chunk_bytes=chunk_bytes)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def signed_in(live, request):
    """Factory for authenticated (context, page) pairs in a chosen timezone and clock.

    Headless Chromium hides scrollbars; parametrize this indirectly with True to draw them as a
    desktop browser does.  Set ASR_BROWSER_ENGINE to select Firefox for browser checks.
    """
    with sync_playwright() as playwright:
        engine = os.environ.get("ASR_BROWSER_ENGINE", "chromium").lower()
        if engine == "firefox":
            browser_type = playwright.firefox
            firefox_path = os.environ.get("ASR_FIREFOX_EXECUTABLE")
            if not firefox_path:
                pytest.fail("ASR_FIREFOX_EXECUTABLE is required for the Firefox browser gate")
            launch = {"executable_path": firefox_path}
        elif engine == "chromium":
            browser_type = playwright.chromium
            chrome_path = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")
            launch = {"executable_path": chrome_path} if chrome_path else {}
        else:
            pytest.fail(f"unsupported ASR_BROWSER_ENGINE: {engine}")
        if getattr(request, "param", False) and engine == "chromium":
            launch["ignore_default_args"] = ["--hide-scrollbars"]
        browser = browser_type.launch(headless=True, **launch)

        def make(
            timezone=TIMEZONE,
            now=FIXED_NOW,
            *,
            color_scheme="light",
            viewport=None,
            reduced_motion="no-preference",
        ):
            context = browser.new_context(
                timezone_id=timezone,
                color_scheme=color_scheme,
                viewport=viewport,
                reduced_motion=reduced_motion,
            )
            first = context.new_page()
            first.clock.set_fixed_time(now)
            first.goto(live.access_link, wait_until="domcontentloaded")
            first.wait_for_url(f"{live.url}/", timeout=10_000)
            return context, first

        yield make
        browser.close()


@pytest.fixture
def browser_context(signed_in):
    return signed_in()


def open_page(context, live, path="/", now=FIXED_NOW):
    page = context.new_page()
    page.clock.set_fixed_time(now)
    page.goto(f"{live.url}{path}", wait_until="domcontentloaded")
    return page


def open_calendar(page):
    """Open the calendar view as a user does: click the selected day of the sidebar calendar."""
    page.locator("#mini-calendar .cal-day.is-selected").click()


def edit_segment(page, index=0):
    """Press a segment's Sửa button, as a user does, and return the text editor it opens."""
    row = page.locator(".transcript-segment").nth(index)
    row.locator(".segment-edit-button").click()
    return row.locator(".segment-text-editor")


def rename_speaker(page, index=0):
    """Press a speaker chip, as a user does, and return the name input that opens in its place."""
    item = page.locator("#speaker-catalog-list > li").nth(index)
    item.locator(".speaker-chip").click()
    return item.locator("input")


def _prepare_browser_asr_model(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(b"model")
    (path / "config.json").write_text(
        json.dumps({"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []}),
        encoding="utf-8",
    )
    (path / "tokenizer.json").write_text(json.dumps({"model": {"type": "BPE"}}), encoding="utf-8")
    (path / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "feature_size": 128,
                "sampling_rate": 16000,
                "hop_length": 160,
                "n_fft": 400,
                "chunk_length": 30,
            }
        ),
        encoding="utf-8",
    )
    return path


def _prepare_browser_qwen_model(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type": "qwen3_asr"}', encoding="utf-8")
    (path / "processor_config.json").write_text(
        '{"feature_extractor": {"sampling_rate": 16000}}', encoding="utf-8"
    )
    for filename in (
        "generation_config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
    ):
        (path / filename).write_bytes(b"model")
    return path


def _publish_browser_generation(live, meeting_id, *, segments=None, speakers=None):
    model_path = _prepare_browser_asr_model(live.settings.data_dir.parent / "asr-model")
    diarization_path = prepare_diarization_model(
        live.settings.data_dir.parent / "diarization-model"
    )
    owner_id = f"browser-seed-{uuid.uuid4().hex}"
    task = accept_transcription_task(
        live.settings.data_dir,
        meeting_id=meeting_id,
        request_id=owner_id,
        model_path=model_path,
        diarization_model_path=diarization_path,
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    claimed = claim_next_task(live.settings.data_dir, owner_id=owner_id)
    assert claimed and claimed["id"] == task["id"]
    assert update_task_progress(
        live.settings.data_dir,
        task["id"],
        owner_id=owner_id,
        stage="publishing",
        elapsed_seconds=1,
        transcript_end_seconds=2,
        segment_count=1,
    )
    generation = publish_complete_generation(
        live.settings.data_dir,
        task["id"],
        owner_id=owner_id,
        segments=segments
        or [
            {
                "id": "a" * 32,
                "start_seconds": 1,
                "end_seconds": 2,
                "text": "A saved sentence.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
            }
        ],
        speakers=speakers
        or [
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1,
                "last_seen_seconds": 2,
            }
        ],
        language_probability=0.9,
        elapsed_seconds=1,
    )
    assert generation is not None
    return generation


def _design_metrics(page):
    return page.evaluate(
        """() => ({
          theme: document.documentElement.dataset.theme,
          preference: document.documentElement.dataset.themePreference,
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          bodyFont: getComputedStyle(document.body).fontFamily,
          headingFont: getComputedStyle(document.querySelector("h1, h2")).fontFamily,
          monoFont: getComputedStyle(document.querySelector(".brand-wordmark")).fontFamily,
          storedTheme: localStorage.getItem("asr-studio:theme"),
        })"""
    )


def _semantic_contrast_ratios(page):
    return page.evaluate(
        """() => {
          const styles = getComputedStyle(document.documentElement);
          const channels = value => {
            const hex = styles.getPropertyValue(value).trim().slice(1);
            return [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255);
          };
          const luminance = value => {
            const linear = channels(value).map(channel => channel <= 0.04045
              ? channel / 12.92
              : ((channel + 0.055) / 1.055) ** 2.4);
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
          };
          const ratio = (foreground, background) => {
            const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
            return (values[0] + 0.05) / (values[1] + 0.05);
          };
          return {
            success: ratio("--success", "--success-soft"),
            warning: ratio("--warning", "--warning-soft"),
            danger: ratio("--danger", "--danger-soft"),
            muted: ratio("--ink-3", "--bg"),
          };
        }"""
    )


def test_design_system_theme_fonts_focus_and_offline_assets(live, signed_in, tmp_path):
    context, page = signed_in(
        color_scheme="dark",
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
    )
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{live.url}/access", wait_until="networkidle")
    page.evaluate("document.fonts.ready")

    metrics = _design_metrics(page)
    assert metrics == {
        "theme": "dark",
        "preference": "system",
        "overflow": 0,
        "bodyFont": '"Inter Variable", system-ui, -apple-system, sans-serif',
        "headingFont": '"Newsreader Variable", Georgia, "Times New Roman", serif',
        "monoFont": '"JetBrains Mono", ui-monospace, SFMono-Regular, monospace',
        "storedTheme": None,
    }
    assert page.evaluate(
        """() => [
          document.fonts.check('16px "Inter Variable"', "Điều phối cuộc họp"),
          document.fonts.check('32px "Newsreader Variable"', "Mở không gian làm việc"),
          document.fonts.check('12px "JetBrains Mono"', "Tiếng Việt EN VI"),
        ].every(Boolean)"""
    )
    assert min(_semantic_contrast_ratios(page).values()) >= 4.5

    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Switch to English")).to_be_focused()
    page.keyboard.press("Tab")
    theme_toggle = page.get_by_role("button", name="Chuyển sang giao diện sáng")
    expect(theme_toggle).to_be_focused()
    expect(page.locator(".access-header .theme-icon-sun")).to_be_visible()
    expect(page.locator(".access-header .theme-icon-moon")).to_be_hidden()
    focus_style = theme_toggle.evaluate(
        "element => ({ width: getComputedStyle(element).outlineWidth, "
        "style: getComputedStyle(element).outlineStyle })"
    )
    assert focus_style["style"] == "solid"
    assert float(focus_style["width"].removesuffix("px")) >= 2
    motion_duration = page.locator(".access-orbit path").first.evaluate(
        "element => parseFloat(getComputedStyle(element).animationDuration)"
    )
    assert motion_duration <= 0.001

    transition_sample = page.evaluate(
        """() => {
          document.querySelector(".access-header [data-theme-toggle]").click();
          const buttonStyle = getComputedStyle(document.querySelector("#access-submit"));
          return {
            theme: document.documentElement.dataset.theme,
            changing: document.documentElement.hasAttribute("data-theme-changing"),
            background: buttonStyle.backgroundColor,
            color: buttonStyle.color,
            transitionDuration: buttonStyle.transitionDuration,
            toggleLabel: document.querySelector("[data-theme-toggle]").getAttribute("aria-label"),
            moonVisible: getComputedStyle(document.querySelector(".theme-icon-moon")).display,
            sunVisible: getComputedStyle(document.querySelector(".theme-icon-sun")).display,
          };
        }"""
    )
    assert transition_sample == {
        "theme": "light",
        "changing": True,
        "background": "rgb(142, 36, 51)",
        "color": "rgb(255, 250, 250)",
        "transitionDuration": "0s",
        "toggleLabel": "Chuyển sang giao diện tối",
        "moonVisible": "block",
        "sunVisible": "none",
    }
    assert _design_metrics(page)["storedTheme"] == "light"
    page.reload(wait_until="domcontentloaded")
    assert _design_metrics(page)["theme"] == "light"
    assert min(_semantic_contrast_ratios(page).values()) >= 4.5

    page.evaluate("localStorage.removeItem('asr-studio:theme')")
    assert _design_metrics(page)["storedTheme"] is None
    page.emulate_media(color_scheme="light", reduced_motion="reduce")
    page.reload(wait_until="domcontentloaded")
    assert _design_metrics(page)["theme"] == "light"
    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
    page.reload(wait_until="domcontentloaded")
    assert _design_metrics(page)["theme"] == "dark"

    snapshot = tmp_path / "access-dark.png"
    page.screenshot(path=snapshot, full_page=True, animations="disabled")
    assert snapshot.stat().st_size > 10_000
    page.set_viewport_size({"width": 390, "height": 844})
    assert _design_metrics(page)["overflow"] == 0

    assert requests
    assert all(url.startswith(f"{live.url}/") for url in requests)
    context.close()


def test_design_system_history_and_transcript_are_responsive(live, signed_in, tmp_path):
    meeting_id = seed_meeting(
        live.settings.data_dir,
        "Điều phối sản phẩm — tuần 42",
        "2026-10-14",
        duration_seconds=720,
    )
    seed_meeting(live.settings.data_dir, "English roadmap", "2026-10-14")
    seed_meeting(live.settings.data_dir, "Cuộc họp chưa đặt ngày")
    _publish_browser_generation(live, meeting_id)

    context, page = signed_in(
        color_scheme="light",
        viewport={"width": 1440, "height": 1000},
    )
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{live.url}/", wait_until="networkidle")
    expect(page.locator("#recent-list a")).to_have_count(2)
    assert _design_metrics(page)["overflow"] == 0

    open_calendar(page)
    expect(page.locator("#large-calendar")).to_be_visible()
    expect(
        page.locator("#large-calendar").get_by_text("Điều phối sản phẩm — tuần 42", exact=True)
    ).to_be_visible()
    assert _design_metrics(page)["overflow"] == 0
    history_snapshot = tmp_path / "history-light.png"
    page.screenshot(path=history_snapshot, full_page=True, animations="disabled")

    page.locator(".sidebar-masthead [data-theme-toggle]").click()
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="networkidle")
    expect(page.locator("#transcript-generation")).to_be_visible()
    expect(page.locator(".segment-text")).to_have_text("A saved sentence.")
    runtime_font = page.locator("#runtime-profile dd").first.evaluate(
        "element => getComputedStyle(element).fontFamily"
    )
    assert runtime_font.startswith('"JetBrains Mono"')
    assert _design_metrics(page)["overflow"] == 0
    meeting_snapshot = tmp_path / "meeting-dark.png"
    page.screenshot(path=meeting_snapshot, full_page=True, animations="disabled")

    page.set_viewport_size({"width": 390, "height": 844})
    assert _design_metrics(page)["overflow"] == 0
    expect(page.locator(".sidebar-masthead [data-theme-toggle]")).to_be_visible()
    mobile_snapshot = tmp_path / "meeting-dark-mobile.png"
    page.screenshot(path=mobile_snapshot, full_page=True, animations="disabled")

    for snapshot in (history_snapshot, meeting_snapshot, mobile_snapshot):
        assert snapshot.stat().st_size > 10_000
    assert requests
    assert all(url.startswith(f"{live.url}/") for url in requests)
    context.close()


def test_history_search_calendar_navigation_and_return(live, browser_context):
    context, page = browser_context
    data_dir = live.settings.data_dir
    seed_meeting(data_dir, "Delta year end", "2025-12-31")
    seed_meeting(data_dir, "Epsilon new year", "2026-01-01")
    seed_meeting(data_dir, "Gamma planning", "2026-10-09")
    alpha = seed_meeting(data_dir, "Alpha review", "2026-10-14", duration_seconds=720)
    beta = seed_meeting(data_dir, "Beta sync", "2026-10-14", duration_seconds=5025)
    seed_meeting(data_dir, "Zeta undated")
    seed_meeting(data_dir, "Leap day", "2024-02-29")
    page.goto(f"{live.url}/", wait_until="domcontentloaded")

    expect(page.locator(".sidebar-label, .sidebar-create, #workspace-link")).to_have_count(0)
    header_create = page.locator(".header-create")
    expect(header_create).to_be_visible()
    expect(header_create).to_have_accessible_name("Cuộc họp mới")
    for width in (681, 680, 521, 520, 390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        expect(header_create).to_be_visible()
        expect(header_create).to_have_accessible_name("Cuộc họp mới")
        header_metrics = page.evaluate(
            """() => {
              const rect = element => {
                const box = element.getBoundingClientRect();
                return { width: box.width, height: box.height };
              };
              const create = document.querySelector('.header-create');
              return {
                gap: getComputedStyle(document.querySelector('.header-actions')).gap,
                calendar: rect(document.querySelector('#open-calendar')),
                create: rect(create),
                createLabelClip: getComputedStyle(create.querySelector('span')).clipPath,
                overflow: (
                    document.documentElement.scrollWidth
                    > document.documentElement.clientWidth
                ),
              };
            }"""
        )
        # Above 680px the sidebar calendar is the way in, so the header button is hidden.
        assert header_metrics["calendar"]["height"] == (40 if width <= 680 else 0)
        assert header_metrics["create"]["height"] == 40
        assert not header_metrics["overflow"]
        if width <= 680:
            assert header_metrics["gap"] == "8px"
        if width <= 520:
            assert header_metrics["create"]["width"] == 40
            assert header_metrics["createLabelClip"] == "inset(50%)"
        else:
            assert header_metrics["createLabelClip"] == "none"
        if width == 681:
            assert header_metrics["gap"] == "11px"
    for _ in range(10):
        if header_create.evaluate("element => element === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(header_create).to_be_focused()
    focus_style = header_create.evaluate(
        "element => ({visible: element.matches(':focus-visible'), "
        "outlineStyle: getComputedStyle(element).outlineStyle, "
        "outlineWidth: getComputedStyle(element).outlineWidth})"
    )
    assert focus_style["visible"] and focus_style["outlineStyle"] == "solid"
    assert float(focus_style["outlineWidth"].removesuffix("px")) >= 2
    page.set_viewport_size({"width": 1280, "height": 720})

    recent = page.locator("#recent-list a")
    expect(recent).to_have_count(3)
    assert [t.strip() for t in page.locator("#recent-list .link-title").all_inner_texts()] == [
        "Beta sync",
        "Alpha review",
        "Gamma planning",
    ]
    # Date and recording length only; there is no start time to show.
    assert page.locator("#recent-list .link-date").all_inner_texts() == [
        "14/10/2026 · 1 giờ 24 phút",
        "14/10/2026 · 12 phút",
        "09/10/2026 · 1 giây",
    ]

    search = page.get_by_label("Tìm cuộc họp theo tên")
    search.fill("LEAP")
    expect(recent).to_have_count(1)
    expect(recent.first).to_contain_text("Leap day")
    expect(recent.first.locator(".link-date")).to_have_text("29/02/2024 · 1 giây")
    search.fill("không tồn tại")
    expect(page.locator("#search-empty")).to_be_visible()
    expect(page.locator("#sidebar-empty")).to_be_hidden()
    search.fill("")
    expect(recent).to_have_count(3)

    # Open from the recent list, then browse the calendar.
    recent.filter(has_text="Alpha review").click()
    page.wait_for_url(f"{live.url}/meetings/{alpha}")
    expect(page.locator("#meeting-title")).to_have_text("Alpha review")
    expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-14")
    open_calendar(page)
    large = page.locator("#large-calendar")
    expect(page.locator("#calendar-title")).to_be_visible()
    expect(large.locator(".cal-title")).to_have_text("Tháng 10 · 2026")
    expect(page.locator("#mini-calendar .cal-title")).to_have_text("Tháng 10 · 2026")

    # Monday-first: 1 Oct 2026 is a Thursday, so the grid starts on 28 Sep.
    expect(large.locator(".cal-weekdays span").first).to_have_text("T2")
    expect(large.locator(".cal-weekdays span").last).to_have_text("CN")
    expect(large.locator(".cal-day").first).to_have_attribute("data-date", "2026-09-28")
    expect(large.locator(".cal-day.is-today")).to_have_attribute("data-date", "2026-10-17")
    expect(large.locator('.cal-day[data-date="2026-10-14"] .cal-count')).to_have_text("2")
    expect(page.locator('#mini-calendar .cal-day[data-date="2026-10-14"] .cal-count')).to_have_text(
        "2"
    )
    expect(large.locator(".cal-summary")).to_have_text("3 cuộc họp trong tháng")
    expect(large.locator(".cal-undated-count")).to_have_text("1")

    large.locator('.cal-day[data-date="2026-10-14"]').click()
    expect(page.locator("#day-panel-title")).to_have_text("14/10/2026")
    expect(page.locator("#day-panel-count")).to_have_text("2")
    expect(page.locator("#day-panel-list a")).to_have_count(2)
    assert not any(":" in text for text in page.locator("#day-panel-list a").all_inner_texts()), (
        "date-only meetings must not show a start time"
    )

    large.locator(".cal-undated").click()
    expect(page.locator("#day-panel-title")).to_have_text("Chưa đặt ngày")
    expect(page.locator("#day-panel-list a")).to_have_text(["Zeta undatedChưa đặt ngày · 1 giây"])

    large.get_by_role("button", name="Hôm nay").click()
    expect(page.locator("#day-panel-title")).to_have_text("17/10/2026")
    expect(page.locator("#day-panel-empty")).to_be_visible()

    # Month/year navigation across the year boundary and a leap year.
    large.locator(".cal-month").select_option("11")
    large.locator(".cal-year").fill("2025")
    large.locator(".cal-year").press("Enter")
    expect(large.locator(".cal-title")).to_have_text("Tháng 12 · 2025")
    expect(page.locator("#mini-calendar .cal-title")).to_have_text("Tháng 12 · 2025")
    expect(large.locator('.cal-day[data-date="2025-12-31"]')).not_to_have_class("is-outside")
    large.get_by_label("Tháng sau").click()
    expect(large.locator(".cal-title")).to_have_text("Tháng 1 · 2026")
    expect(large.locator(".cal-day").first).to_have_attribute("data-date", "2025-12-29")
    expect(large.locator('.cal-day[data-date="2026-01-01"] .cal-count')).to_have_text("1")
    large.get_by_label("Tháng trước").click()
    large.get_by_label("Tháng trước").click()
    expect(large.locator(".cal-title")).to_have_text("Tháng 11 · 2025")
    expect(large.locator(".cal-summary")).to_have_text("Không có cuộc họp trong tháng này.")
    large.locator(".cal-month").select_option("1")
    large.locator(".cal-year").fill("2024")
    large.locator(".cal-year").press("Enter")
    expect(large.locator('.cal-day[data-date="2024-02-29"]')).not_to_have_class("is-outside")
    expect(large.locator('.cal-day[data-date="2024-02-29"] .cal-count')).to_have_text("1")
    large.locator(".cal-year").fill("2026")
    large.locator(".cal-year").press("Enter")
    expect(large.locator('.cal-day[data-date="2026-03-01"]')).to_have_class("cal-day is-outside")
    expect(large.locator(".cal-day[data-date='2026-02-29']")).to_have_count(0)

    # Keyboard: arrows move the selected day and keep focus on it.
    large.locator(".cal-month").select_option("9")
    large.locator('.cal-day[data-date="2026-10-14"]').click()
    large.locator('.cal-day[data-date="2026-10-14"]').focus()
    page.keyboard.press("ArrowDown")
    expect(large.locator(".cal-day.is-selected")).to_have_attribute("data-date", "2026-10-21")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-10-21"
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-10-20"
    page.keyboard.press("Enter")
    expect(page.locator("#day-panel-title")).to_have_text("20/10/2026")

    # Browsing without choosing a meeting returns to the one open before the calendar.
    page.get_by_role("button", name="Quay lại notes").click()
    page.wait_for_url(f"{live.url}/meetings/{alpha}")
    expect(page.locator("#meeting-title")).to_have_text("Alpha review")
    expect(page.locator("#recent-list a[aria-current='page']")).to_contain_text("Alpha review")

    # Choosing a different meeting from a calendar cell opens exactly that record.
    open_calendar(page)
    large.locator('.cal-day[data-date="2026-10-14"]').click()
    page.locator("#day-panel-list a").filter(has_text="Beta sync").click()
    page.wait_for_url(f"{live.url}/meetings/{beta}")
    expect(page.locator("#meeting-title")).to_have_text("Beta sync")
    page.go_back()
    expect(page.locator("#meeting-title")).to_have_text("Alpha review")
    open_calendar(page)
    large.locator('.cal-day[data-date="2026-10-14"]').click()
    large.locator(".cal-chip").filter(has_text="Alpha review").click()
    page.wait_for_url(f"{live.url}/meetings/{alpha}")

    page.goto(f"{live.url}/meetings/{alpha}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Alpha review")
    page.get_by_role("link", name="ASR Studio — về workspace").click()
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#home-view")).to_be_visible()

    # An unauthenticated context cannot read the history.
    anonymous = page.context.browser.new_context()
    try:
        assert anonymous.request.get(f"{live.url}/api/meetings").status == 401
    finally:
        anonymous.close()


def _confirm_meeting_deletion(page, meeting_title):
    page.locator("#meeting-actions").click()
    page.locator("#delete-meeting").click()
    dialog = page.locator("#meeting-delete-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("File nguồn bên ngoài không bị thay đổi")
    expect(dialog).to_contain_text("Hủy chép lời")
    expect(dialog).to_contain_text("Hủy tạo notes")
    expect(dialog).to_contain_text("chờ tác vụ kết thúc")
    expect(dialog.locator("#meeting-delete-cancel")).to_have_text("Đóng")
    expect(dialog.locator("#meeting-delete-help")).to_contain_text(
        "Đóng hộp thoại sau khi xác nhận không hủy yêu cầu xóa"
    )
    expect(dialog.locator("#meeting-delete-help")).to_contain_text("Nếu dọn dẹp bị gián đoạn")
    expect(dialog.locator("#meeting-delete-help")).to_contain_text(
        "có thể thử lại sau khi khắc phục lỗi lưu trữ"
    )
    confirm = page.locator("#meeting-delete-confirm")
    expect(confirm).to_be_disabled()
    page.locator("#meeting-delete-confirmation").fill("wrong name")
    expect(confirm).to_be_disabled()
    page.locator("#meeting-delete-confirmation").fill(meeting_title)
    expect(confirm).to_be_enabled()
    confirm.click()


def test_meeting_deletion_refreshes_recent_search_calendar_and_undated(live, browser_context):
    dated = seed_meeting(live.settings.data_dir, "Dated deletion target", "2026-10-14")
    undated = seed_meeting(live.settings.data_dir, "Undated item to remove")
    seed_meeting(live.settings.data_dir, "Keep this calendar meeting", "2026-10-14")
    context, page = browser_context
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    expect(page.locator("#recent-list a")).to_have_count(2)

    page.locator("#recent-list a").filter(has_text="Dated deletion target").click()
    expect(page.locator("#meeting-title")).to_have_text("Dated deletion target")
    _confirm_meeting_deletion(page, "Dated deletion target")
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#recent-list a")).to_have_count(1)
    expect(page.locator("#recent-list")).not_to_contain_text("Dated deletion target")
    search = page.get_by_label("Tìm cuộc họp theo tên")
    search.fill("Dated deletion target")
    expect(page.locator("#search-empty")).to_be_visible()
    search.fill("")
    open_calendar(page)
    expect(
        page.locator('#large-calendar .cal-day[data-date="2026-10-14"] .cal-count')
    ).to_have_text("1")
    expect(page.locator("#large-calendar .cal-undated-count")).to_have_text("1")
    page.get_by_role("button", name="Quay lại notes").click()

    search.fill("Undated item to remove")
    page.locator("#recent-list a").filter(has_text="Undated item to remove").click()
    expect(page.locator("#meeting-title")).to_have_text("Undated item to remove")
    _confirm_meeting_deletion(page, "Undated item to remove")
    page.wait_for_url(f"{live.url}/")
    search.fill("")
    expect(page.locator("#recent-list a")).to_have_count(1)
    expect(page.locator("#recent-list")).not_to_contain_text("Undated item to remove")
    expect(page.locator("#recent-list")).to_contain_text("Keep this calendar meeting")
    open_calendar(page)
    expect(page.locator("#large-calendar .cal-undated-count")).to_have_text("0")
    page.locator("#large-calendar .cal-undated").click()
    expect(page.locator("#day-panel-list a")).to_have_count(0)
    expect(page.locator("#day-panel-empty")).to_have_text("Mọi cuộc họp đều đã có ngày.")
    assert context.request.get(f"{live.url}/api/meetings/{dated}").status == 404
    assert context.request.get(f"{live.url}/api/meetings/{undated}").status == 404


@pytest.mark.parametrize("selected_after_other_delete", ["target", "other"])
def test_concurrent_meeting_delete_reconciles_only_canonical_missing_response(
    live, browser_context, selected_after_other_delete
):
    target_title = "Meeting A deleted from another client"
    other_title = "Meeting B retained during deletion"
    target_id = seed_meeting(live.settings.data_dir, target_title, "2026-10-14")
    other_id = seed_meeting(live.settings.data_dir, other_title, "2026-10-14")
    context, page = browser_context
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    expect(page.locator("#recent-list a")).to_have_count(2)
    expect(page.locator('#mini-calendar .cal-day[data-date="2026-10-14"] .cal-count')).to_have_text(
        "2"
    )

    page.locator("#recent-list a").filter(has_text=target_title).click()
    expect(page.locator("#meeting-title")).to_have_text(target_title)
    page.locator("#meeting-actions").click()
    page.locator("#delete-meeting").click()
    page.locator("#meeting-delete-confirmation").fill(target_title)
    held = hold_first(page, rf"/api/meetings/{target_id}$", "DELETE")
    page.locator("#meeting-delete-confirm").click()

    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert held, "DELETE request did not reach the hold point"
    expect(page.locator("#meeting-deletion-status")).to_be_visible()
    expect(page.locator("#meeting-deletion-status")).to_contain_text("Đang dọn dữ liệu…")
    expect(page.locator("#meeting-deletion-status")).not_to_contain_text("Dọn dẹp chưa hoàn tất")

    audio_before = None
    if selected_after_other_delete == "other":
        page.locator("#meeting-delete-cancel").click()
        page.locator("#recent-list a").filter(has_text=other_title).click()
        page.wait_for_url(f"{live.url}/meetings/{other_id}")
        expect(page.locator("#meeting-title")).to_have_text(other_title)
        expect(page.locator("#recent-list a[aria-current='page']")).to_contain_text(other_title)
        audio_before = page.locator("#meeting-audio").evaluate(
            "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src, "
            "currentTime: Number.isFinite(audio.currentTime) ? audio.currentTime : null, "
            "paused: audio.paused })"
        )
        assert audio_before["meetingId"] == other_id

    other_delete = context.request.delete(
        f"{live.url}/api/meetings/{target_id}", headers={"Origin": live.url}
    )
    assert other_delete.status == 200
    assert other_delete.json()["status"] == "deleted"
    assert context.request.get(f"{live.url}/api/meetings/{target_id}").status == 404
    expect(page.locator("#recent-list a")).to_have_count(2)

    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE"
            and response.url.endswith(f"/api/meetings/{target_id}")
        )
    ) as delayed_response:
        held[0].continue_()
    response = delayed_response.value
    assert response.status == 404
    assert response.json()["detail"]["code"] == "meeting_not_found"

    if selected_after_other_delete == "target":
        page.wait_for_url(f"{live.url}/")
        expect(page.locator("#home-view")).to_be_visible()
    else:
        expect(page.locator("#meeting-title")).to_have_text(other_title)
        expect(page.locator("#recent-list a[aria-current='page']")).to_contain_text(other_title)
        audio_after = page.locator("#meeting-audio").evaluate(
            "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src, "
            "currentTime: Number.isFinite(audio.currentTime) ? audio.currentTime : null, "
            "paused: audio.paused })"
        )
        assert audio_after == audio_before

    expect(page.locator("#recent-list a")).to_have_count(1)
    expect(page.locator("#recent-list")).not_to_contain_text(target_title)
    expect(page.locator("#recent-list")).to_contain_text(other_title)
    expect(page.locator('#mini-calendar .cal-day[data-date="2026-10-14"] .cal-count')).to_have_text(
        "1"
    )
    assert context.request.get(f"{live.url}/api/meetings/{other_id}").status == 200


def test_delete_ui_keeps_unrelated_404_visible(live, browser_context):
    meeting_id = seed_meeting(live.settings.data_dir, "Unrelated 404 deletion target", "2026-10-14")
    context, page = browser_context
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    def return_unrelated_404(route):
        if route.request.method == "DELETE":
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "another_resource_not_found",
                            "message": "The requested deletion target was not found.",
                        }
                    }
                ),
            )
        else:
            route.fallback()

    page.route(re.compile(rf"/api/meetings/{meeting_id}$"), return_unrelated_404)
    _confirm_meeting_deletion(page, "Unrelated 404 deletion target")

    expect(page.locator("#meeting-delete-error")).to_be_visible()
    expect(page.locator("#meeting-delete-error")).to_have_text("Không tìm thấy dữ liệu yêu cầu.")
    expect(page.locator("#meeting-title")).to_have_text("Unrelated 404 deletion target")
    expect(page.locator("#recent-list a")).to_have_count(1)
    assert context.request.get(f"{live.url}/api/meetings/{meeting_id}").status == 200


def test_delete_ui_guides_active_task_cancel_and_wait(live, browser_context, tmp_path):
    from asr_studio.tasks import accept_transcription_task

    meeting_id = seed_meeting(live.settings.data_dir, "Active deletion target", "2026-10-14")
    model_path = _prepare_browser_asr_model(tmp_path / "whisper-model")
    diarization_path = prepare_diarization_model(tmp_path / "diarization-model")
    accept_transcription_task(
        live.settings.data_dir,
        meeting_id=meeting_id,
        request_id="browser-delete-active-task",
        model_path=model_path,
        diarization_model_path=diarization_path,
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    context, page = browser_context
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Active deletion target")

    _confirm_meeting_deletion(page, "Active deletion target")

    expect(page.locator("#meeting-delete-error")).to_contain_text("chờ trạng thái kết thúc")
    expect(page.locator("#meeting-deletion-status")).to_be_visible()
    expect(page.locator("#meeting-deletion-status")).to_contain_text("chờ trạng thái kết thúc")
    expect(page.locator("#meeting-deletion-status")).not_to_contain_text("khởi động")
    expect(page.locator("#delete-meeting")).to_have_text("Xóa cuộc họp")
    expect(page.locator("#meeting-delete-confirm")).to_have_text("Xóa dữ liệu đã lưu")
    assert context.request.get(f"{live.url}/api/meetings/{meeting_id}").status == 200


@pytest.mark.parametrize(
    ("status_code", "error_code", "error_message", "expected_message"),
    [
        (
            409,
            "meeting_task_active",
            "A task is still running.",
            "chờ trạng thái kết thúc",
        ),
        (
            503,
            "storage_unavailable",
            "Storage service is temporarily unavailable.",
            "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.",
        ),
    ],
)
def test_delete_failure_after_dialog_close_is_persisted_for_origin_meeting(
    live,
    browser_context,
    status_code,
    error_code,
    error_message,
    expected_message,
):
    target_title = f"Delayed deletion failure {error_code}"
    other_title = f"Unrelated meeting during {error_code}"
    target_id = seed_meeting(live.settings.data_dir, target_title, "2026-10-14")
    other_id = seed_meeting(live.settings.data_dir, other_title, "2026-10-14")
    _context, page = browser_context
    page.goto(f"{live.url}/meetings/{target_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text(target_title)

    held = hold_first(page, rf"/api/meetings/{target_id}$", "DELETE")
    _confirm_meeting_deletion(page, target_title)
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert held, "DELETE request did not reach the hold point"

    page.locator("#meeting-delete-cancel").click()
    expect(page.locator("#meeting-delete-dialog")).to_be_hidden()
    page.locator("#recent-list a").filter(has_text=other_title).click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text(other_title)
    audio_before = page.locator("#meeting-audio").evaluate(
        "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src })"
    )
    assert audio_before["meetingId"] == other_id

    held[0].fulfill(
        status=status_code,
        content_type="application/json",
        body=json.dumps(
            {"detail": {"code": error_code, "message": error_message}}, ensure_ascii=False
        ),
    )
    expect(page.locator("#delete-meeting")).to_be_enabled(timeout=10_000)
    expect(page.locator("#meeting-title")).to_have_text(other_title)
    expect(page.locator("#meeting-deletion-status")).to_be_hidden()
    audio_after = page.locator("#meeting-audio").evaluate(
        "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src })"
    )
    assert audio_after == audio_before

    page.locator("#recent-list a").filter(has_text=target_title).click()
    page.wait_for_url(f"{live.url}/meetings/{target_id}")
    expect(page.locator("#meeting-title")).to_have_text(target_title)
    status = page.locator("#meeting-deletion-status")
    expect(status).to_be_visible()
    expect(status).to_contain_text(expected_message)
    expect(status).not_to_contain_text("khởi động")
    expect(status).not_to_contain_text("Thử dọn tiếp")
    expect(page.locator("#delete-meeting")).to_have_text("Xóa cuộc họp")

    page.locator("#meeting-actions").click()

    page.locator("#delete-meeting").click()
    expect(page.locator("#meeting-delete-dialog")).to_be_visible()
    expect(page.locator("#meeting-delete-confirm")).to_have_text("Xóa dữ liệu đã lưu")
    page.locator("#meeting-delete-cancel").click()


def test_delete_ui_keeps_failed_cleanup_visible_and_retries(live, browser_context, tmp_path):
    meeting_id = seed_meeting(live.settings.data_dir, "Retry deletion target", "2026-10-14")
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        upload_id = connection.execute(
            "SELECT upload_id FROM recordings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()[0]
    outside = tmp_path / "external-targets"
    outside.mkdir()
    external_file = outside / f"{upload_id}.part"
    external_file.write_bytes(b"leave this outside data alone")
    upload_dir = live.settings.data_dir / "uploads"
    upload_dir.rmdir()
    upload_dir.symlink_to(outside, target_is_directory=True)

    context, page = browser_context
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    _confirm_meeting_deletion(page, "Retry deletion target")
    expect(page.locator("#meeting-delete-error")).to_contain_text("Yêu cầu đã được lưu")
    expect(page.locator("#meeting-deletion-status")).to_be_visible()
    expect(page.locator("#meeting-deletion-status")).to_have_text(
        "Dọn dẹp chưa hoàn tất. Hệ thống sẽ tự thử lại khi khởi động; "
        "sau khi xử lý sự cố, hãy chọn “Thử dọn tiếp”."
    )
    expect(page.locator("#delete-meeting")).to_have_text("Thử dọn tiếp")
    assert external_file.read_bytes() == b"leave this outside data alone"

    page.locator("#meeting-delete-cancel").click()
    expect(page.locator("#meeting-delete-dialog")).to_be_hidden()
    page.locator("#meeting-actions").click()
    page.locator("#delete-meeting").click()
    dialog = page.locator("#meeting-delete-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#meeting-delete-cancel")).to_have_text("Đóng")
    expect(dialog.locator("#meeting-delete-help")).to_contain_text(
        "Đóng hộp thoại sau khi xác nhận không hủy yêu cầu xóa"
    )
    expect(dialog.locator("#meeting-delete-help")).to_contain_text("Nếu dọn dẹp bị gián đoạn")
    expect(dialog.locator("#meeting-delete-help")).to_contain_text(
        "có thể thử lại sau khi khắc phục lỗi lưu trữ"
    )

    upload_dir.unlink()
    upload_dir.mkdir()
    page.locator("#meeting-delete-confirmation").fill("Retry deletion target")
    page.locator("#meeting-delete-confirm").click()
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#recent-list a")).to_have_count(0)
    assert context.request.get(f"{live.url}/api/meetings/{meeting_id}").status == 404


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_pending_meeting_delete_is_scoped_across_navigation(
    live, browser_context, tmp_path, outcome
):
    target_title = f"Pending deletion target {outcome}"
    other_title = f"Other meeting during {outcome}"
    target_id = seed_meeting(live.settings.data_dir, target_title, "2026-10-14")
    other_id = seed_meeting(live.settings.data_dir, other_title, "2026-10-14")
    upload_dir = None
    external_file = None
    if outcome == "failure":
        with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
            upload_id = connection.execute(
                "SELECT upload_id FROM recordings WHERE meeting_id = ?", (target_id,)
            ).fetchone()[0]
        outside = tmp_path / "external-targets"
        outside.mkdir()
        external_file = outside / f"{upload_id}.part"
        external_file.write_bytes(b"leave this outside data alone")
        upload_dir = live.settings.data_dir / "uploads"
        upload_dir.rmdir()
        upload_dir.symlink_to(outside, target_is_directory=True)

    context, page = browser_context
    try:
        page.goto(f"{live.url}/meetings/{target_id}", wait_until="domcontentloaded")
        expect(page.locator("#meeting-title")).to_have_text(target_title)
        page.locator("#meeting-actions").click()
        page.locator("#delete-meeting").click()
        page.locator("#meeting-delete-confirmation").fill(target_title)
        held = hold_first(page, rf"/api/meetings/{target_id}$", "DELETE")
        page.locator("#meeting-delete-confirm").click()

        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(25)
        assert held, "DELETE request did not reach the hold point"
        expect(page.locator("#delete-meeting")).to_be_disabled()
        expect(page.locator("#meeting-deletion-status")).to_be_visible()
        expect(page.locator("#meeting-deletion-status")).to_contain_text("Đang dọn dữ liệu…")
        expect(page.locator("#meeting-deletion-status")).not_to_contain_text(
            "Dọn dẹp chưa hoàn tất"
        )
        expect(page.locator("#meeting-deletion-status")).to_contain_text("Thử dọn tiếp")
        dialog = page.locator("#meeting-delete-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.locator("#meeting-delete-cancel")).to_have_text("Đóng")
        expect(dialog.locator("#meeting-delete-help")).to_contain_text(
            "Đóng hộp thoại sau khi xác nhận không hủy yêu cầu xóa"
        )
        expect(dialog.locator("#meeting-delete-help")).to_contain_text("Nếu dọn dẹp bị gián đoạn")
        expect(dialog.locator("#meeting-delete-help")).to_contain_text(
            "có thể thử lại sau khi khắc phục lỗi lưu trữ"
        )

        page.locator("#meeting-delete-cancel").click()
        expect(page.locator("#meeting-delete-dialog")).to_be_hidden()
        page.locator("#recent-list a").filter(has_text=other_title).click()
        page.wait_for_url(f"{live.url}/meetings/{other_id}")
        expect(page.locator("#meeting-title")).to_have_text(other_title)
        expect(page.locator("#meeting-audio")).to_have_attribute("data-meeting-id", other_id)
        audio_before = page.locator("#meeting-audio").evaluate(
            "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src })"
        )
        expect(page.locator("#delete-meeting")).to_be_disabled()
        expect(page.locator("#meeting-deletion-status")).to_be_hidden()
        expect(page.locator("#meeting-delete-dialog")).to_be_hidden()

        held[0].continue_()
        expect(page.locator("#delete-meeting")).to_be_enabled(timeout=10_000)
        expect(page.locator("#meeting-title")).to_have_text(other_title)
        expect(page.locator("#meeting-audio")).to_have_attribute("data-meeting-id", other_id)
        audio_after = page.locator("#meeting-audio").evaluate(
            "audio => ({ meetingId: audio.dataset.meetingId, src: audio.src })"
        )
        assert audio_after == audio_before
        expect(page.locator("#meeting-deletion-status")).to_be_hidden()
        expect(page.locator("#meeting-delete-dialog")).to_be_hidden()

        page.locator("#meeting-actions").click()

        page.locator("#delete-meeting").click()
        expect(page.locator("#meeting-delete-dialog")).to_be_visible()
        expect(page.locator("#meeting-delete-name")).to_have_text(other_title)
        expect(page.locator("#meeting-delete-confirm")).to_have_text("Xóa dữ liệu đã lưu")
        page.locator("#meeting-delete-cancel").click()

        if outcome == "success":
            assert context.request.get(f"{live.url}/api/meetings/{target_id}").status == 404
            assert context.request.get(f"{live.url}/api/meetings/{other_id}").status == 200
        else:
            assert external_file.read_bytes() == b"leave this outside data alone"
            page.goto(f"{live.url}/meetings/{target_id}", wait_until="domcontentloaded")
            expect(page.locator("#meeting-title")).to_have_text(target_title)
            expect(page.locator("#meeting-deletion-status")).to_be_visible()
            expect(page.locator("#meeting-deletion-status")).to_contain_text(
                "Dọn dẹp chưa hoàn tất"
            )
            expect(page.locator("#delete-meeting")).to_have_text("Thử dọn tiếp")
    finally:
        if upload_dir is not None and upload_dir.is_symlink():
            upload_dir.unlink()
            upload_dir.mkdir()


def test_wordmark_home_preserves_unsaved_transcript_draft(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(
        live.settings.data_dir, "Wordmark draft retention", "2026-10-14", duration_seconds=30
    )
    _publish_browser_generation(live, meeting_id)
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Wordmark draft retention")

    editor = edit_segment(page)
    editor.fill("Unsaved transcript draft.")
    page.evaluate("window.__wordmarkSpaMarker = 'same document'")
    page.get_by_role("link", name="ASR Studio — về workspace").click()
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#home-view")).to_be_visible()
    expect(page.locator("#page-title")).to_be_focused()
    assert page.evaluate("window.__wordmarkSpaMarker") == "same document"

    recent_meeting = page.locator("#recent-list a").filter(has_text="Wordmark draft retention")
    expect(recent_meeting).to_be_visible()
    recent_meeting.click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_have_text("Wordmark draft retention")
    expect(page.locator(".segment-text-editor")).to_have_value("Unsaved transcript draft.")


def test_empty_history_states(live, browser_context):
    _, page = browser_context
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    expect(page.locator("#sidebar-empty")).to_have_text("Cuộc họp đã lưu sẽ xuất hiện tại đây.")
    expect(page.locator("#workspace-empty")).to_be_visible()
    open_calendar(page)
    expect(page.locator("#large-calendar .cal-summary")).to_have_text(
        "Không có cuộc họp trong tháng này."
    )
    page.locator("#large-calendar .cal-undated").click()
    expect(page.locator("#day-panel-empty")).to_have_text("Mọi cuộc họp đều đã có ngày.")
    expect(page.locator("#large-calendar .cal-count")).to_have_count(0)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#home-view")).to_be_visible()


def test_sidebar_stays_viewport_high_and_in_place_while_a_long_transcript_scrolls(live, signed_in):
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_segments SET text = ? WHERE generation_id = ?",
            ("A long meeting discussion. " * 500, generation_id),
        )
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="networkidle")
    expect(page.locator("#transcript-segments > li")).to_have_count(2)
    sidebar = page.locator(".sidebar")

    def assert_pinned(height):
        box = sidebar.evaluate(
            "element => { const box = element.getBoundingClientRect();"
            " return { top: box.top, height: box.height }; }"
        )
        # At a fractional scroll end, sticky positioning can sit a sub-pixel off the top.
        assert abs(box["top"]) < 1 and box["height"] == height, box

    assert page.evaluate("document.body.scrollHeight") > 2 * 800
    assert_pinned(800)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert page.evaluate("window.scrollY") > 800
    assert_pinned(800)
    expect(page.locator(".version-link")).to_be_in_viewport()

    # A viewport shorter than the sidebar keeps its height and scrolls the sidebar itself.
    page.set_viewport_size({"width": 1280, "height": 400})
    assert_pinned(400)
    assert sidebar.evaluate("element => element.scrollHeight > element.clientHeight")
    sidebar.evaluate("element => element.scrollTo(0, element.scrollHeight)")
    expect(page.locator(".version-link")).to_be_in_viewport()

    # At 680px and below the sidebar is still the top bar that scrolls away with the page.
    page.set_viewport_size({"width": 390, "height": 844})
    assert sidebar.evaluate("element => getComputedStyle(element).position") == "static"
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert sidebar.evaluate("element => element.getBoundingClientRect().bottom") <= 0


RAIL_METRICS = """() => {
  const rail = document.querySelector(".sidebar");
  const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
  // What the browser gives the month select when nothing squeezes it: widest label, padding, caret.
  const month = document.querySelector("#mini-calendar .cal-month");
  const probe = month.cloneNode(true);
  probe.style.cssText = "position: absolute; visibility: hidden; width: max-content; flex: none";
  month.parentElement.append(probe);
  const monthWanted = probe.getBoundingClientRect().width;
  probe.remove();
  const sideways = rail.scrollWidth - rail.clientWidth;
  // Even content that cannot fit must not summon a horizontal scrollbar.
  const wide = document.createElement("div");
  wide.style.cssText = "width: 999px; height: 1px; flex: none";
  rail.append(wide);
  const horizontalBar = rail.offsetHeight - rail.clientHeight;
  wide.remove();
  return {
    scrollbar: rail.offsetWidth - rail.clientWidth
      - parseFloat(getComputedStyle(rail).borderRightWidth),
    tall: rail.scrollHeight > rail.clientHeight,
    horizontalBar,
    sideways,
    clientRight: rail.clientWidth,
    wordmarkRight: rect(".brand-wordmark").right,
    toggleLeft: rect(".theme-toggle").left,
    toggleRight: rect(".theme-toggle").right,
    monthWidth: month.getBoundingClientRect().width,
    monthWanted,
    pickRight: Math.max(
      ...[".cal-month", ".cal-year", ".cal-today"].map((c) => rect("#mini-calendar " + c).right),
    ),
    calendarRight: rect("#mini-calendar").right,
  };
}"""


@pytest.mark.parametrize("signed_in", [True], indirect=True, ids=["classic-scrollbars"])
def test_sidebar_fits_beside_a_classic_scrollbar_and_still_scrolls(live, signed_in):
    for title, date in (("Alpha", "2026-10-14"), ("Beta", "2026-10-14"), ("Gamma", "2026-10-16")):
        seed_meeting(live.settings.data_dir, title, date)
    for width, height in ((1366, 768), (1024, 700), (900, 600)):
        _context, page = signed_in(viewport={"width": width, "height": height})
        page.goto(f"{live.url}/", wait_until="domcontentloaded")
        expect(page.locator("#recent-list .recent-link")).to_have_count(3)
        expect(page.locator("#mini-calendar .cal-day").first).to_be_visible()
        metrics = page.evaluate(RAIL_METRICS)

        # The case is real: a drawn scrollbar takes width and the content outgrows the viewport.
        assert metrics["scrollbar"] > 0 and metrics["tall"], metrics
        assert metrics["horizontalBar"] == 0 and metrics["sideways"] <= 0, metrics
        assert metrics["wordmarkRight"] <= metrics["toggleLeft"], metrics
        assert metrics["toggleRight"] <= metrics["clientRight"] - 4, metrics  # clear of the bar
        assert metrics["monthWidth"] >= metrics["monthWanted"] - 0.5, metrics
        assert metrics["pickRight"] <= metrics["calendarRight"] + 0.5, metrics

        # The footer stays visible while the calendar/list still scroll independently.
        expect(page.locator(".version-link")).to_be_in_viewport()
        before_scroll = page.locator(".sidebar").evaluate("el => el.scrollTop")
        page.mouse.move(40, height // 2)
        page.mouse.wheel(0, 2000)
        page.wait_for_function(
            "before => document.querySelector('.sidebar').scrollTop > before", arg=before_scroll
        )
        expect(page.locator(".version-link")).to_be_in_viewport()


def test_calendar_opens_from_the_sidebar_calendar_and_the_header_button_only_on_phones(
    live, signed_in
):
    seed_meeting(live.settings.data_dir, "Dated meeting", "2026-10-14")
    seed_meeting(live.settings.data_dir, "Undated meeting")
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/", wait_until="domcontentloaded")

    expect(page.locator("#mini-open-calendar")).to_have_count(0)
    expect(page.get_by_role("button", name="Mở lịch")).to_have_count(0)
    expect(page.locator("#mini-calendar")).to_be_visible()
    expect(page.locator("#open-calendar")).to_be_hidden()

    page.locator('#mini-calendar .cal-day[data-date="2026-10-14"]').click()
    expect(page.locator("#calendar-view")).to_be_visible()
    expect(page.locator("#day-panel-title")).to_have_text("14/10/2026")
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#home-view")).to_be_visible()

    page.locator("#mini-calendar .cal-undated").click()
    expect(page.locator("#calendar-view")).to_be_visible()
    expect(page.locator("#day-panel-title")).to_have_text("Chưa đặt ngày")
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#home-view")).to_be_visible()

    page.set_viewport_size({"width": 375, "height": 800})
    expect(page.locator("#mini-calendar")).to_be_hidden()
    expect(page.locator("#open-calendar")).to_be_visible()
    page.locator("#open-calendar").click()
    expect(page.locator("#calendar-view")).to_be_visible()


def test_diarization_error_shows_sanitized_copyable_diagnostic_command(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Diarization failure", "2026-10-14")
    task_id = uuid.uuid4().hex
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, "
            "audio_duration_seconds, error_code, error_message) "
            "VALUES (?, ?, ?, ?, 'transcription', '/private/model/path', 'model', 'cpu', "
            "'int8', 'en', 'failed', 'failed', 5, 'diarization_input_decode_failed', ?)",
            (
                task_id,
                uuid.uuid4().hex,
                "request-hash",
                meeting_id,
                "vendor detail /private/path token=secret",
            ),
        )

    generation_id = uuid.uuid4().hex
    segment_id = uuid.uuid4().hex

    def route_meeting_with_generation(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = {
            "id": generation_id,
            "generation_number": 1,
            "status": "complete",
            "language_hint": "en",
            "model_name": "whisper-large-v3",
            "language_probability": 0.94,
            "source_version": 1,
            "uncertainty_policy": "faster-whisper-review-cues-v1",
            "diarization_model_name": "Community-1",
            "segment_count": 1,
            "completed_at": "2026-10-17 00:00:05",
            "speakers": [
                {
                    "speaker_id": "SPEAKER_00",
                    "display_name": "Speaker 1",
                    "first_seen_seconds": 0.0,
                    "last_seen_seconds": 1.0,
                },
                {
                    "speaker_id": "SPEAKER_01",
                    "display_name": "Speaker 2",
                    "first_seen_seconds": 1.0,
                    "last_seen_seconds": 2.0,
                },
            ],
            "segments": [
                {
                    "id": segment_id,
                    "sequence": 0,
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "text": "Earlier complete transcript.",
                    "original_speaker_id": "SPEAKER_00",
                    "speaker_id": "SPEAKER_00",
                    "avg_logprob": -0.3,
                    "no_speech_prob": 0.02,
                    "compression_ratio": 0.8,
                    "uncertain": False,
                    "review_reasons": [],
                    "overlap": False,
                    "speaker_dominance": 1.0,
                    "assignment_version": 1,
                }
            ],
        }
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting_with_generation)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"),
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps(
                {"detail": {"code": "speaker_save_failed", "message": "Không thể lưu người nói."}}
            ),
        ),
    )

    context.add_init_script(
        "window.__copiedCommand = ''; "
        "Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {"
        "window.__copiedCommand = value;}}});"
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    expect(page.locator("#transcription-error")).to_be_visible()
    expect(page.locator("#task-diagnostic-code")).to_have_text("diarization_input_decode_failed")
    expect(page.locator("#task-diagnostic-id")).to_have_text(task_id)
    command = f"docker compose exec -T app python -m asr_studio.cli task diagnose {task_id}"
    command_input = page.locator("#task-diagnostic-command")
    expect(command_input).to_have_value(command)
    expect(command_input).to_have_attribute("readonly", "")
    expect(page.locator("#transcription-error")).not_to_contain_text("private/path")
    expect(page.locator("#transcription-error")).not_to_contain_text("secret")

    page.get_by_role("button", name="Sao chép lệnh").click()
    expect(page.locator("#task-diagnostic-copy-status")).to_have_text("Đã sao chép lệnh.")
    assert page.evaluate("window.__copiedCommand") == command
    assert page.locator("#transcription-error #task-diagnostic-copy").count() == 0
    assert page.locator("#transcription-error #task-diagnostic-copy-status").count() == 0

    edit_segment(page)
    page.locator(".segment-speaker-select").select_option("SPEAKER_01")
    expect(page.locator("#transcript-mutation-error-message")).to_have_text(
        "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau."
    )
    expect(page.locator("#transcription-error-message")).to_have_text(
        "Không thể giải mã bản ghi. Tải lại file âm thanh hợp lệ rồi thử lại."
    )
    expect(page.locator(".segment-speaker-select")).to_have_value("SPEAKER_00")
    expect(page.locator("#task-diagnostic-details")).to_be_visible()
    expect(page.locator("#task-diagnostic-command-wrap")).to_be_visible()
    expect(page.locator("#task-diagnostic-copy-status")).to_have_text("Đã sao chép lệnh.")

    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET error_code = 'publication_failed', error_message = ? WHERE id = ?",
            ("database is locked at /private/path", task_id),
        )
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#transcription-error-message")).to_have_text(
        "Không thể lưu transcript hoàn chỉnh. Kiểm tra bộ nhớ lưu trữ rồi thử lại."
    )
    expect(page.locator("#task-diagnostic-code")).to_have_text("publication_failed")
    expect(page.locator("#task-diagnostic-id")).to_have_text(task_id)
    expect(page.locator("#task-diagnostic-command")).to_have_value(command)
    expect(page.locator("#transcription-error")).not_to_contain_text("database is locked")
    expect(page.locator("#transcription-error")).not_to_contain_text("/private/path")

    page.route(
        f"{live.url}/api/tasks",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps(
                {"detail": {"code": "worker_busy", "message": "Không thể gửi lại tác vụ."}}
            ),
        ),
    )
    page.get_by_role("button", name="Chạy lại bản ghi").click()
    expect(page.locator("#transcription-error-message")).to_contain_text(
        "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau."
    )
    expect(page.locator("#task-diagnostic-details")).to_be_hidden()
    expect(page.locator("#task-diagnostic-command-wrap")).to_be_hidden()

    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET error_code = 'constructor', error_message = ? WHERE id = ?",
            ("Safe fallback.", task_id),
        )
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#transcription-error-message")).to_have_text("Tác vụ chưa hoàn tất.")
    expect(page.locator("#task-diagnostic-details")).to_be_hidden()
    expect(page.locator("#task-diagnostic-command-wrap")).to_be_hidden()


def test_edit_metadata_conflict_keeps_draft_and_persists_across_restart(live, browser_context):
    context, page_a = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Original", "2026-10-14", language_hint="vi")
    seed_meeting(live.settings.data_dir, "Other", "2026-10-13")
    page_a.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page_a.locator("#meeting-title")).to_have_text("Original")
    page_b = open_page(context, live, f"/meetings/{meeting_id}")
    expect(page_b.locator("#meeting-title")).to_have_text("Original")

    patches: list[dict] = []
    page_b.on(
        "request",
        lambda request: (
            patches.append(json.loads(request.post_data)) if request.method == "PATCH" else None
        ),
    )

    # Tab A starts a draft but does not save yet.
    page_a.locator("#meeting-actions").click()
    page_a.get_by_role("button", name="Sửa tên và ngày").click()
    page_a.locator("#edit-title").fill("Draft from A")
    page_a.locator("#edit-clear-date").click()

    # Tab B saves a title-only edit; it must not carry a date or language.
    page_b.locator("#meeting-actions").click()
    page_b.get_by_role("button", name="Sửa tên và ngày").click()
    page_b.locator("#edit-title").fill("Saved from B")
    page_b.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_b.locator("#meeting-title")).to_have_text("Saved from B")
    expect(page_b.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.")
    assert patches == [{"expected_metadata_version": 1, "title": "Saved from B"}]
    expect(page_b.locator("#recent-list a").first).to_contain_text("Saved from B")

    # Tab A's save is stale: rejected, draft kept, nothing merged or overwritten.
    page_a.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_a.locator("#edit-conflict")).to_contain_text("Saved from B")
    expect(page_a.locator("#edit-title")).to_have_value("Draft from A")
    expect(page_a.locator("#edit-date")).to_have_value("")
    expect(page_a.locator("#meeting-title")).to_have_text("Saved from B")
    expect(page_a.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-14")
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert (stored["title"], stored["meeting_date"]) == ("Saved from B", "2026-10-14")

    # After comparing, an explicit second save wins and removes the date.
    page_a.get_by_role("button", name="Lưu bản nháp lên bản mới nhất").click()
    expect(page_a.locator("#meeting-title")).to_have_text("Draft from A")
    expect(page_a.locator("#meeting-date")).to_have_text("Chưa đặt ngày")
    expect(page_a.locator("#meeting-language")).to_have_text("Tiếng Việt")
    expect(page_a.locator("#recent-list")).not_to_contain_text("Draft from A")
    open_calendar(page_a)
    expect(page_a.locator("#large-calendar .cal-undated-count")).to_have_text("1")
    expect(page_a.locator('#large-calendar .cal-day[data-date="2026-10-14"]')).not_to_have_class(
        "has-meetings"
    )
    page_a.get_by_label("Tìm cuộc họp theo tên").fill("draft")
    expect(page_a.locator("#recent-list a")).to_have_count(1)
    expect(page_a.locator("#recent-list a")).to_contain_text("Chưa đặt ngày")

    # Restart the server: the edits come from SQLite, not frontend memory.
    live.stop()
    live.start()
    page_a.reload(wait_until="domcontentloaded")
    expect(page_a.locator("#large-calendar .cal-undated-count")).to_have_text("1")
    page_a.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page_a.locator("#meeting-title")).to_have_text("Draft from A")
    expect(page_a.locator("#meeting-date")).to_have_text("Chưa đặt ngày")
    final = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert final["metadata_version"] == 3
    assert final["notes_source_version"] == 2  # only the date change bumped the notes source
    assert final["language_hint"] == "vi"

    # Re-adding a date from the editor moves it back onto the calendar.
    page_a.locator("#meeting-actions").click()
    page_a.get_by_role("button", name="Sửa tên và ngày").click()
    page_a.locator("#edit-date").fill("2026-02-28")
    page_a.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_a.locator("#meeting-date")).to_have_attribute("datetime", "2026-02-28")
    open_calendar(page_a)
    page_a.locator("#large-calendar .cal-month").select_option("1")
    expect(
        page_a.locator('#large-calendar .cal-day[data-date="2026-02-28"] .cal-count')
    ).to_have_text("1")


def record_patches(page) -> list[dict]:
    patches: list[dict] = []
    page.on(
        "request",
        lambda request: (
            patches.append(json.loads(request.post_data)) if request.method == "PATCH" else None
        ),
    )
    return patches


def test_partial_date_is_not_treated_as_clearing_the_date(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Keep my date", "2026-10-14")
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    patches = record_patches(page)

    page.locator("#meeting-actions").click()

    page.get_by_role("button", name="Sửa tên và ngày").click()
    date = page.locator("#edit-date")
    date.focus()
    page.keyboard.press("Backspace")  # clears only the focused month segment
    assert date.evaluate("element => element.validity.badInput") is True
    assert date.input_value() == "", "a partial date reads as empty, which must not mean delete"
    page.locator("#edit-title").fill("Renamed too")

    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-error")).to_contain_text("chưa đầy đủ")
    expect(page.locator("#meeting-edit")).to_be_visible()
    expect(page.locator("#edit-title")).to_have_value("Renamed too")
    assert patches == []
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert (stored["title"], stored["meeting_date"]) == ("Keep my date", "2026-10-14")

    # Deleting the date is still possible, but only on purpose.
    page.locator("#edit-clear-date").click()
    assert page.locator("#edit-date").evaluate("element => element.validity.badInput") is False
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#meeting-date")).to_have_text("Chưa đặt ngày")
    assert patches == [
        {"expected_metadata_version": 1, "title": "Renamed too", "meeting_date": None}
    ]


def test_conflict_resends_only_fields_the_user_edited(live, browser_context):
    context, page_a = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Orig title", "2026-10-14")
    path = f"/meetings/{meeting_id}"
    page_a.goto(f"{live.url}{path}", wait_until="domcontentloaded")
    page_b = open_page(context, live, path)
    patches_a = record_patches(page_a)
    patches_b = record_patches(page_b)

    page_a.locator("#meeting-actions").click()

    page_a.get_by_role("button", name="Sửa tên và ngày").click()
    page_a.locator("#edit-title").fill("Title from A")
    page_b.locator("#meeting-actions").click()
    page_b.get_by_role("button", name="Sửa tên và ngày").click()
    page_b.locator("#edit-date").fill("2026-11-02")
    page_b.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_b.locator("#meeting-date")).to_have_attribute("datetime", "2026-11-02")
    assert patches_b == [{"expected_metadata_version": 1, "meeting_date": "2026-11-02"}]

    page_a.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_a.locator("#edit-conflict")).to_be_visible()
    expect(page_a.locator("#edit-title")).to_have_value("Title from A")
    expect(page_a.locator("#edit-date")).to_have_value("2026-11-02")  # untouched: follows server
    page_a.get_by_role("button", name="Lưu bản nháp lên bản mới nhất").click()
    expect(page_a.locator("#meeting-title")).to_have_text("Title from A")
    expect(page_a.locator("#meeting-date")).to_have_attribute("datetime", "2026-11-02")
    assert patches_a == [
        {"expected_metadata_version": 1, "title": "Title from A"},
        {"expected_metadata_version": 2, "title": "Title from A"},
    ]
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert (stored["title"], stored["meeting_date"]) == ("Title from A", "2026-11-02")
    assert (stored["metadata_version"], stored["notes_source_version"]) == (3, 2)

    # The same field edited in both tabs may be overwritten, but only after confirmation.
    page_b.reload(wait_until="domcontentloaded")
    expect(page_b.locator("#meeting-title")).to_have_text("Title from A")
    page_a.locator("#meeting-actions").click()
    page_a.get_by_role("button", name="Sửa tên và ngày").click()
    page_a.locator("#edit-title").fill("A wins")
    page_b.locator("#meeting-actions").click()
    page_b.get_by_role("button", name="Sửa tên và ngày").click()
    page_b.locator("#edit-title").fill("B first")
    page_b.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_b.locator("#meeting-title")).to_have_text("B first")
    page_a.get_by_role("button", name="Lưu thay đổi").click()
    expect(page_a.locator("#edit-conflict")).to_be_visible()
    expect(page_a.locator("#edit-title")).to_have_value("A wins")
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert stored["title"] == "B first"
    page_a.get_by_role("button", name="Lưu bản nháp lên bản mới nhất").click()
    expect(page_a.locator("#meeting-title")).to_have_text("A wins")


def test_calendar_does_not_add_history_entries_or_revive_stale_return_target(live, browser_context):
    _, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Back target", "2026-10-14")
    page.goto(f"{live.url}/", wait_until="domcontentloaded")

    page.locator("#recent-list a").first.click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    assert page.url == f"{live.url}/meetings/{meeting_id}", "the calendar must not change the URL"

    # Browser Back from the calendar goes to the previous page, not to a stale meeting.
    page.go_back()
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#home-view")).to_be_visible()
    expect(page.locator("#calendar-view")).to_be_hidden()
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#home-view")).to_be_visible()
    expect(page.locator("#meeting-view")).to_be_hidden()
    assert page.url == f"{live.url}/"

    # Returning from the calendar adds no entry: one Back from the meeting reaches home.
    page.locator("#recent-list a").first.click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Back target")
    page.go_back()
    page.wait_for_url(f"{live.url}/")
    expect(page.locator("#home-view")).to_be_visible()


def test_search_failure_is_an_error_not_an_empty_result(live, browser_context):
    _, page = browser_context
    seed_meeting(live.settings.data_dir, "Alpha review", "2026-10-14")
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    search = page.get_by_label("Tìm cuộc họp theo tên")

    page.route(re.compile(r"/api/meetings\?q="), lambda route: route.fulfill(status=500, body="{}"))
    search.fill("Alpha")
    expect(page.locator("#search-error")).to_be_visible()
    expect(page.locator("#search-empty")).to_be_hidden()

    page.unroute(re.compile(r"/api/meetings\?q="))
    search.fill("Alph")
    expect(page.locator("#search-error")).to_be_hidden()
    expect(page.locator("#recent-list a")).to_have_count(1)
    search.fill("zzz")
    expect(page.locator("#search-empty")).to_be_visible()
    expect(page.locator("#search-error")).to_be_hidden()


def test_negative_utc_offset_keeps_date_only_values_and_local_today(live, signed_in):
    # 05:00 UTC on 17 Oct is still the 16th in American Samoa (UTC-11).
    context, page = signed_in(timezone="Pacific/Pago_Pago", now="2026-10-17T05:00:00Z")
    meeting_id = seed_meeting(live.settings.data_dir, "Late night", "2026-10-14")
    seed_meeting(live.settings.data_dir, "Year end", "2025-12-31")
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-14")
    expect(page.locator("#meeting-date")).to_contain_text("14 tháng 10")
    expect(page.locator("#recent-list .link-date").first).to_have_text("14/10/2026 · 1 giây")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    expect(page.locator("#edit-date")).to_have_value("2026-10-14")
    page.locator("#edit-cancel").click()

    open_calendar(page)
    large = page.locator("#large-calendar")
    expect(large.locator(".cal-day.is-today")).to_have_attribute("data-date", "2026-10-16")
    expect(large.locator('.cal-day[data-date="2026-10-14"] .cal-count')).to_have_text("1")
    large.locator(".cal-month").select_option("11")
    large.locator(".cal-year").fill("2025")
    large.locator(".cal-year").press("Enter")
    expect(large.locator('.cal-day[data-date="2025-12-31"] .cal-count')).to_have_text("1")


def test_mini_calendar_opens_large_calendar_and_supports_keyboard(
    live, browser_context, monkeypatch
):
    _, page = browser_context
    seed_meeting(live.settings.data_dir, "Mini target", "2026-10-14")
    seed_meeting(live.settings.data_dir, "Mini undated")
    history_request_started = threading.Event()
    history_request_release = threading.Event()
    original_list_meetings = routes_module.list_meetings

    def delayed_list_meetings(data_dir, *, query=None):
        if query is None:
            history_request_started.set()
            history_request_release.wait(timeout=5)
        return original_list_meetings(data_dir, query=query)

    monkeypatch.setattr(routes_module, "list_meetings", delayed_list_meetings)
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    assert history_request_started.wait(timeout=2)
    mini = page.locator("#mini-calendar")
    calendar = page.locator("#calendar-view")
    expect(calendar).to_be_hidden()

    mini.locator(".cal-day.is-selected").focus()
    history_request_release.set()
    expect(mini.locator('.cal-day[data-date="2026-10-14"] .cal-count')).to_have_text("1")
    page.keyboard.press("ArrowRight")
    expect(mini.locator(".cal-day.is-selected")).to_have_attribute("data-date", "2026-10-18")
    assert page.evaluate("document.activeElement.closest('#mini-calendar') !== null")
    assert page.evaluate("document.activeElement.dataset.date") == "2026-10-18"
    expect(calendar).to_be_hidden()
    page.keyboard.press("Enter")
    expect(calendar).to_be_visible()
    expect(page.locator("#day-panel-title")).to_have_text("18/10/2026")
    page.get_by_role("button", name="Quay lại notes").click()
    expect(calendar).to_be_hidden()

    mini.locator('.cal-day[data-date="2026-10-14"]').click()
    expect(calendar).to_be_visible()
    expect(page.locator('#large-calendar .cal-day[data-date="2026-10-14"]')).to_have_class(
        re.compile(r"is-selected")
    )
    expect(page.locator("#day-panel-title")).to_have_text("14/10/2026")
    expect(page.locator("#day-panel-list a")).to_have_count(1)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(calendar).to_be_hidden()

    mini.locator(".cal-undated").click()
    expect(calendar).to_be_visible()
    expect(page.locator("#day-panel-title")).to_have_text("Chưa đặt ngày")
    expect(page.locator("#day-panel-list a")).to_contain_text(["Mini undated"])


def hold_first(page, pattern, method):
    """Hold the first matching request until the test releases it; later ones pass."""
    held: list = []

    def handler(route):
        if route.request.method == method and not held:
            held.append(route)
        else:
            route.fallback()

    page.route(re.compile(pattern), handler)
    return held


def settle(page, milliseconds=300):
    page.evaluate(f"new Promise(resolve => setTimeout(resolve, {milliseconds}))")


def wait_for_stored_title(context, live, meeting_id, title):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
        if stored["title"] == title:
            return stored
        time.sleep(0.05)
    raise AssertionError(f"{title!r} was never stored")


def audio_meeting_id(page):
    return page.locator("#meeting-audio").get_attribute("data-meeting-id")


def test_slow_save_never_repaints_after_navigating_elsewhere(live, browser_context):
    context, page = browser_context
    a = seed_meeting(live.settings.data_dir, "Meeting A", "2026-10-14")
    b = seed_meeting(live.settings.data_dir, "Meeting B", "2026-10-13")
    page.goto(f"{live.url}/meetings/{a}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Meeting A")

    # Save A, then open B before the PATCH returns.
    held = hold_first(page, rf"/api/meetings/{a}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("A renamed")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-save")).to_be_disabled()
    page.locator("#recent-list a").filter(has_text="Meeting B").click()
    page.wait_for_url(f"{live.url}/meetings/{b}")
    expect(page.locator("#meeting-title")).to_have_text("Meeting B")
    held[0].continue_()
    wait_for_stored_title(context, live, a, "A renamed")
    settle(page)
    expect(page.locator("#meeting-title")).to_have_text("Meeting B")
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")  # the name shows once, below
    assert page.title() == "Meeting B · ASR Studio"
    assert page.url == f"{live.url}/meetings/{b}"
    assert audio_meeting_id(page) == b
    expect(page.locator("#meeting-edit")).to_be_hidden()
    expect(page.locator("#edit-status")).to_have_text("")
    expect(page.locator("#recent-list")).to_contain_text("A renamed")  # lists still learn of it

    # Save B, then open the calendar before the PATCH returns.
    held = hold_first(page, rf"/api/meetings/{b}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("B renamed")
    page.get_by_role("button", name="Lưu thay đổi").click()
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    held[0].continue_()
    wait_for_stored_title(context, live, b, "B renamed")
    settle(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    expect(page.locator("#meeting-view")).to_be_hidden()
    expect(page.locator("#page-title")).to_have_text("Lịch cuộc họp")
    assert page.title() == "Lịch cuộc họp · ASR Studio"
    assert page.url == f"{live.url}/meetings/{b}"
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("B renamed")
    expect(page.locator("#meeting-edit")).to_be_hidden()


def test_delayed_transcript_save_notifies_other_tab_after_origin_navigation_and_keeps_draft(
    live, browser_context
):
    context, page_a = browser_context
    other_id = seed_meeting(live.settings.data_dir, "Other tab destination", "2026-10-13")
    meeting_id, _generation_id = open_reading_meeting(live, page_a, title="Shared transcript")
    page_b = open_page(context, live, f"/meetings/{meeting_id}")
    expect(page_b.locator(".segment-edit-button").first).to_be_enabled()

    # Tab B has a dirty draft that must survive the authoritative refresh caused by A.
    row_b = page_b.locator(".transcript-segment").first
    editor_b = edit_segment(page_b)
    editor_b.fill("Draft kept in B")

    observed = page_b.context.new_page()
    observed.goto(f"{live.url}/", wait_until="domcontentloaded")
    observed.evaluate(
        """() => {
          window.__meetingChangeMessages = [];
          const channel = new BroadcastChannel('asr-studio:meeting-changes');
          channel.addEventListener('message', event =>
            window.__meetingChangeMessages.push(event.data));
        }"""
    )

    # Tab A confirms the write only after the user has moved to another meeting.
    held = hold_first(
        page_a,
        rf"/api/meetings/{meeting_id}/transcript-segments/[^/]+/text$",
        "PATCH",
    )
    row_a = page_a.locator(".transcript-segment").first
    edit_segment(page_a)
    row_a.locator(".segment-text-editor").fill("Saved from A")
    row_a.get_by_role("button", name="Lưu đoạn").click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page_a.wait_for_timeout(25)
    assert held, "the transcript save did not reach the hold point"

    page_a.locator("#recent-list a").filter(has_text="Other tab destination").click()
    page_a.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page_a.locator("#meeting-title")).to_have_text("Other tab destination")
    page_b.wait_for_load_state("networkidle")
    receiver_notes_reads = []
    page_b.on(
        "request",
        lambda request: (
            receiver_notes_reads.append(request.url)
            if request.method == "GET"
            and request.url == f"{live.url}/api/meetings/{meeting_id}/notes"
            else None
        ),
    )
    held[0].continue_()

    expect(row_b.locator(".segment-text-editor")).to_have_value("Draft kept in B", timeout=10_000)
    expect(row_b.locator(".transcript-draft-review")).to_contain_text(
        "Bản mới nhất đã lưu: Saved from A", timeout=10_000
    )
    page_b.wait_for_load_state("networkidle")
    assert len(receiver_notes_reads) == 1, "one hint must cause one authoritative notes read"
    row_b.get_by_role("button", name="Hủy").click()
    expect(row_b.locator(".segment-draft-mark")).to_have_text("Bản nháp chưa lưu")
    messages = observed.evaluate("window.__meetingChangeMessages")
    assert len(messages) == 1, "the receiver's authoritative read must not echo-broadcast"
    assert messages[0]["meetingId"] == meeting_id
    observed.close()
    page_b.close()


def test_slow_conflict_lookup_never_repaints_after_navigating_elsewhere(live, browser_context):
    context, page = browser_context
    a = seed_meeting(live.settings.data_dir, "Conflict A", "2026-10-14")
    b = seed_meeting(live.settings.data_dir, "Conflict B", "2026-10-13")
    page.goto(f"{live.url}/meetings/{a}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Conflict A")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("Draft A")

    other_tab = context.request.patch(
        f"{live.url}/api/meetings/{a}",
        headers={"Origin": live.url},
        data={"expected_metadata_version": 1, "title": "From elsewhere"},
    )
    assert other_tab.ok
    # The conflict re-read of A is the next GET of A; hold it while we move on to B.
    held = hold_first(page, rf"/api/meetings/{a}$", "GET")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-save")).to_be_disabled()
    page.locator("#recent-list a").filter(has_text="Conflict B").click()
    page.wait_for_url(f"{live.url}/meetings/{b}")
    expect(page.locator("#meeting-title")).to_have_text("Conflict B")
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        time.sleep(0.05)
    assert held, "the conflict lookup never started"
    held[0].continue_()
    settle(page)
    expect(page.locator("#meeting-title")).to_have_text("Conflict B")
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")  # the name shows once, below
    assert audio_meeting_id(page) == b
    expect(page.locator("#edit-conflict")).to_be_hidden()
    expect(page.locator("#meeting-edit")).to_be_hidden()
    stored = context.request.get(f"{live.url}/api/meetings/{a}").json()
    assert stored["title"] == "From elsewhere", "a conflicted save must not write"


def test_leaving_calendar_follows_the_url_even_while_loading_or_after_an_error(
    live, browser_context
):
    context, page = browser_context
    x = seed_meeting(live.settings.data_dir, "Slow meeting", "2026-10-14")

    # Calendar opened while /meetings/X is still loading: leaving loads X, not home.
    held = hold_first(page, rf"/api/meetings/{x}$", "GET")
    page.goto(f"{live.url}/meetings/{x}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-loading")).to_be_visible()
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    held[0].continue_()
    settle(page)
    expect(page.locator("#calendar-view")).to_be_visible()  # the late load must not repaint
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Slow meeting")
    assert page.url == f"{live.url}/meetings/{x}"
    assert page.evaluate("document.activeElement.id") == "meeting-title"

    # From the error view the URL still names the meeting that failed, so return there.
    page.goto(f"{live.url}/meetings/missing", wait_until="domcontentloaded")
    expect(page.locator("#meeting-error")).to_be_visible()
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-error")).to_be_visible()
    assert page.url == f"{live.url}/meetings/missing"
    assert page.evaluate("document.activeElement.id") == "meeting-error-title"

    # From home, leaving lands on home with focus on the page heading.
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#home-view")).to_be_visible()
    assert page.evaluate("document.activeElement.id") == "page-title"


def test_duration_rounds_before_choosing_the_unit(live, browser_context):
    _, page = browser_context
    for seconds in (0.4, 59.4, 59.5, 59.99, 90, 3599.6):
        seed_meeting(live.settings.data_dir, f"d {seconds}", "2026-10-20", duration_seconds=seconds)
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    open_calendar(page)
    page.locator('#large-calendar .cal-day[data-date="2026-10-20"]').click()
    expect(page.locator("#day-panel-list a")).to_have_count(6)
    labels = {
        link.locator(".link-title").inner_text(): link.locator(".link-date").inner_text()
        for link in page.locator("#day-panel-list a").all()
    }
    assert labels == {
        "d 0.4": "20/10/2026 · 1 giây",
        "d 59.4": "20/10/2026 · 59 giây",
        "d 59.5": "20/10/2026 · 1 phút",
        "d 59.99": "20/10/2026 · 1 phút",
        "d 90": "20/10/2026 · 2 phút",
        "d 3599.6": "20/10/2026 · 1 giờ 00 phút",
    }
    assert not any("60 giây" in text for text in labels.values())


def test_year_box_commits_on_typing_and_enter_and_rejects_bad_years(live, browser_context):
    _, page = browser_context
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    open_calendar(page)
    large_title = page.locator("#large-calendar .cal-title")
    mini_title = page.locator("#mini-calendar .cal-title")
    year = page.locator("#large-calendar .cal-year")
    expect(large_title).to_have_text("Tháng 10 · 2026")

    # Real key presses: partial years are ignored, the 4th digit commits, Enter keeps it.
    year.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("2031")
    expect(large_title).to_have_text("Tháng 10 · 2031")
    page.keyboard.press("Enter")
    expect(large_title).to_have_text("Tháng 10 · 2031")
    expect(mini_title).to_have_text("Tháng 10 · 2031")
    expect(page.locator("#mini-calendar .cal-year")).to_have_value("2031")

    # Intermediate and out-of-range text never corrupts state; Enter restores the real year.
    for bad in ("20", "999", "12345", ""):
        year.fill(bad)
        expect(large_title).to_have_text("Tháng 10 · 2031")
        year.press("Enter")
        expect(year).to_have_value("2031")
        expect(large_title).to_have_text("Tháng 10 · 2031")

    # Mini calendar's own year box drives both, and Enter without a change is harmless.
    mini_year = page.locator("#mini-calendar .cal-year")
    mini_year.fill("2024")
    mini_year.press("Enter")
    expect(large_title).to_have_text("Tháng 10 · 2024")
    expect(year).to_have_value("2024")


def test_year_box_blur_commits_valid_years_and_reverts_partial_ones(live, browser_context):
    _, page = browser_context
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    open_calendar(page)
    title = page.locator("#large-calendar .cal-title")
    year = page.locator("#large-calendar .cal-year")

    year.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("2040")
    page.keyboard.press("Tab")  # blur with a valid year keeps it
    expect(title).to_have_text("Tháng 10 · 2040")
    expect(year).to_have_value("2040")
    expect(page.locator("#mini-calendar .cal-year")).to_have_value("2040")

    for partial in ("99", "20", "12345"):
        year.click()
        page.keyboard.press("Control+A")
        year.fill(partial)  # one input event, so no valid prefix is typed on the way
        page.keyboard.press("Tab")  # blur with an intermediate/invalid year reverts it
        expect(year).to_have_value("2040")
        expect(title).to_have_text("Tháng 10 · 2040")
        expect(page.locator("#mini-calendar .cal-title")).to_have_text("Tháng 10 · 2040")

    # Blurring by clicking a day must not swallow that click.
    year.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("20")
    page.locator('#large-calendar .cal-day[data-date="2040-10-15"]').click()
    expect(page.locator("#day-panel-title")).to_have_text("15/10/2040")
    expect(year).to_have_value("2040")


def test_background_history_refresh_does_not_overwrite_partial_year_text(live, browser_context):
    _, page = browser_context
    seed_meeting(live.settings.data_dir, "Refresh target", "2026-10-14")
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    expect(page.locator("#recent-list a")).to_have_count(1)

    held = hold_first(page, r"/api/meetings$", "GET")  # the refresh the calendar triggers
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    year = page.locator("#large-calendar .cal-year")
    year.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("20")
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        time.sleep(0.05)
    assert held, "the calendar's history refresh never started"
    held[0].continue_()
    settle(page)

    expect(year).to_have_value("20")
    assert page.evaluate("document.activeElement.classList.contains('cal-year')")
    expect(page.locator("#large-calendar .cal-title")).to_have_text("Tháng 10 · 2026")
    # The refresh itself did land: the data is on the calendar.
    expect(
        page.locator('#large-calendar .cal-day[data-date="2026-10-14"] .cal-count')
    ).to_have_text("1")
    page.keyboard.press("Tab")
    expect(year).to_have_value("2026")


def release_when_held(held):
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        time.sleep(0.05)
    assert held, "the request never started"
    held[0].continue_()


def test_save_released_after_a_calendar_round_trip_is_rendered_and_saved(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Round trip", "2026-10-14")
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Round trip")

    held = hold_first(page, rf"/api/meetings/{meeting_id}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("After round trip")
    page.locator("#edit-date").fill("2026-10-20")
    page.get_by_role("button", name="Lưu thay đổi").click()
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Round trip")  # nothing saved yet
    release_when_held(held)

    expect(page.locator("#meeting-title")).to_have_text("After round trip")
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")  # the name shows once, below
    expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-20")
    expect(page.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.")
    expect(page.locator("#meeting-edit")).to_be_hidden()
    expect(page.locator("#edit-meeting")).to_be_enabled()
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert (stored["title"], stored["meeting_date"]) == ("After round trip", "2026-10-20")
    expect(page.locator("#recent-list")).to_contain_text("After round trip")


def test_save_result_arriving_while_the_calendar_is_open_shows_after_leaving(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Quiet save", "2026-10-14")
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    held = hold_first(page, rf"/api/meetings/{meeting_id}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("Saved in background")
    page.get_by_role("button", name="Lưu thay đổi").click()
    open_calendar(page)
    release_when_held(held)
    wait_for_stored_title(context, live, meeting_id, "Saved in background")
    settle(page)
    expect(page.locator("#page-title")).to_have_text("Lịch cuộc họp")  # calendar untouched

    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Saved in background")
    expect(page.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.")
    expect(page.locator("#meeting-edit")).to_be_hidden()


def test_conflict_arriving_around_a_calendar_round_trip_is_not_lost(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Conflict trip", "2026-10-14")
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    def other_tab_renames(title, version):
        response = context.request.patch(
            f"{live.url}/api/meetings/{meeting_id}",
            headers={"Origin": live.url},
            data={"expected_metadata_version": version, "title": title},
        )
        assert response.ok

    # Conflict lands after returning from the calendar.
    held = hold_first(page, rf"/api/meetings/{meeting_id}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("My draft")
    page.get_by_role("button", name="Lưu thay đổi").click()
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    other_tab_renames("Other tab one", 1)
    release_when_held(held)
    expect(page.locator("#edit-conflict")).to_contain_text("Other tab one")
    expect(page.locator("#edit-title")).to_have_value("My draft")
    expect(page.locator("#meeting-title")).to_have_text("Other tab one")
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert stored["title"] == "Other tab one"

    # Conflict lands while the calendar is open: it is waiting after leaving.
    page.get_by_role("button", name="Hủy và bỏ bản nháp").click()
    held = hold_first(page, rf"/api/meetings/{meeting_id}$", "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("Second draft")
    page.get_by_role("button", name="Lưu thay đổi").click()
    open_calendar(page)
    other_tab_renames("Other tab two", 2)
    release_when_held(held)
    wait_for_stored_title(context, live, meeting_id, "Other tab two")
    settle(page)
    expect(page.locator("#page-title")).to_have_text("Lịch cuộc họp")
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#edit-conflict")).to_contain_text("Other tab two")
    expect(page.locator("#edit-title")).to_have_value("Second draft")
    expect(page.locator("#meeting-title")).to_have_text("Other tab two")
    expect(page.locator("#edit-save")).to_have_text("Lưu bản nháp lên bản mới nhất")


def fetch_now_deliver_later(page, pattern):
    """Let the first GET reach the server immediately, but hold its response back.

    Unlike holding the request, this freezes the *content* at the moment of the read, so a
    later release delivers genuinely stale data regardless of timing.
    """
    held: list = []

    def handler(route):
        if route.request.method == "GET" and not held:
            held.append((route, route.fetch()))
        else:
            route.fallback()

    page.route(re.compile(pattern), handler)
    return held


def test_stale_meeting_read_answering_after_a_save_cannot_roll_the_ui_back(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Before save", "2026-10-14")
    meeting_path = rf"/api/meetings/{meeting_id}$"
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Before save")
    patches = record_patches(page)

    # 1. Start a save (version 1 -> 2) and keep its PATCH from reaching the server.
    held_patch = hold_first(page, meeting_path, "PATCH")
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("After save")
    page.locator("#edit-date").fill("2026-10-20")
    page.get_by_role("button", name="Lưu thay đổi").click()

    # 2. Detour through the calendar; leaving re-reads the meeting. That read hits the
    #    server now, while the title is still "Before save", but its response is held.
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    held_read = fetch_now_deliver_later(page, meeting_path)
    page.get_by_role("button", name="Quay lại notes").click()
    deadline = time.monotonic() + 10
    while not held_read and time.monotonic() < deadline:
        page.wait_for_timeout(50)  # route handlers only run while we are inside Playwright
    assert held_read, "leaving the calendar never re-read the meeting"
    stale_route, stale_response = held_read[0]
    assert stale_response.json()["title"] == "Before save"
    assert stale_response.json()["metadata_version"] == 1

    # 3. Now the save lands and the UI shows it.
    release_when_held(held_patch)
    wait_for_stored_title(context, live, meeting_id, "After save")
    expect(page.locator("#meeting-title")).to_have_text("After save")
    expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-20")

    # 4. The stale (version 1) read finally answers. It must not roll anything back.
    stale_route.fulfill(response=stale_response)
    settle(page)
    expect(page.locator("#meeting-title")).to_have_text("After save")
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")  # the name shows once, below
    expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-20")
    assert page.title() == "After save · ASR Studio"

    # The model kept version 2: the next edit saves cleanly instead of conflicting.
    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    expect(page.locator("#edit-title")).to_have_value("After save")
    expect(page.locator("#edit-date")).to_have_value("2026-10-20")
    page.locator("#edit-title").fill("Third title")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#meeting-title")).to_have_text("Third title")
    expect(page.locator("#edit-conflict")).to_be_hidden()
    assert patches[-1] == {"expected_metadata_version": 2, "title": "Third title"}
    stored = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert (stored["title"], stored["meeting_date"], stored["metadata_version"]) == (
        "Third title",
        "2026-10-20",
        3,
    )


def test_runtime_profile_and_saved_status_render_without_collapsing(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Runtime profile")
    lock_fd = acquire_worker_lock(live.settings.data_dir)
    assert lock_fd is not None
    write_runtime_profile(
        live.settings.data_dir,
        RuntimeProfile(
            device="cuda",
            compute_type="float16",
            model_path="/models/qwen3-asr-1.7b",
        ),
    )
    write_heartbeat(live.settings.data_dir, "idle")
    try:
        page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
        expect(page.locator("#runtime-backend")).to_have_text("GPU · CUDA · bfloat16")
        expect(page.locator("#runtime-model")).to_have_text("qwen3-asr-1.7b")
        expect(page.locator("#status-recording")).to_have_text("Bản ghi · Đã lưu · 1 giây")
        expect(page.locator("#status-transcript")).to_have_text("Transcript · Chưa chép lời")
        for item in page.locator("#meeting-status-strip li").all():
            assert item.bounding_box()["width"] > 80  # an item must not collapse to its dot
    finally:
        os.close(lock_fd)


def test_add_speaker_named_and_blank_names_use_the_real_api(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Real speaker API", duration_seconds=30)
    _publish_browser_generation(live, meeting_id)
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Real speaker API")

    # This test intentionally does not intercept the add-speaker route.
    render_key = page.locator("#transcript-generation").get_attribute("data-render-key")
    generation_id = render_key.split(":", 1)[0]
    speakers_url = f"/transcript-generations/{generation_id}/speakers"
    add_name = page.locator("#speaker-add-name")
    add_name.fill("Browser witness")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(speakers_url)
    ) as named_response_info:
        add_name.press("Enter")
    named_response = named_response_info.value
    assert named_response.status == 200
    assert named_response.request.post_data_json["display_name"] == "Browser witness"
    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(page.locator(".transcript-speaker-add-draft-review")).to_have_count(0)
    named_speaker = page.locator("#speaker-catalog-list .speaker-chip").last
    expect(named_speaker).to_have_text("Browser witness")
    expect(named_speaker).to_be_focused()

    add_name.fill("")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(speakers_url)
    ) as blank_response_info:
        add_name.press("Enter")
    blank_response = blank_response_info.value
    assert blank_response.status == 200
    assert blank_response.request.post_data_json["display_name"] == ""
    assert blank_response.json()["display_name"] == "Speaker 3"
    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(page.locator(".transcript-speaker-add-draft-review")).to_have_count(0)
    blank_speaker = page.locator("#speaker-catalog-list .speaker-chip").last
    expect(blank_speaker.locator(".speaker-chip-name")).to_have_text("Speaker 3")
    expect(blank_speaker.locator(".speaker-chip-mark")).to_have_text("đặt tên")
    expect(blank_speaker).to_be_focused()


def test_add_speaker_current_generation_conflict_can_be_reapplied_to_latest(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Speaker add recovery", "2026-10-14")
    old_generation = {
        "id": "speaker-add-old-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [{"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"}],
        "segments": [
            {
                "id": "speaker-add-old-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Old transcript.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    latest_generation = json.loads(json.dumps(old_generation))
    latest_generation["id"] = "speaker-add-current-generation"
    latest_generation["generation_number"] = 2
    latest_generation["source_version"] = 7
    latest_generation["segments"][0]["id"] = "speaker-add-current-segment"
    current = {"enabled": False}
    add_requests = []

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(
            json.dumps(latest_generation if current["enabled"] else old_generation)
        )
        payload["transcription"]["generation_history"] = (
            [json.loads(json.dumps(old_generation))] if current["enabled"] else []
        )
        route.fulfill(response=response, json=payload)

    def route_add_speaker(route):
        body = route.request.post_data_json
        add_requests.append((route.request.url, body))
        if len(add_requests) == 1:
            current["enabled"] = True
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "current_generation_changed",
                            "message": "The current transcript changed.",
                        }
                    }
                ),
            )
            return
        assert add_requests[-1][0].endswith(
            f"/transcript-generations/{latest_generation['id']}/speakers"
        )
        assert body == {"expected_source_version": 7, "display_name": "Recoverable speaker"}
        latest_generation["source_version"] = 8
        latest_generation["speakers"].append(
            {
                "speaker_id": "MANUAL_RECOVERED",
                "display_name": body["display_name"],
                "first_seen_seconds": 0.0,
                "last_seen_seconds": 2.0,
            }
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "speaker_id": "MANUAL_RECOVERED",
                    "display_name": body["display_name"],
                    "first_seen_seconds": 0.0,
                    "last_seen_seconds": 2.0,
                    "generation_id": latest_generation["id"],
                    "source_version": latest_generation["source_version"],
                }
            ),
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers$"),
        route_add_speaker,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    page.evaluate("""() => {
      const originalFetch = window.fetch.bind(window);
      window.__speakerAddFetchCount = 0;
      window.fetch = (resource, options) => {
        const url = typeof resource === "string" ? resource : resource.url;
        if (url.includes("/transcript-generations/") && url.endsWith("/speakers")) {
          window.__speakerAddFetchCount += 1;
        }
        return originalFetch(resource, options);
      };
    }""")
    add_name = page.locator("#speaker-add-name")
    add_name.fill("Recoverable speaker")
    add_name.press("Enter")

    reload_button = page.locator("#speaker-add-form [data-reload-latest]")
    expect(reload_button).to_be_visible()
    expect(page.locator(".transcript-speaker-add-draft-review")).to_contain_text(
        "Tên cần thêm: Recoverable speaker"
    )
    reapply = page.get_by_role("button", name="Thêm vào transcript hiện tại")
    expect(reapply).to_be_disabled()
    expect(add_name).to_be_disabled()
    submit = page.locator("#speaker-add-form button[type=submit]")
    expect(submit).to_be_disabled()
    page.locator("#speaker-add-form").evaluate("""form => {
      const input = form.querySelector("#speaker-add-name");
      input.disabled = false;
      form.querySelector("button[type=submit]").disabled = false;
      input.focus();
    }""")
    add_name.press("Enter")
    assert page.evaluate("window.__speakerAddFetchCount") == 1
    assert len(add_requests) == 1
    reload_button.click()
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    expect(add_name).to_be_empty()
    recovery = page.locator(".transcript-speaker-add-draft-review")
    expect(recovery).to_contain_text("Tên cần thêm: Recoverable speaker")
    expect(recovery.get_by_role("button", name="Thêm vào transcript hiện tại")).to_be_enabled()
    recovery.get_by_role("button", name="Thêm vào transcript hiện tại").click()

    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(page.locator(".transcript-speaker-add-draft-review")).to_have_count(0)
    expect(
        page.locator('#speaker-catalog-list .speaker-chip[data-speaker-id="MANUAL_RECOVERED"]')
    ).to_have_text("Recoverable speaker")
    assert [body for _url, body in add_requests] == [
        {"expected_source_version": 1, "display_name": "Recoverable speaker"},
        {"expected_source_version": 7, "display_name": "Recoverable speaker"},
    ]


def test_add_speaker_source_conflict_recovers_after_new_generation_with_lower_version(
    live, browser_context
):
    _context, page = browser_context
    meeting_id = seed_meeting(
        live.settings.data_dir, "Speaker add mixed generation race", "2026-10-14"
    )
    source_generation = {
        "id": "speaker-mixed-source-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 10,
        "segment_count": 1,
        "speakers": [{"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"}],
        "segments": [
            {
                "id": "speaker-mixed-source-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Source generation transcript.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    current_generation = json.loads(json.dumps(source_generation))
    current_generation["id"] = "speaker-mixed-current-generation"
    current_generation["generation_number"] = 2
    current_generation["source_version"] = 1
    current_generation["segments"][0]["id"] = "speaker-mixed-current-segment"
    published = {"value": False}
    add_requests = []

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(
            json.dumps(current_generation if published["value"] else source_generation)
        )
        payload["transcription"]["generation_history"] = (
            [json.loads(json.dumps(source_generation))] if published["value"] else []
        )
        route.fulfill(response=response, json=payload)

    def route_add_speaker(route):
        body = route.request.post_data_json
        add_requests.append((route.request.url, body))
        if len(add_requests) == 1:
            published["value"] = True
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "source_version_changed",
                            "message": "The transcript source changed.",
                        }
                    }
                ),
            )
            return
        assert add_requests[-1][0].endswith(
            f"/transcript-generations/{current_generation['id']}/speakers"
        )
        assert body == {"expected_source_version": 1, "display_name": "Race speaker"}
        current_generation["source_version"] = 2
        current_generation["speakers"].append(
            {
                "speaker_id": "MANUAL_RACE",
                "display_name": body["display_name"],
                "first_seen_seconds": 0.0,
                "last_seen_seconds": 2.0,
            }
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "speaker_id": "MANUAL_RACE",
                    "display_name": body["display_name"],
                    "first_seen_seconds": 0.0,
                    "last_seen_seconds": 2.0,
                    "generation_id": current_generation["id"],
                    "source_version": current_generation["source_version"],
                }
            ),
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers$"),
        route_add_speaker,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    page.evaluate("""() => {
      const originalFetch = window.fetch.bind(window);
      window.__speakerAddFetchCount = 0;
      window.fetch = (resource, options) => {
        const url = typeof resource === "string" ? resource : resource.url;
        if (url.includes("/transcript-generations/") && url.endsWith("/speakers")) {
          window.__speakerAddFetchCount += 1;
        }
        return originalFetch(resource, options);
      };
    }""")
    add_name = page.locator("#speaker-add-name")
    add_name.fill("Race speaker")
    add_name.press("Enter")

    expect(page.locator("#speaker-add-form [data-reload-latest]")).to_be_visible()
    expect(add_name).to_be_disabled()
    expect(page.locator("#speaker-add-form button[type=submit]")).to_be_disabled()
    page.locator("#speaker-add-form").evaluate("""form => {
      const input = form.querySelector("#speaker-add-name");
      input.disabled = false;
      form.querySelector("button[type=submit]").disabled = false;
      input.focus();
    }""")
    add_name.press("Enter")
    assert page.evaluate("window.__speakerAddFetchCount") == 1
    assert len(add_requests) == 1

    page.locator("#speaker-add-form [data-reload-latest]").click()
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    expect(add_name).to_be_empty()
    reapply = page.get_by_role("button", name="Thêm vào transcript hiện tại")
    expect(reapply).to_be_enabled()
    reapply.click()

    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(page.locator(".transcript-speaker-add-draft-review")).to_have_count(0)
    assert [body for _url, body in add_requests] == [
        {"expected_source_version": 10, "display_name": "Race speaker"},
        {"expected_source_version": 1, "display_name": "Race speaker"},
    ]


def test_transcript_mutation_focus_restores_semantic_replacements_after_refresh(
    live, browser_context
):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Mutation focus refresh", duration_seconds=30)
    generation = _publish_browser_generation(live, meeting_id)
    generation_id = generation["id"]
    added_speaker = context.request.post(
        f"{live.url}/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers",
        headers={"Origin": live.url},
        data={"expected_source_version": 1, "display_name": "Speaker 2"},
    )
    assert added_speaker.status == 200
    held_mutations = []

    def hold_mutation_response(route):
        response = route.fetch()
        assert response.status == 200
        held_mutations.append((route, response))

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-(?:segments|generations)/"),
        hold_mutation_response,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#transcript-generation")).to_have_attribute(
        "data-render-key", f"{generation_id}:2:true"
    )

    def wait_for_mutation_and_refresh(expected_version, focus_selector, capture_segment=False):
        deadline = time.monotonic() + 10
        while not held_mutations and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held_mutations) == 1
        page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
        expect(page.locator("#transcript-generation")).to_have_attribute(
            "data-render-key", f"{generation_id}:{expected_version}:true"
        )
        assert page.evaluate("() => !window.__focusBeforeRefresh.isConnected")
        assert page.evaluate("() => document.activeElement === document.body")
        replacement = page.locator(focus_selector).first
        if capture_segment:
            page.evaluate(
                "() => { window.__segmentBeforeAssignmentResponse = "
                "document.querySelector('.transcript-segment'); }"
            )
        held_route, held_response = held_mutations.pop()
        held_route.fulfill(response=held_response)
        expect(replacement).to_be_focused()
        return replacement

    # Saving closes the editor, so focus lands on the Sửa button that replaces it.
    text_editor = edit_segment(page)
    text_editor.fill("Saved text focus survives refresh.")
    text_save = page.get_by_role("button", name="Lưu đoạn")
    text_save.focus()
    page.evaluate("() => { window.__focusBeforeRefresh = document.activeElement; }")
    text_save.press("Enter")
    wait_for_mutation_and_refresh(3, ".segment-edit-button")
    expect(page.locator(".segment-text")).to_have_text("Saved text focus survives refresh.")
    expect(page.locator("#transcript-mutation-status")).to_be_visible()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")

    # Likewise a saved name closes its rename and focus lands on the chip.
    speaker_name = rename_speaker(page)
    speaker_name.fill("Saved speaker focus survives refresh.")
    page.evaluate("() => { window.__focusBeforeRefresh = document.activeElement; }")
    speaker_name.press("Enter")
    wait_for_mutation_and_refresh(4, "#speaker-catalog-list > li:first-child .speaker-chip")
    expect(page.locator("#speaker-catalog-list > li:first-child .speaker-chip")).to_contain_text(
        "Saved speaker focus survives refresh."
    )
    expect(page.locator("#transcript-mutation-status")).to_be_visible()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")

    edit_segment(page)
    assignment = page.locator(".segment-speaker-select")
    assignment.focus()
    page.evaluate("() => { window.__focusBeforeRefresh = document.activeElement; }")
    assignment.press("ArrowDown")
    replacement_assignment = wait_for_mutation_and_refresh(
        5, ".segment-speaker-select", capture_segment=True
    )
    expect(replacement_assignment).to_be_focused()
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        f"{generation_id}:5:true"
    )
    assert page.evaluate(
        "() => window.__segmentBeforeAssignmentResponse === "
        "document.querySelector('.transcript-segment')"
    )


def test_delayed_text_and_name_errors_target_replacement_statuses(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(
        live.settings.data_dir, "Mutation error status refresh", duration_seconds=30
    )
    _publish_browser_generation(live, meeting_id)
    generation = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()[
        "transcription"
    ]["generation"]
    held = []

    def hold_selected_saves(route):
        body = route.request.post_data_json
        text_save = route.request.url.endswith(
            f"transcript-segments/{generation['segments'][0]['id']}/text"
        )
        name_save = route.request.url.endswith(
            f"transcript-generations/{generation['id']}/speakers/SPEAKER_00"
        )
        if (text_save and body["text"] == "Delayed text failure.") or (
            name_save and body["display_name"] == "Delayed name failure."
        ):
            held.append(route)
            return
        route.fallback()

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-(?:segments|generations)/"),
        hold_selected_saves,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#transcript-generation")).to_have_attribute(
        "data-render-key", f"{generation['id']}:1:true"
    )

    def wait_for_held_request():
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1

    def fail_after_refresh(status_selector, expected_version, fulfill_error):
        old_status = page.locator(status_selector)
        old_status.evaluate("element => { window.__oldMutationStatus = element; }")
        page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
        expect(page.locator("#transcript-generation")).to_have_attribute(
            "data-render-key", f"{generation['id']}:{expected_version}:true"
        )
        assert page.evaluate("() => !window.__oldMutationStatus.isConnected")
        replacement = page.locator(status_selector).first
        held_route = held.pop()
        held_route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": {"message": fulfill_error}}),
        )
        expect(replacement).to_have_text("Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau.")
        expect(page.locator("#transcript-mutation-status")).to_be_visible()
        expect(page.locator("#transcript-mutation-status")).to_have_text(
            "Máy chủ chưa thể hoàn tất yêu cầu. Hãy thử lại sau."
        )

    text = edit_segment(page)
    text.fill("Delayed text failure.")
    page.get_by_role("button", name="Lưu đoạn").click()
    wait_for_held_request()
    external_text = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-segments/{generation['segments'][0]['id']}/text",
        headers={"Origin": live.url},
        data={
            "expected_generation_id": generation["id"],
            "expected_source_version": 1,
            "text": "Saved elsewhere before delayed error.",
        },
    )
    assert external_text.status == 200
    fail_after_refresh(".segment-edit-status", 2, "Synthetic delayed text save failure.")

    name = rename_speaker(page)
    name.fill("Delayed name failure.")
    page.locator("#speaker-catalog-list .speaker-name-form button[type=submit]").first.click()
    wait_for_held_request()
    external_name = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-generations/{generation['id']}/speakers/SPEAKER_00",
        headers={"Origin": live.url},
        data={"expected_source_version": 2, "display_name": "Saved name elsewhere."},
    )
    assert external_name.status == 200
    fail_after_refresh(
        "#speaker-catalog-list .speaker-form-status",
        3,
        "Synthetic delayed speaker name failure.",
    )


def test_same_meeting_refresh_preserves_generation_missing_from_stale_snapshot(
    live, browser_context
):
    _context, page = browser_context
    meeting_id = seed_meeting(
        live.settings.data_dir, "Missing generation snapshot", duration_seconds=30
    )
    held_prepublication_read = []

    def hold_first_read(route):
        if not held_prepublication_read:
            held_prepublication_read.append((route, route.fetch()))
        else:
            route.continue_()

    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Missing generation snapshot")
    page.route(f"{live.url}/api/meetings/{meeting_id}", hold_first_read)
    page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
    deadline = time.monotonic() + 10
    while not held_prepublication_read and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_prepublication_read) == 1
    stale_route, stale_response = held_prepublication_read[0]
    assert stale_response.json()["transcription"]["generation"] is None

    generation = _publish_browser_generation(live, meeting_id)
    page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(page.locator(".segment-text")).to_have_text("A saved sentence.")

    stale_route.fulfill(response=stale_response)
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(page.locator(".segment-text")).to_have_text("A saved sentence.")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        f"{generation['id']}:1:true"
    )


def test_speaker_add_name_clears_on_meeting_and_generation_switches(live, browser_context):
    context, page = browser_context
    first_meeting = seed_meeting(
        live.settings.data_dir, "Speaker name scope one", duration_seconds=30
    )
    second_meeting = seed_meeting(live.settings.data_dir, "Speaker name scope two", "2026-10-18")
    _publish_browser_generation(live, first_meeting)
    generation = context.request.get(f"{live.url}/api/meetings/{first_meeting}").json()[
        "transcription"
    ]["generation"]
    history_generation = json.loads(json.dumps(generation))
    history_generation["id"] = "browser-history-generation"
    history_generation["generation_number"] = 0
    history_generation["segments"][0]["id"] = "b" * 32
    next_generation = json.loads(json.dumps(generation))
    next_generation["id"] = "browser-next-generation"
    next_generation["generation_number"] = 2
    next_generation["segments"][0]["id"] = "c" * 32
    publish_next_generation = {"enabled": False}

    def route_first_meeting(route):
        response = route.fetch()
        payload = response.json()
        transcription = payload["transcription"]
        current = next_generation if publish_next_generation["enabled"] else generation
        transcription["generation"] = json.loads(json.dumps(current))
        history = (
            [generation, history_generation] if current is next_generation else [history_generation]
        )
        transcription["generation_history"] = json.loads(json.dumps(history))
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{first_meeting}", route_first_meeting)
    page.goto(f"{live.url}/meetings/{first_meeting}", wait_until="domcontentloaded")
    add_name = page.locator("#speaker-add-name")
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    add_name.fill("Unsent for history switch")
    page.locator("#generation-selector").select_option(history_generation["id"])
    expect(add_name).to_be_empty()
    page.locator("#generation-selector").select_option(generation["id"])

    add_name.fill("Unsent for current publication")
    publish_next_generation["enabled"] = True
    page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    expect(add_name).to_be_empty()

    add_name.fill("Unsent for meeting switch")
    page.locator("#recent-list a").filter(has_text="Speaker name scope two").click()
    page.wait_for_url(f"{live.url}/meetings/{second_meeting}")
    expect(page.locator("#meeting-title")).to_have_text("Speaker name scope two")
    expect(add_name).to_be_empty()


def test_transcription_requires_explicit_start_and_renders_poll_cancel_and_rerun(
    live, browser_context
):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Transcript controls", duration_seconds=30)
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Transcript controls")
    post_bodies: list[dict[str, object]] = []
    tasks: dict[str, dict[str, object]] = {}
    current_generation: dict[str, object] | None = None
    generation_history: list[dict[str, object]] = []
    late_text_responses: list[tuple[object, dict[str, object], dict[str, object]]] = []
    late_speaker_responses: list[tuple[object, dict[str, object]]] = []
    late_add_responses: list[tuple[object, dict[str, object]]] = []
    late_add_snapshot: dict[str, object] | None = None
    add_speaker_requests: list[dict[str, object]] = []
    task_number = 0

    def task_response(
        task_id,
        status,
        *,
        request_id="browser-request-id",
        worker_state=None,
        segment_count=1,
        end_seconds=4.25,
    ):
        return {
            "id": task_id,
            "request_id": request_id,
            "meeting_id": meeting_id,
            "kind": "transcription",
            "status": status,
            "worker_state": worker_state
            or ("healthy" if status in {"running", "stopping"} else "idle"),
            "stage": "transcribing" if status == "running" else status,
            "result_scope": "transcript_only" if status == "succeeded" else None,
            "audio_duration_seconds": 30,
            "transcript_end_seconds": end_seconds,
            "segment_count": segment_count,
            "elapsed_seconds": 2.5,
            "error_code": None,
            "error_message": None,
            "accepted_at": "2026-10-17 00:00:00",
            "started_at": "2026-10-17 00:00:00",
            "heartbeat_at": "2026-10-17 00:00:02",
            "cancel_requested_at": None,
            "completed_at": None,
            "updated_at": "2026-10-17 00:00:02",
        }

    def generation_response(number):
        return {
            "id": f"generation-{number}",
            "generation_number": number,
            "status": "complete",
            "language_hint": "en",
            "model_name": "whisper-large-v3",
            "language_probability": 0.94,
            "source_version": 1,
            "uncertainty_policy": "faster-whisper-review-cues-v1",
            "diarization_model_name": "Community-1",
            "segment_count": 1,
            "completed_at": "2026-10-17 00:00:05",
            "speakers": [
                {
                    "speaker_id": "SPEAKER_00",
                    "display_name": "Speaker 1",
                    "first_seen_seconds": 1.0,
                    "last_seen_seconds": 5.0,
                },
                {
                    "speaker_id": "SPEAKER_01",
                    "display_name": "Speaker 2",
                    "first_seen_seconds": 5.0,
                    "last_seen_seconds": 8.0,
                },
            ],
            "segments": [
                {
                    "id": f"{number:032x}",
                    "sequence": 0,
                    "start_seconds": 1.25,
                    "end_seconds": 4.25,
                    "text": f"Transcript generation {number}.",
                    "original_speaker_id": "SPEAKER_00",
                    "speaker_id": "SPEAKER_00",
                    "avg_logprob": -0.3,
                    "no_speech_prob": 0.02,
                    "compression_ratio": 0.8,
                    "uncertain": False,
                    "review_reasons": [],
                    "overlap": False,
                    "speaker_dominance": 1.0,
                    "assignment_version": 1,
                }
            ],
        }

    def route_task_requests(route):
        nonlocal current_generation, generation_history, task_number
        path = route.request.url.removeprefix(live.url)
        if route.request.method == "POST" and path == "/api/tasks":
            post_bodies.append(route.request.post_data_json)
            if len(post_bodies) == 1:
                route.abort("connectionfailed")
                return
            if len(post_bodies) == 4:
                route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps(
                        {"detail": {"code": "system_busy", "message": "Slot is busy."}}
                    ),
                )
                return
            task_number += 1
            task_id = f"browser-task-{task_number}"
            request_id = post_bodies[-1]["request_id"]
            tasks[task_id] = {"polls": 0, "status": "running", "request_id": request_id}
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    task_response(
                        task_id,
                        "running",
                        request_id=request_id,
                        worker_state="checking-worker",
                    )
                ),
            )
            return
        if route.request.method == "POST" and path.endswith("/cancel"):
            task_id = path.split("/")[-2]
            tasks[task_id]["status"] = "canceled"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    task_response(
                        task_id,
                        "stopping",
                        request_id=tasks[task_id]["request_id"],
                    )
                ),
            )
            return
        if route.request.method == "GET" and path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            task = tasks[task_id]
            if task["status"] == "canceled":
                payload = task_response(task_id, "canceled", request_id=task["request_id"])
            else:
                task["polls"] = int(task["polls"]) + 1
                polls_until_complete = 100 if task_id == "browser-task-3" else 2
                if task["polls"] >= polls_until_complete:
                    task["status"] = "succeeded"
                    if current_generation:
                        generation_history = [current_generation, *generation_history]
                    current_generation = generation_response(task_number)
                payload = task_response(task_id, str(task["status"]), request_id=task["request_id"])
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if (
            route.request.method == "GET"
            and path == f"/api/meetings/{meeting_id}"
            and current_generation
        ):
            response = route.fetch()
            payload = response.json()
            payload["transcription"]["generation"] = late_add_snapshot or current_generation
            payload["transcription"]["generation_history"] = generation_history
            payload["transcription"]["task"] = task_response(
                f"browser-task-{task_number}",
                "succeeded",
                request_id=post_bodies[-1]["request_id"],
            )
            route.fulfill(response=response, json=payload)
            return
        route.fallback()

    page.route(
        re.compile(r"/api/(?:tasks(?:/[^/]+(?:/cancel)?)?|meetings/[^/]+)$"), route_task_requests
    )
    speaker_updates = []
    held_speaker_update = []

    def route_speaker_update(route):
        speaker_updates.append(route.request.post_data_json)
        segment_id = route.request.url.rsplit("/", 2)[-2]
        segment = next(
            segment for segment in current_generation["segments"] if segment["id"] == segment_id
        )
        segment["speaker_id"] = speaker_updates[-1]["speaker_id"]
        segment["assignment_version"] += 1
        current_generation["source_version"] = speaker_updates[-1]["expected_source_version"] + 1
        response = {
            "id": segment_id,
            "generation_id": speaker_updates[-1]["expected_generation_id"],
            "original_speaker_id": segment["original_speaker_id"],
            "speaker_id": speaker_updates[-1]["speaker_id"],
            "assignment_version": segment["assignment_version"],
            "source_version": current_generation["source_version"],
        }
        if len(speaker_updates) == 1:
            held_speaker_update.append((route, response))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"), route_speaker_update
    )
    text_updates: list[dict[str, object]] = []

    def route_text_update(route):
        body = route.request.post_data_json
        text_updates.append(body)
        segment_id = route.request.url.rsplit("/", 2)[-2]
        segment = next(
            segment for segment in current_generation["segments"] if segment["id"] == segment_id
        )
        if body["text"] == "Saved after refresh.":
            late_text_responses.append((route, body, segment))
            return
        if body["text"] == "Stale draft retained in editor.":
            segment["text"] = "Concurrent saved text."
            current_generation["source_version"] = body["expected_source_version"] + 1
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "source_version_changed",
                            "message": "The transcript source changed.",
                        }
                    }
                ),
            )
            return
        segment["text"] = body["text"]
        current_generation["source_version"] = body["expected_source_version"] + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": segment_id,
                    "generation_id": body["expected_generation_id"],
                    "text": body["text"],
                    "source_version": current_generation["source_version"],
                }
            ),
        )

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/text$"),
        route_text_update,
    )

    speaker_catalog_updates: list[dict[str, object]] = []

    def route_speaker_catalog_update(route):
        body = route.request.post_data_json
        speaker_catalog_updates.append(body)
        speaker_id = route.request.url.rsplit("/", 1)[-1]
        speaker = next(
            speaker
            for speaker in current_generation["speakers"]
            if speaker["speaker_id"] == speaker_id
        )
        if body["display_name"] == "Unsubmitted speaker name.":
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "current_generation_changed",
                            "message": "The current transcript changed.",
                        }
                    }
                ),
            )
            return
        speaker["display_name"] = body["display_name"] or (
            "Speaker 1" if speaker_id == "SPEAKER_00" else "Speaker 2"
        )
        current_generation["source_version"] = body["expected_source_version"] + 1
        response = {
            "generation_id": body.get("expected_generation_id", "generation-1"),
            "speaker_id": speaker_id,
            "display_name": speaker["display_name"],
            "source_version": current_generation["source_version"],
        }
        if body["display_name"] == "Saved before refresh.":
            late_speaker_responses.append((route, response))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers/[^/]+$"),
        route_speaker_catalog_update,
    )

    def route_add_speaker(route):
        nonlocal late_add_snapshot
        body = route.request.post_data_json
        add_speaker_requests.append(body)
        display_name = body.get("display_name") or "Speaker 3"
        speaker_id = "MANUAL_browser" if display_name == "Witness" else "MANUAL_late_browser"
        if display_name == "Late witness":
            late_add_snapshot = json.loads(json.dumps(current_generation))
        current_generation["speakers"].append(
            {
                "speaker_id": speaker_id,
                "display_name": display_name,
                "first_seen_seconds": 0,
                "last_seen_seconds": 0,
            }
        )
        current_generation["source_version"] = body["expected_source_version"] + 1
        response = {
            "generation_id": current_generation["id"],
            "speaker_id": speaker_id,
            "display_name": display_name,
            "first_seen_seconds": 0,
            "last_seen_seconds": 0,
            "source_version": current_generation["source_version"],
        }
        if display_name == "Late witness":
            late_add_responses.append((route, response))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers$"),
        route_add_speaker,
    )
    assert post_bodies == []  # Reopening an imported meeting never starts ASR.
    start_button = page.get_by_role("button", name="Bắt đầu chép lời")
    expect(start_button).to_be_visible()
    assert "%" not in page.locator("#transcription-panel").inner_text()
    assert "ETA" not in page.locator("#transcription-panel").inner_text()

    start_button.click()
    expect(page.locator("#transcription-error")).to_be_visible(timeout=5_000)
    expect(start_button).to_be_visible()
    start_button.click()
    expect(page.locator("#transcription-status")).to_contain_text("Đang kiểm tra worker")
    expect(page.locator("#transcription-status")).to_have_attribute(
        "data-worker-state", "checking-worker"
    )
    expect(page.locator("#transcript-generation")).to_be_visible(timeout=8_000)
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(page.locator(".segment-text")).to_have_text("Transcript generation 1.")
    expect(page.locator(".segment-timestamp")).to_have_text("0:01.25")
    assert "/api/meetings/" + meeting_id + "/audio" in page.locator("#meeting-audio").get_attribute(
        "src"
    )
    page.locator("#meeting-audio").evaluate(
        "audio => Object.defineProperty(audio, 'currentTime', "
        "{configurable: true, writable: true, value: 0})"
    )
    page.locator(".segment-timestamp").click()
    expect(page.locator(".transcript-segment")).to_have_attribute("aria-current", "true")
    assert page.locator("#meeting-audio").evaluate("audio => audio.currentTime") == 1.25
    edit_segment(page)
    speaker_select = page.locator(".segment-speaker-select")
    speaker_select.focus()
    speaker_select.press("ArrowDown")
    expect(page.locator("#transcript-generation")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#transcript-mutation-status")).to_have_text(
        "Đang lưu thay đổi transcript."
    )
    deadline = time.monotonic() + 10
    while not held_speaker_update and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_speaker_update) == 1
    held_route, held_response = held_speaker_update.pop()
    held_route.fulfill(status=200, content_type="application/json", body=json.dumps(held_response))
    expect(speaker_select).to_have_value("SPEAKER_01")
    expect(speaker_select).to_have_attribute("data-assignment-version", "2")
    expect(speaker_select).to_be_focused()
    expect(page.locator("#transcript-generation")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#transcript-mutation-status")).to_be_visible()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu lựa chọn người nói.")
    expect(page.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Speaker 1")
    expect(page.locator("#transcript-generation")).to_have_attribute(
        "data-render-key", "generation-1:2:true"
    )
    assert speaker_updates == [
        {
            "expected_generation_id": "generation-1",
            "expected_source_version": 1,
            "expected_assignment_version": 1,
            "speaker_id": "SPEAKER_01",
        }
    ]
    page.locator("#transcript-segments").evaluate(
        "list => { window.__transcriptListBeforeAssignmentRefresh = list; }"
    )
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_be_focused()
    assert page.evaluate(
        "window.__transcriptListBeforeAssignmentRefresh === "
        "document.querySelector('#transcript-segments')"
    )
    expect(page.locator(".segment-speaker-select")).to_have_value("SPEAKER_01")

    edited_html = '<img src=x onerror="window.__transcriptXss = true"> Corrected words.'
    text_editor = page.locator(".segment-text-editor")
    text_editor.fill(edited_html)
    text_editor.focus()
    text_editor.press("Tab")
    expect(page.get_by_role("button", name="Lưu đoạn")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")
    expect(page.locator(".segment-edit-button")).to_be_focused()
    expect(page.locator(".segment-text")).to_have_text(edited_html)
    expect(page.locator(".segment-text-editor")).to_have_count(0)
    expect(page.locator(".transcript-segment img")).to_have_count(0)
    assert text_updates == [
        {
            "expected_generation_id": "generation-1",
            "expected_source_version": 2,
            "text": edited_html,
        }
    ]

    edit_segment(page)
    speaker_name = page.locator("#speaker-catalog-list .speaker-name-form input")
    rename_speaker(page).fill("Alice")
    page.locator("#speaker-catalog-list button[type=submit]").first.click()
    expect(page.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Alice")
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")
    expect(page.locator("#speaker-catalog-list .speaker-chip").first).to_have_text("Alice")
    rename_speaker(page)
    page.get_by_role("button", name="Khôi phục tên mặc định").first.click()
    expect(page.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Speaker 1")
    assert [entry["expected_source_version"] for entry in speaker_catalog_updates] == [3, 4]

    add_speaker_name = page.locator("#speaker-add-name")
    add_speaker_name.fill("Witness")
    add_speaker_name.press("Enter")
    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(speaker_select.locator('option[value="MANUAL_browser"]')).to_have_text("Witness")
    speaker_select.select_option("MANUAL_browser")
    expect(speaker_select).to_have_attribute("data-assignment-version", "3")
    assert speaker_updates[-1]["expected_source_version"] == 6
    assert speaker_updates[-1]["speaker_id"] == "MANUAL_browser"

    # A plain server refresh must surface changed fields before it can advance the
    # source version used by a draft save.
    refreshed_text_draft = "Draft text before refresh."
    refreshed_speaker_draft = "Draft speaker before refresh."
    text_editor.fill(refreshed_text_draft)
    rename_speaker(page).fill(refreshed_speaker_draft)
    current_generation["segments"][0]["text"] = "Concurrent text from another tab."
    current_generation["speakers"][0]["display_name"] = "Concurrent speaker name."
    current_generation["source_version"] += 1
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    text_editor = page.locator(".segment-text-editor")
    expect(text_editor).to_have_value(refreshed_text_draft)
    expect(page.locator("#transcript-segments .transcript-latest-value")).to_have_text(
        "Bản mới nhất đã lưu: Concurrent text from another tab."
    )
    expect(page.locator("#speaker-catalog-list .transcript-latest-value")).to_have_text(
        "Bản mới nhất đã lưu: Concurrent speaker name."
    )
    page.get_by_role("button", name="Lưu đoạn").click()
    expect(page.locator(".segment-edit-status")).to_contain_text("chỉnh bản nháp trước khi lưu")
    assert len(text_updates) == 1
    page.locator("#speaker-catalog-list button[type=submit]").first.click()
    expect(page.locator("#speaker-catalog-list .speaker-form-status").first).to_contain_text(
        "chỉnh bản nháp trước khi lưu"
    )
    assert len(speaker_catalog_updates) == 2

    text_editor.fill("Reviewed transcript draft.")
    page.get_by_role("button", name="Lưu đoạn").click()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")
    expect(page.locator(".segment-text")).to_have_text("Reviewed transcript draft.")
    speaker_name.fill("Reviewed speaker name.")
    page.locator("#speaker-catalog-list button[type=submit]").first.click()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")
    assert text_updates[-1]["expected_source_version"] == 8
    assert speaker_catalog_updates[-1]["expected_source_version"] == 9

    # Let a successful save request wait across a refresh that still shows the
    # pre-save value. Its eventual response must reconcile the retained draft.
    text_editor = edit_segment(page)
    text_editor.fill("Saved after refresh.")
    page.get_by_role("button", name="Lưu đoạn").click()
    deadline = time.monotonic() + 10
    while not late_text_responses and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(late_text_responses) == 1
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator(".segment-text-editor")).to_have_value("Saved after refresh.")
    held_text_route, held_text_body, held_segment = late_text_responses[0]
    held_segment["text"] = held_text_body["text"]
    current_generation["source_version"] = held_text_body["expected_source_version"] + 1
    held_text_route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "id": held_segment["id"],
                "generation_id": held_text_body["expected_generation_id"],
                "text": held_text_body["text"],
                "source_version": current_generation["source_version"],
            }
        ),
    )

    stale_draft = "Stale draft retained in editor."
    stale_speaker_draft = "Unsubmitted speaker name."
    rename_speaker(page).fill(stale_speaker_draft)
    text_editor = edit_segment(page)
    text_editor.fill(stale_draft)
    text_editor.focus()
    text_editor.press("Tab")
    page.keyboard.press("Enter")
    expect(page.locator(".segment-edit-status")).to_contain_text("Bản nháp vẫn được giữ")
    expect(text_editor).to_have_value(stale_draft)
    assert len(text_updates) == 4  # No stale write is retried automatically.
    reload_latest = page.get_by_role("button", name="Tải lại bản mới nhất")
    expect(reload_latest).to_be_visible()
    reload_latest.click()
    expect(text_editor).to_have_value(stale_draft)
    expect(page.locator(".transcript-latest-value")).to_have_text(
        "Bản mới nhất đã lưu: Concurrent saved text."
    )
    expect(speaker_name).to_have_value(stale_speaker_draft)
    page.locator("#speaker-catalog-list button[type=submit]").first.click()
    expect(page.locator("#speaker-catalog-list .speaker-form-status").first).to_contain_text(
        "Bản nháp vẫn được giữ"
    )
    reload_latest = page.get_by_role("button", name="Tải lại bản mới nhất")
    expect(reload_latest).to_be_visible()
    reload_latest.click()
    expect(speaker_name).to_have_value(stale_speaker_draft)
    expect(page.locator("#speaker-catalog-list .transcript-latest-value")).to_have_text(
        "Bản mới nhất đã lưu: Reviewed speaker name."
    )
    assert len(text_updates) == 4  # Reloading never retries the rejected text save.
    assert post_bodies[0]["request_id"] == post_bodies[1]["request_id"]

    # The successful name save commits before its response, then the task refresh
    # replaces the meeting object. Its late response must not leave a recovery draft.
    second_speaker_name = rename_speaker(page, 1)
    expect(
        page.locator("#speaker-catalog-list > li").first.locator(".speaker-chip-mark")
    ).to_have_text("bản nháp")
    second_speaker_name.fill("Saved before refresh.")
    page.locator("#speaker-catalog-list button[type=submit]").click()
    deadline = time.monotonic() + 10
    while not late_speaker_responses and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(late_speaker_responses) == 1
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(second_speaker_name).to_have_value("Saved before refresh.")
    held_speaker_route, held_speaker_response = late_speaker_responses[0]
    held_speaker_route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(held_speaker_response),
    )
    expect(page.get_by_role("button", name="Lưu đoạn")).to_be_enabled()

    # The server commits this add, but its response waits while a refresh installs
    # a pre-add snapshot for the same generation.
    add_speaker_name = page.locator("#speaker-add-name")
    add_speaker_name.fill("Late witness")
    add_speaker_name.press("Enter")
    deadline = time.monotonic() + 10
    while not late_add_responses and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(late_add_responses) == 1
    assert len(add_speaker_requests) == 2
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#speaker-catalog-list")).not_to_contain_text("Late witness")
    held_add_route, held_add_response = late_add_responses.pop()
    held_add_route.fulfill(
        status=200, content_type="application/json", body=json.dumps(held_add_response)
    )
    late_add_snapshot = None
    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(page.locator("#speaker-add-name")).to_be_empty()
    expect(page.locator("#speaker-catalog-list .speaker-chip").last).to_have_text("Late witness")
    expect(
        page.locator('.segment-speaker-select option[value="MANUAL_late_browser"]')
    ).to_have_text("Late witness")
    assert len(add_speaker_requests) == 2  # A late success does not retry the add.
    assert (
        sum(
            speaker["speaker_id"] == "MANUAL_late_browser"
            for speaker in current_generation["speakers"]
        )
        == 1
    )

    rerun = page.get_by_role("button", name="Chạy lại bản ghi")
    rerun.click()
    speaker_select.focus()
    page.wait_for_timeout(1_200)
    expect(speaker_select).to_be_focused()
    expect(page.locator("#generation-title")).to_have_text("Transcript #2", timeout=8_000)
    expect(page.locator("#transcript-draft-recovery")).to_be_visible()
    expect(page.locator("#transcript-draft-recovery .transcript-draft-value")).to_have_text(
        [f"Bản nháp chưa lưu: {stale_draft}", f"Bản nháp chưa lưu: {stale_speaker_draft}"]
    )
    page.locator("#generation-selector").select_option("generation-1")
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(page.locator("#generation-mode")).to_have_text("Bản lịch sử · Chỉ đọc.")
    expect(page.locator(".segment-text")).to_have_text("Concurrent saved text.")
    expect(page.locator(".transcript-draft-value")).to_have_text(
        [f"Bản nháp chưa lưu: {stale_speaker_draft}", f"Bản nháp chưa lưu: {stale_draft}"]
    )
    expect(page.get_by_role("button", name="Sao chép bản nháp")).to_have_count(2)
    page.evaluate(
        """() => Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: { writeText: async text => { window.__copiedTranscriptDraft = text; } },
        })"""
    )
    copy_segment_draft = page.get_by_role("button", name="Sao chép bản nháp").last
    copy_segment_draft.click()
    expect(page.locator(".transcript-draft-copy-status").last).to_have_text("Đã sao chép bản nháp.")
    assert page.evaluate("window.__copiedTranscriptDraft") == stale_draft
    assert "img" not in page.locator("#transcript-generation").inner_html()
    expect(page.locator(".transcript-segment img")).to_have_count(0)
    expect(page.locator(".segment-text-editor")).to_have_count(0)
    expect(page.locator(".segment-speaker-select")).to_have_count(0)
    expect(page.locator("#speaker-add-form")).to_be_hidden()
    page.locator("#generation-selector").select_option("generation-2")
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    assert post_bodies[2]["request_id"] != post_bodies[1]["request_id"]

    rerun.click()
    expect(page.locator("#transcription-error")).to_be_visible(timeout=5_000)
    rerun.click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
    assert post_bodies[3]["request_id"] != post_bodies[2]["request_id"]
    assert post_bodies[4]["request_id"] != post_bodies[3]["request_id"]

    recovery_copy = page.get_by_role("button", name="Sao chép bản nháp").first
    recovery_copy.evaluate("button => { window.__recoveryCopyButton = button; button.focus(); }")
    expect(recovery_copy).to_be_focused()
    deadline = time.monotonic() + 10
    while tasks["browser-task-3"]["polls"] < 1 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert tasks["browser-task-3"]["polls"] >= 1
    assert page.evaluate(
        "window.__recoveryCopyButton === document.activeElement && "
        "window.__recoveryCopyButton.isConnected"
    )
    page.evaluate(
        """() => {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: async () => { throw new Error('clipboard unavailable'); } },
          });
          Object.defineProperty(document, 'execCommand', {
            configurable: true,
            value: () => false,
          });
        }"""
    )
    recovery_copy.click()
    expect(page.locator(".transcript-draft-copy-status").first).to_have_text(
        "Bản nháp đã được chọn. Nhấn Ctrl+C hoặc ⌘C để sao chép."
    )
    selected_recovery_text = f"Bản nháp chưa lưu: {stale_draft}"
    assert page.evaluate("window.getSelection().toString()") == selected_recovery_text
    deadline = time.monotonic() + 10
    while tasks["browser-task-3"]["polls"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert tasks["browser-task-3"]["polls"] >= 2
    expect(page.locator(".transcript-draft-copy-status").first).to_have_text(
        "Bản nháp đã được chọn. Nhấn Ctrl+C hoặc ⌘C để sao chép."
    )
    assert page.evaluate("window.getSelection().toString()") == selected_recovery_text
    recovery_copy.focus()
    deadline = time.monotonic() + 10
    while tasks["browser-task-3"]["polls"] < 3 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert tasks["browser-task-3"]["polls"] >= 3
    expect(recovery_copy).to_be_focused()
    expect(page.locator(".transcript-draft-copy-status").first).to_have_text(
        "Bản nháp đã được chọn. Nhấn Ctrl+C hoặc ⌘C để sao chép."
    )
    assert page.evaluate("window.getSelection().toString()") == selected_recovery_text

    page.get_by_role("button", name="Hủy chép lời").click()
    expect(page.locator("#transcription-status")).to_contain_text("Đã hủy", timeout=5_000)
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    assert len(post_bodies) == 5


def test_transcript_reload_cannot_replace_a_newer_successful_save(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Reload mutation race", "2026-10-14")
    server_generation = {
        "id": "reload-race-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [
            {"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"},
            {"speaker_id": "SPEAKER_01", "display_name": "Speaker 2"},
        ],
        "segments": [
            {
                "id": "reload-race-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Original race text.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    hold_reload = {"enabled": False}
    held_reload = []
    held_speaker_save = []

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(json.dumps(server_generation))
        payload["transcription"]["generation_history"] = []
        if hold_reload["enabled"]:
            held_reload.append((route, response, payload))
            return
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/text$"),
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": "source_version_changed",
                        "message": "The transcript source changed.",
                    }
                }
            ),
        ),
    )

    def route_speaker_save(route):
        body = route.request.post_data_json
        if body["display_name"] == "Held speaker save.":
            held_speaker_save.append((route, body))
            return
        speaker_id = route.request.url.rsplit("/", 1)[-1]
        speaker = next(
            item for item in server_generation["speakers"] if item["speaker_id"] == speaker_id
        )
        speaker["display_name"] = body["display_name"]
        server_generation["source_version"] = body["expected_source_version"] + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generation_id": body.get("expected_generation_id", server_generation["id"]),
                    "speaker_id": speaker_id,
                    "display_name": speaker["display_name"],
                    "source_version": server_generation["source_version"],
                }
            ),
        )

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers/[^/]+$"),
        route_speaker_save,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    editor = edit_segment(page)
    editor.fill("Keep this draft while loading.")
    page.get_by_role("button", name="Lưu đoạn").click()
    expect(page.locator(".segment-edit-status")).to_contain_text("Bản nháp vẫn được giữ")
    reload_button = page.get_by_role("button", name="Tải lại bản mới nhất")
    expect(reload_button).to_be_visible()

    first_speaker_input = rename_speaker(page)
    first_speaker_input.fill("Held speaker save.")
    page.locator("#speaker-catalog-list button[type=submit]").first.click()
    deadline = time.monotonic() + 10
    while not held_speaker_save and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_speaker_save) == 1
    expect(reload_button).to_be_disabled()
    held_speaker_route, held_speaker_body = held_speaker_save.pop()
    server_generation["speakers"][0]["display_name"] = held_speaker_body["display_name"]
    server_generation["source_version"] = held_speaker_body["expected_source_version"] + 1
    held_speaker_route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "generation_id": server_generation["id"],
                "speaker_id": "SPEAKER_00",
                "display_name": server_generation["speakers"][0]["display_name"],
                "source_version": server_generation["source_version"],
            }
        ),
    )
    expect(page.locator("#speaker-catalog-list .speaker-chip").first).to_have_text(
        "Held speaker save."
    )
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")
    expect(reload_button).to_be_enabled()

    hold_reload["enabled"] = True
    reload_button.click()
    deadline = time.monotonic() + 10
    while not held_reload and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_reload) == 1
    held_reload_route, held_reload_response, held_reload_payload = held_reload.pop()
    assert held_reload_payload["transcription"]["generation"]["source_version"] == 2

    second_speaker_input = rename_speaker(page, 1)
    second_speaker_input.fill("Saved after reload began.")
    page.locator("#speaker-catalog-list button[type=submit]").click()
    expect(page.locator("#speaker-catalog-list .speaker-chip").nth(1)).to_have_text(
        "Saved after reload began."
    )
    assert server_generation["source_version"] == 3
    expect(reload_button).to_be_disabled()

    hold_reload["enabled"] = False
    held_reload_route.fulfill(response=held_reload_response, json=held_reload_payload)
    expect(page.locator("#speaker-catalog-list .speaker-chip").nth(1)).to_have_text(
        "Saved after reload began."
    )
    expect(editor).to_have_value("Keep this draft while loading.")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        "reload-race-generation:3:true"
    )
    expect(page.get_by_role("button", name="Tải lại bản mới nhất")).to_have_count(0)


def test_meeting_install_preserves_newer_transcript_after_metadata_and_same_meeting_load(
    live, browser_context
):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Meeting install race", "2026-10-14")
    server_generation = {
        "id": "meeting-install-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [],
        "segments": [
            {
                "id": "meeting-install-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Original meeting transcript.",
                "original_speaker_id": None,
                "speaker_id": None,
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    stale_generation = json.loads(json.dumps(server_generation))
    held_metadata_save = []
    stale_meeting_load = {"enabled": False}

    def inject_generation(route, generation):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(json.dumps(generation))
        payload["transcription"]["generation_history"] = []
        return response, payload

    def route_meeting(route):
        if route.request.method == "PATCH":
            response = route.fetch()
            if response.status != 200:
                route.fulfill(response=response)
                return
            payload = response.json()
            payload["transcription"]["generation"] = json.loads(json.dumps(stale_generation))
            payload["transcription"]["generation_history"] = []
            if not held_metadata_save:
                held_metadata_save.append((route, response, payload))
                return
            route.fulfill(response=response, json=payload)
            return
        response, payload = inject_generation(
            route,
            stale_generation if stale_meeting_load["enabled"] else server_generation,
        )
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    text_saves = []

    def route_text_save(route):
        body = route.request.post_data_json
        text_saves.append(body)
        segment = server_generation["segments"][0]
        segment["text"] = body["text"]
        server_generation["source_version"] = body["expected_source_version"] + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": segment["id"],
                    "generation_id": body["expected_generation_id"],
                    "text": segment["text"],
                    "source_version": server_generation["source_version"],
                }
            ),
        )

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/text$"),
        route_text_save,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    page.locator("#meeting-actions").click()

    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("Metadata saved during transcript edit")
    page.get_by_role("button", name="Lưu thay đổi").click()
    deadline = time.monotonic() + 10
    while not held_metadata_save and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_metadata_save) == 1

    editor = edit_segment(page)
    editor.fill("Transcript save version two.")
    page.get_by_role("button", name="Lưu đoạn").click()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")
    expect(page.locator(".segment-text")).to_have_text("Transcript save version two.")
    assert server_generation["source_version"] == 2
    editor = edit_segment(page)
    editor.fill("Keep this unsaved transcript draft.")

    metadata_route, metadata_response, metadata_payload = held_metadata_save.pop()
    assert metadata_payload["transcription"]["generation"]["source_version"] == 1
    metadata_route.fulfill(response=metadata_response, json=metadata_payload)
    expect(page.locator("#meeting-title")).to_have_text("Metadata saved during transcript edit")
    expect(editor).to_have_value("Keep this unsaved transcript draft.")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        "meeting-install-generation:2:true"
    )

    stale_meeting_load["enabled"] = True
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Metadata saved during transcript edit")
    expect(editor).to_have_value("Keep this unsaved transcript draft.")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        "meeting-install-generation:2:true"
    )

    editor.fill("Follow-up saved from the newest version.")
    page.get_by_role("button", name="Lưu đoạn").click()
    expect(page.locator(".segment-text")).to_have_text("Follow-up saved from the newest version.")
    assert [body["expected_source_version"] for body in text_saves] == [1, 2]
    assert server_generation["source_version"] == 3

    page.locator("#meeting-actions").click()

    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("Keep this metadata conflict draft")
    external_save = _context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}",
        headers={"Origin": live.url},
        data={"expected_metadata_version": 2, "title": "Metadata saved elsewhere"},
    )
    assert external_save.ok
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-conflict")).to_contain_text("Metadata saved elsewhere")
    expect(page.locator("#edit-title")).to_have_value("Keep this metadata conflict draft")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        "meeting-install-generation:3:true"
    )


def test_late_speaker_assignment_reconciles_after_same_meeting_reload(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Late assignment", "2026-10-14")
    server_generation = {
        "id": "late-assignment-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [
            {"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"},
            {"speaker_id": "SPEAKER_01", "display_name": "Speaker 2"},
        ],
        "segments": [
            {
                "id": "late-assignment-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Assignment race transcript.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    stale_generation = json.loads(json.dumps(server_generation))
    assignments = []
    held_assignment = []

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(json.dumps(stale_generation))
        payload["transcription"]["generation_history"] = []
        route.fulfill(response=response, json=payload)

    def route_assignment(route):
        body = route.request.post_data_json
        assignments.append(body)
        segment = server_generation["segments"][0]
        segment["speaker_id"] = body["speaker_id"]
        segment["assignment_version"] += 1
        server_generation["source_version"] = body["expected_source_version"] + 1
        response = {
            "id": segment["id"],
            "generation_id": body["expected_generation_id"],
            "original_speaker_id": segment["original_speaker_id"],
            "speaker_id": segment["speaker_id"],
            "assignment_version": segment["assignment_version"],
            "source_version": server_generation["source_version"],
        }
        if len(assignments) == 1:
            held_assignment.append((route, response))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"),
        route_assignment,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    edit_segment(page)
    page.locator(".segment-speaker-select").select_option("SPEAKER_01")
    deadline = time.monotonic() + 10
    while not held_assignment and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held_assignment) == 1

    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()
    speaker_select = page.locator(".segment-speaker-select")
    expect(speaker_select).to_have_value("SPEAKER_01")
    held_route, held_response = held_assignment.pop()
    held_route.fulfill(status=200, content_type="application/json", body=json.dumps(held_response))

    expect(speaker_select).to_have_value("SPEAKER_01")
    expect(speaker_select).to_have_attribute("data-current-speaker", "SPEAKER_01")
    expect(speaker_select).to_have_attribute("data-assignment-version", "2")
    expect(page.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Speaker 1")
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        "late-assignment-generation:2:true"
    )

    speaker_select.select_option("SPEAKER_00")
    expect(speaker_select).to_have_value("SPEAKER_00")
    assert [
        (body["expected_source_version"], body["expected_assignment_version"])
        for body in assignments
    ] == [(1, 1), (2, 2)]
    expect(speaker_select).to_have_attribute("data-assignment-version", "3")
    expect(speaker_select).to_have_attribute("data-current-speaker", "SPEAKER_00")
    assert server_generation["source_version"] == 3


def test_transcript_reload_button_is_cleaned_up_after_error_and_navigation(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Reload source", "2026-10-14")
    other_id = seed_meeting(live.settings.data_dir, "Reload destination", "2026-10-13")
    generation = {
        "id": "reload-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [],
        "segments": [
            {
                "id": "reload-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Original text.",
                "original_speaker_id": None,
                "speaker_id": None,
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }

    def route_generation(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = generation
        payload["transcription"]["generation_history"] = []
        route.fulfill(response=response, json=payload)

    page.route(re.compile(rf"/api/meetings/{meeting_id}$"), route_generation)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/text$"),
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": "source_version_changed",
                        "message": "The transcript source changed.",
                    }
                }
            ),
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    edit_segment(page).fill("A local draft.")
    page.get_by_role("button", name="Lưu đoạn").click()
    reload_button = page.get_by_role("button", name="Tải lại bản mới nhất")
    expect(reload_button).to_be_visible()

    held = []
    get_count = 0

    def fail_then_hold_reload(route):
        nonlocal get_count
        if route.request.method != "GET":
            route.fallback()
            return
        get_count += 1
        if get_count == 1:
            route.fulfill(status=503, body="Unavailable")
        elif get_count == 2:
            held.append(route)
        else:
            route.fallback()

    page.route(re.compile(rf"/api/meetings/{meeting_id}$"), fail_then_hold_reload)
    reload_button.click()
    expect(reload_button).to_be_enabled()
    reload_button.evaluate("button => { window.__reloadInitiator = button; }")
    reload_button.click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held
    expect(reload_button).to_be_disabled()

    page.locator("#recent-list a").filter(has_text="Reload destination").click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text("Reload destination")
    held[0].continue_()
    settle(page)
    assert page.evaluate(
        "!window.__reloadInitiator.isConnected || !window.__reloadInitiator.disabled"
    )


def test_assignment_conflict_survives_task_rerender_in_its_own_error_panel(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Separate mutation errors", "2026-10-14")
    generation = {
        "id": "separate-errors-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [
            {"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"},
            {"speaker_id": "SPEAKER_01", "display_name": "Speaker 2"},
        ],
        "segments": [
            {
                "id": "separate-errors-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Original text.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    task = {
        "id": "a" * 32,
        "request_id": "separate-errors-request",
        "meeting_id": meeting_id,
        "kind": "transcription",
        "status": "failed",
        "worker_state": "idle",
        "stage": "failed",
        "result_scope": None,
        "audio_duration_seconds": 30,
        "transcript_end_seconds": 2,
        "segment_count": 1,
        "elapsed_seconds": 1,
        "error_code": "publication_failed",
        "error_message": "The saved task failed.",
        "accepted_at": "2026-10-17 00:00:00",
        "started_at": "2026-10-17 00:00:00",
        "heartbeat_at": "2026-10-17 00:00:01",
        "cancel_requested_at": None,
        "completed_at": "2026-10-17 00:00:01",
    }

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = generation
        payload["transcription"]["generation_history"] = []
        payload["transcription"]["task"] = dict(task)
        route.fulfill(response=response, json=payload)

    def fail_assignment(route):
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": "source_version_changed",
                        "message": "The transcript source changed.",
                    }
                }
            ),
        )

    page.route(re.compile(rf"/api/meetings/{meeting_id}$"), route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"),
        fail_assignment,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#transcription-error")).to_be_visible()
    expect(page.locator("#task-diagnostic-code")).to_have_text("publication_failed")
    edit_segment(page)
    page.locator(".segment-speaker-select").select_option("SPEAKER_01")
    expect(page.locator(".segment-speaker-select")).to_have_value("SPEAKER_01")
    expect(page.locator("#transcript-mutation-error")).to_be_visible()
    expect(page.locator("#transcript-mutation-error-message")).to_contain_text(
        "Transcript đã thay đổi"
    )
    expect(page.locator("#transcript-mutation-error [data-reload-latest]")).to_be_visible()
    expect(page.locator("#transcription-error [data-reload-latest]")).to_have_count(0)

    task["error_code"] = "diarization_model_load_failed"
    task["error_message"] = "An unrelated task failed."
    open_calendar(page)
    page.get_by_role("button", name="Quay lại notes").click()

    expect(page.locator("#transcription-error")).to_be_visible()
    expect(page.locator("#task-diagnostic-code")).to_have_text("diarization_model_load_failed")
    expect(page.locator("#transcription-error [data-reload-latest]")).to_have_count(0)
    expect(page.locator("#transcript-mutation-error")).to_be_visible()
    expect(page.locator("#transcript-mutation-error [data-reload-latest]")).to_be_visible()
    page.locator("#transcript-mutation-error [data-reload-latest]").click()
    expect(page.locator("#transcript-mutation-error")).to_be_hidden()
    expect(page.locator("#transcription-error")).to_be_visible()
    expect(page.locator("#task-diagnostic-code")).to_have_text("diarization_model_load_failed")


def test_assignment_source_conflict_survives_reload_and_can_be_reapplied(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Assignment draft recovery", "2026-10-14")
    generation = {
        "id": "assignment-draft-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [
            {"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"},
            {"speaker_id": "SPEAKER_01", "display_name": "Speaker 2"},
        ],
        "segments": [
            {
                "id": "assignment-draft-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Assignment draft transcript.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    assignment_requests = []

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(json.dumps(generation))
        payload["transcription"]["generation_history"] = []
        route.fulfill(response=response, json=payload)

    def route_assignment(route):
        body = route.request.post_data_json
        assignment_requests.append(body)
        segment = generation["segments"][0]
        if len(assignment_requests) == 1:
            generation["source_version"] = 2
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "code": "source_version_changed",
                            "message": "The transcript source changed.",
                        }
                    }
                ),
            )
            return
        assert body["expected_source_version"] == 3
        segment["speaker_id"] = body["speaker_id"]
        segment["assignment_version"] += 1
        generation["source_version"] = 4
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": segment["id"],
                    "generation_id": generation["id"],
                    "original_speaker_id": segment["original_speaker_id"],
                    "speaker_id": segment["speaker_id"],
                    "assignment_version": segment["assignment_version"],
                    "source_version": generation["source_version"],
                }
            ),
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"),
        route_assignment,
    )

    def route_speaker_name(route):
        body = route.request.post_data_json
        assert body["expected_source_version"] == 2
        speaker = generation["speakers"][1]
        speaker["display_name"] = body["display_name"]
        generation["source_version"] = 3
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "speaker_id": speaker["speaker_id"],
                    "display_name": speaker["display_name"],
                    "source_version": generation["source_version"],
                }
            ),
        )

    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers/SPEAKER_01$"),
        route_speaker_name,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    edit_segment(page)
    select = page.locator(".segment-speaker-select")
    select.select_option("SPEAKER_01")
    expect(select).to_have_value("SPEAKER_01")
    expect(page.locator(".transcript-assignment-draft-review")).to_contain_text(
        "Lựa chọn cần xem lại: Speaker 2"
    )
    reapply = page.get_by_role("button", name="Áp dụng lại lựa chọn")
    expect(reapply).to_be_disabled()
    reload_button = page.locator("#transcript-mutation-error [data-reload-latest]")
    reload_button.click()

    expect(select).to_have_value("SPEAKER_01")
    reapply = page.get_by_role("button", name="Áp dụng lại lựa chọn")
    expect(reapply).to_be_enabled()
    rename_speaker(page, 1).fill("Alice")
    page.locator(
        'form[data-speaker-name][data-speaker-id="SPEAKER_01"] button[type="submit"]'
    ).click()
    expect(page.locator("#speaker-catalog-list .speaker-chip").nth(1)).to_have_text("Alice")
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")
    expect(
        page.locator(".transcript-assignment-draft-review .transcript-draft-value")
    ).to_have_text("Lựa chọn cần xem lại: Alice")
    reapply = page.get_by_role("button", name="Áp dụng lại lựa chọn")
    reapply.click()
    expect(page.locator(".segment-speaker-select")).to_have_value("SPEAKER_01")
    expect(page.locator(".segment-speaker-select")).to_have_attribute(
        "data-current-speaker", "SPEAKER_01"
    )
    expect(page.locator(".transcript-assignment-draft-review")).to_have_count(0)
    assert [
        (request["expected_source_version"], request["speaker_id"])
        for request in assignment_requests
    ] == [(1, "SPEAKER_01"), (3, "SPEAKER_01")]
    assert page.locator("#transcript-generation").get_attribute("data-render-key") == (
        f"{generation['id']}:4:true"
    )


def test_assignment_current_generation_conflict_is_retained_for_recovery(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(
        live.settings.data_dir, "Assignment generation recovery", "2026-10-14"
    )
    old_generation = {
        "id": "assignment-old-generation",
        "generation_number": 1,
        "status": "complete",
        "language_hint": "en",
        "model_name": "whisper-large-v3",
        "source_version": 1,
        "segment_count": 1,
        "speakers": [
            {"speaker_id": "SPEAKER_00", "display_name": "Speaker 1"},
            {"speaker_id": "SPEAKER_01", "display_name": "Speaker 2"},
        ],
        "segments": [
            {
                "id": "assignment-old-segment",
                "sequence": 0,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "Old generation transcript.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "uncertain": False,
                "review_reasons": [],
                "assignment_version": 1,
            }
        ],
    }
    new_generation = json.loads(json.dumps(old_generation))
    new_generation["id"] = "assignment-new-generation"
    new_generation["generation_number"] = 2
    new_generation["segments"][0]["id"] = "assignment-new-segment"
    latest = {"enabled": False}

    def route_meeting(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"] = json.loads(
            json.dumps(new_generation if latest["enabled"] else old_generation)
        )
        payload["transcription"]["generation_history"] = (
            [json.loads(json.dumps(old_generation))] if latest["enabled"] else []
        )
        route.fulfill(response=response, json=payload)

    def route_assignment(route):
        latest["enabled"] = True
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": "current_generation_changed",
                        "message": "The current transcript changed.",
                    }
                }
            ),
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_meeting)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-segments/[^/]+/speaker$"),
        route_assignment,
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    edit_segment(page)
    page.locator(".segment-speaker-select").select_option("SPEAKER_01")
    expect(page.locator(".segment-speaker-select")).to_have_value("SPEAKER_01")
    page.locator("#transcript-mutation-error [data-reload-latest]").click()
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    recovery = page.locator("#transcript-draft-recovery")
    expect(recovery).to_be_visible()
    expect(recovery).to_contain_text("Bản mới nhất đã lưu: Speaker 1")
    expect(recovery).to_contain_text("Bản nháp chưa lưu: Speaker 2")
    recovery.get_by_role("button", name="Xem transcript này").click()
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(page.locator(".transcript-assignment-draft-review")).to_contain_text(
        "Lựa chọn cần xem lại: Speaker 2"
    )


# ---- transcript read mode: one segment editor and one speaker rename at a time (#45) ----

READING_SPEAKERS = [
    {
        "speaker_id": "SPEAKER_00",
        "display_name": "Speaker 1",
        "first_seen_seconds": 1,
        "last_seen_seconds": 9,
    },
    {
        "speaker_id": "SPEAKER_01",
        "display_name": "Alice",
        "first_seen_seconds": 4,
        "last_seen_seconds": 6,
    },
]
READING_SEGMENTS = [
    {
        "id": "1" * 32,
        "start_seconds": 1,
        "end_seconds": 3,
        "text": "We open with the launch plan.",
        "original_speaker_id": "SPEAKER_00",
        "avg_logprob": -0.2,
        "no_speech_prob": 0.05,
        "compression_ratio": 1.1,
    },
    {
        "id": "2" * 32,
        "start_seconds": 4,
        "end_seconds": 6,
        "text": "Costs look high this quarter.",
        "original_speaker_id": "SPEAKER_01",
        "avg_logprob": -1.4,  # below -1.0 the backend adds the asr_uncertain reason itself
        "no_speech_prob": 0.1,
        "compression_ratio": 1.2,
    },
    {
        "id": "3" * 32,
        "start_seconds": 7,
        "end_seconds": 9,
        "text": "Someone agrees, but it is hard to tell who.",
        "original_speaker_id": None,
        "review_reasons": ["unidentified_speaker"],
    },
]


def open_reading_meeting(live, page, title="Reading mode"):
    meeting_id = seed_meeting(live.settings.data_dir, title, "2026-10-14", duration_seconds=30)
    generation = _publish_browser_generation(
        live, meeting_id, segments=READING_SEGMENTS, speakers=READING_SPEAKERS
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator(".segment-edit-button").first).to_be_enabled()  # the source lock is known
    return meeting_id, generation["id"]


def speaker_chip(page, index):
    """A speaker's chip by position; unlike `.speaker-chip`, stable while a rename replaces one."""
    return page.locator("#speaker-catalog-list > li").nth(index).locator(".speaker-chip")


def show_a_past_generation(page):
    """Serve each meeting with a copy of its transcript as history: a second transcript to open."""

    def route_meeting(route):
        if route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        payload = response.json()
        past = json.loads(json.dumps(payload["transcription"]["generation"]))
        past["id"] = "browser-past-generation"
        past["generation_number"] = 0
        for segment in past["segments"]:
            segment["id"] = "b" + segment["id"][1:]
        payload["transcription"]["generation_history"] = [past]
        route.fulfill(response=response, json=payload)

    page.route(re.compile(r"/api/meetings/[^/?]+$"), route_meeting)


def test_transcript_reads_by_default_and_shows_no_editing_control(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    rows = page.locator(".transcript-segment")
    expect(rows).to_have_count(3)

    for control in ("textarea", "select", "button[type=submit]", "[contenteditable]"):
        expect(page.locator(f"#transcript-segments {control}")).to_have_count(0)
    expect(page.locator(".segment-original-speaker, .segment-model-measures")).to_have_count(0)

    first, second, third = rows.nth(0), rows.nth(1), rows.nth(2)
    expect(first.locator(".segment-timestamp")).to_have_text("0:01.00")
    expect(first.locator(".segment-timestamp")).to_have_accessible_name("Phát bản ghi từ 0:01.00")
    expect(first.locator(".segment-speaker")).to_have_text("Speaker 1")
    expect(second.locator(".segment-speaker")).to_have_text("Alice")
    expect(third.locator(".segment-speaker")).to_have_text("Chưa xác định")
    expect(first.locator(".segment-text")).to_have_text("We open with the launch plan.")
    expect(first.locator(".segment-review-cue")).to_have_count(0)
    expect(second.locator(".segment-review-cue")).to_have_text("Cần kiểm tra · Nhận dạng chưa chắc")
    expect(third.locator(".segment-review-cue")).to_have_text(
        "Cần kiểm tra · Chưa xác định người nói"
    )
    sizes = page.eval_on_selector_all(
        ".segment-text", "els => els.map(el => parseFloat(getComputedStyle(el).fontSize))"
    )
    assert len(sizes) == 3 and min(sizes) >= 13

    # Sửa is always on screen: it needs no hover and no focus.
    for index in range(3):
        edit = rows.nth(index).locator(".segment-edit-button")
        expect(edit).to_be_visible()
        expect(edit).to_have_text("Sửa")
        assert edit.evaluate("el => getComputedStyle(el).opacity") == "1"


def test_edit_opens_the_editor_and_esc_cancels_and_ctrl_enter_saves(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    patches = record_patches(page)
    row = page.locator(".transcript-segment").nth(0)

    # Enter on the focused Sửa opens the editor, focuses it and types nothing into it.
    row.locator(".segment-edit-button").focus()
    page.keyboard.press("Enter")
    editor = row.locator(".segment-text-editor")
    expect(editor).to_be_focused()
    expect(editor).to_have_value("We open with the launch plan.")
    expect(row.locator(".segment-speaker-select")).to_be_visible()
    expect(row.get_by_role("button", name="Lưu đoạn")).to_be_visible()
    expect(row.get_by_role("button", name="Hủy")).to_be_visible()
    expect(row.locator(".segment-edit-button")).to_have_count(0)

    # A key that ends an IME composition is not a command.
    editor.fill("Composing…")
    for key, ctrl in (("Escape", False), ("Enter", True)):
        editor.evaluate(
            """(el, [key, ctrl]) => el.dispatchEvent(new KeyboardEvent("keydown", {
              key, ctrlKey: ctrl, isComposing: true, bubbles: true, cancelable: true }))""",
            [key, ctrl],
        )
    expect(editor).to_have_value("Composing…")
    assert patches == []

    # Esc cancels: the typed text goes, nothing is sent and focus is back on Sửa.
    editor.fill("Typed then abandoned.")
    page.keyboard.press("Escape")
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-text")).to_have_text("We open with the launch plan.")
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(row.locator(".segment-draft-mark")).to_have_count(0)
    assert patches == []

    # So does Hủy.
    row.locator(".segment-edit-button").click()
    row.locator(".segment-text-editor").fill("Another abandoned draft.")
    row.get_by_role("button", name="Hủy").click()
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(row.locator(".segment-draft-mark")).to_have_count(0)
    assert patches == []

    # Saving without a change closes the editor and sends nothing.
    row.locator(".segment-edit-button").click()
    row.get_by_role("button", name="Lưu đoạn").click()
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Không có thay đổi.")
    assert patches == []

    # Ctrl+Enter saves for real, closes the editor and puts focus back on Sửa.
    row.locator(".segment-edit-button").click()
    row.locator(".segment-text-editor").fill("We open with the revised launch plan.")
    with page.expect_response(
        lambda response: response.request.method == "PATCH" and response.url.endswith("/text")
    ) as saved:
        page.keyboard.press("Control+Enter")
    assert saved.value.status == 200
    expect(row.locator(".segment-text")).to_have_text("We open with the revised launch plan.")
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")

    # ⌘+Enter does the same.
    row.locator(".segment-edit-button").click()
    row.locator(".segment-text-editor").fill("Saved with the command key.")
    page.keyboard.press("Meta+Enter")
    expect(row.locator(".segment-text")).to_have_text("Saved with the command key.")
    expect(row.locator(".segment-edit-button")).to_be_focused()
    assert [patch["text"] for patch in patches] == [
        "We open with the revised launch plan.",
        "Saved with the command key.",
    ]


def test_ctrl_enter_saves_from_every_control_of_the_open_editor_once(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    patches = record_patches(page)
    page.evaluate(
        """() => {
          window.__submits = 0;
          document.querySelector("#transcript-generation")
            .addEventListener("submit", () => { window.__submits += 1; }, true);
        }"""
    )
    row = page.locator(".transcript-segment").nth(0)
    controls = [
        ("the text box", row.locator(".segment-text-editor"), "Control+Enter"),
        ("the speaker select", row.locator(".segment-speaker-select"), "Control+Enter"),
        ("Lưu đoạn", row.get_by_role("button", name="Lưu đoạn"), "Meta+Enter"),
        ("Hủy", row.get_by_role("button", name="Hủy"), "Control+Enter"),
        ("the Chi tiết summary", row.locator("details.segment-details summary"), "Meta+Enter"),
    ]
    for saved, (name, control, shortcut) in enumerate(controls, start=1):
        row.locator(".segment-edit-button").click()
        text = f"Saved from {name}."
        row.locator(".segment-text-editor").fill(text)
        control.focus()
        page.keyboard.press(shortcut)
        expect(row.locator(".segment-text")).to_have_text(text)
        expect(row.locator(".segment-text-editor")).to_have_count(0)
        expect(row.locator(".segment-edit-button")).to_be_focused()
        # One submit and one request per press: neither Hủy nor the summary also acts on the key.
        assert page.evaluate("window.__submits") == saved, name
        assert [patch["text"] for patch in patches][saved - 1 :] == [text], name

    # A key that ends an IME composition is not a command, on any control of the row.
    row.locator(".segment-edit-button").click()
    row.locator(".segment-text-editor").fill("Composing…")
    row.locator(".segment-speaker-select").evaluate(
        """el => el.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", ctrlKey: true, isComposing: true, bubbles: true, cancelable: true }))"""
    )
    settle(page)
    expect(row.locator(".segment-text-editor")).to_have_value("Composing…")
    assert len(patches) == len(controls)


def test_unsaved_text_survives_opening_another_segment_and_is_marked(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    patches = record_patches(page)
    rows = page.locator(".transcript-segment")
    first, second = rows.nth(0), rows.nth(1)

    first.locator(".segment-edit-button").click()
    first.locator(".segment-text-editor").fill("An unsaved first draft.")
    second.locator(".segment-edit-button").click()

    # One editor at a time: the first row is back in read mode on its saved text ...
    expect(page.locator(".segment-text-editor")).to_have_count(1)
    expect(second.locator(".segment-text-editor")).to_be_focused()
    expect(first.locator(".segment-text")).to_have_text("We open with the launch plan.")
    # ... and a marker made of words, a dashed border and the button's name says a draft waits.
    mark = first.locator(".segment-draft-mark")
    expect(mark).to_be_visible()
    expect(mark).to_have_text("Bản nháp chưa lưu")
    assert mark.evaluate("el => getComputedStyle(el).borderTopStyle") == "dashed"
    expect(first.locator(".segment-edit-button")).to_have_accessible_name(
        "Sửa đoạn bắt đầu tại 0:01.00 (có bản nháp chưa lưu)"
    )
    expect(second.locator(".segment-draft-mark")).to_have_count(0)
    assert patches == []

    # Reopening restores the text; Hủy then drops it for good.
    first.locator(".segment-edit-button").click()
    expect(first.locator(".segment-text-editor")).to_have_value("An unsaved first draft.")
    expect(second.locator(".segment-text-editor")).to_have_count(0)
    first.get_by_role("button", name="Hủy").click()
    expect(first.locator(".segment-draft-mark")).to_have_count(0)
    first.locator(".segment-edit-button").click()
    expect(first.locator(".segment-text-editor")).to_have_value("We open with the launch plan.")


def test_esc_and_cancel_keep_a_conflict_marked_text_draft(live, browser_context):
    context, page = browser_context
    show_a_past_generation(page)
    meeting_id, generation_id = open_reading_meeting(live, page)
    row = page.locator(".transcript-segment").nth(0)
    draft = "My draft, typed before the other save."
    editor = edit_segment(page)
    editor.fill(draft)

    # Another client saves this segment first, so the save below meets a real 409.
    other = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-segments/{READING_SEGMENTS[0]['id']}/text",
        headers={"Origin": live.url},
        data={
            "expected_generation_id": generation_id,
            "expected_source_version": 1,
            "text": "The other client's version.",
        },
    )
    assert other.ok
    row.get_by_role("button", name="Lưu đoạn").click()
    expect(row.locator(".segment-edit-status")).to_contain_text("Bản nháp vẫn được giữ")

    # Esc closes the editor but keeps the draft: the row stays marked, reopening brings back the
    # text and the review of the conflict.
    editor.press("Escape")
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(row.locator(".segment-draft-mark")).to_have_text("Bản nháp chưa lưu")
    row.locator(".segment-edit-button").click()
    expect(row.locator(".segment-text-editor")).to_have_value(draft)
    expect(row.locator(".transcript-draft-review")).to_contain_text(f"Bản nháp chưa lưu: {draft}")

    # So does Hủy; the draft is then listed for recovery while another transcript is on screen.
    row.get_by_role("button", name="Hủy").click()
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_focused()
    expect(row.locator(".segment-draft-mark")).to_have_text("Bản nháp chưa lưu")
    page.locator("#generation-selector").select_option("browser-past-generation")
    recovery = page.locator("#transcript-draft-recovery")
    expect(recovery).to_be_visible()
    expect(recovery.locator(".transcript-draft-value")).to_have_text(f"Bản nháp chưa lưu: {draft}")


# Tab stops of one row, as the read-mode test counts them.
TAB_STOPS_JS = """row => [
  ...row.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]"),
].filter((el) => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length).length"""


def save_over_another_clients_edit(context, live, page, draft):
    """Save `draft` over another client's edit of the first segment: a real 409."""
    meeting_id, generation_id = open_reading_meeting(live, page)
    row = page.locator(".transcript-segment").nth(0)
    edit_segment(page).fill(draft)
    other = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-segments/{READING_SEGMENTS[0]['id']}/text",
        headers={"Origin": live.url},
        data={
            "expected_generation_id": generation_id,
            "expected_source_version": 1,
            "text": "The other client's version.",
        },
    )
    assert other.ok
    row.get_by_role("button", name="Lưu đoạn").click()
    expect(row.locator(".segment-edit-status")).to_contain_text("Bản nháp vẫn được giữ")


def test_a_closed_conflicted_segment_keeps_a_reload_in_the_conflict_panel(live, browser_context):
    context, page = browser_context
    draft = "My draft, typed before the other save."
    save_over_another_clients_edit(context, live, page, draft)
    first = page.locator(".transcript-segment").nth(0)
    second = page.locator(".transcript-segment").nth(1)
    panel = page.locator("#transcript-mutation-error")
    reloads = page.locator("[data-reload-latest]")  # every reload action on the page

    # The editor is still open: the reload sits beside Lưu đoạn and the panel stays out of it.
    expect(first.locator(".segment-edit-actions [data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    expect(panel).to_be_hidden()

    # Esc closes it. A read row keeps two tab stops, so the reload moves to the conflict panel.
    first.locator(".segment-text-editor").press("Escape")
    expect(first.locator(".segment-text-editor")).to_have_count(0)
    expect(first.locator(".segment-edit-button")).to_be_focused()
    expect(panel).to_be_visible()
    expect(panel.get_by_role("button", name="Tải lại bản mới nhất")).to_have_count(1)
    expect(reloads).to_have_count(1)
    assert first.evaluate(TAB_STOPS_JS) == 2

    # So does Hủy, and so does opening another segment, which closes the editor it leaves.
    first.locator(".segment-edit-button").click()
    first.get_by_role("button", name="Hủy").click()
    expect(panel.locator("[data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    first.locator(".segment-edit-button").click()
    second.locator(".segment-edit-button").click()
    expect(first.locator(".segment-text-editor")).to_have_count(0)
    expect(second.locator(".segment-text-editor")).to_have_count(1)
    expect(panel.locator("[data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    assert first.evaluate(TAB_STOPS_JS) == 2

    # The panel's reload brings the other client's text. The draft stays, and since the latest
    # version is now on screen, closing the editor again does not offer another reload.
    panel.get_by_role("button", name="Tải lại bản mới nhất").click()
    expect(panel).to_be_hidden()
    expect(reloads).to_have_count(0)
    first.locator(".segment-edit-button").click()
    expect(first.locator(".segment-text-editor")).to_have_value(draft)
    expect(first.locator(".transcript-draft-review")).to_contain_text(
        "Bản mới nhất đã lưu: The other client's version."
    )
    first.get_by_role("button", name="Hủy").click()
    expect(first.locator(".segment-draft-mark")).to_have_text("Bản nháp chưa lưu")
    expect(panel).to_be_hidden()
    expect(reloads).to_have_count(0)


def test_a_reopened_conflicted_segment_offers_the_reload_in_its_editor(live, browser_context):
    context, page = browser_context
    draft = "My draft, typed before the other save."
    save_over_another_clients_edit(context, live, page, draft)
    first = page.locator(".transcript-segment").nth(0)
    panel = page.locator("#transcript-mutation-error")
    reloads = page.locator("[data-reload-latest]")  # every reload action on the page
    first.locator(".segment-text-editor").press("Escape")
    expect(first.locator(".segment-text-editor")).to_have_count(0)

    # Reopened, the review still shows the text this page last saw; the reload is beside Lưu đoạn
    # and the panel steps aside, so there is one reload on the page.
    first.locator(".segment-edit-button").click()
    expect(first.locator(".segment-text-editor")).to_have_value(draft)
    review = first.locator(".transcript-draft-review")
    expect(review).to_contain_text("Bản mới nhất đã lưu: We open with the launch plan.")
    actions = first.locator(".segment-edit-actions")
    expect(actions.locator("[data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    expect(panel).to_be_hidden()

    # It loads the other client's version into the review and leaves the draft and the editor alone.
    actions.get_by_role("button", name="Tải lại bản mới nhất").click()
    expect(review).to_contain_text("Bản mới nhất đã lưu: The other client's version.")
    expect(first.locator(".segment-text-editor")).to_have_value(draft)
    expect(reloads).to_have_count(0)

    # The user can now review the draft against it and save over the newer version.
    reviewed = f"{draft} Reviewed."
    first.locator(".segment-text-editor").fill(reviewed)
    first.get_by_role("button", name="Lưu đoạn").click()
    expect(first.locator(".segment-text")).to_have_text(reviewed)
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")
    expect(reloads).to_have_count(0)


def test_a_conflicted_speaker_name_keeps_a_reload_in_its_form_and_the_conflict_panel(
    live, browser_context
):
    context, page = browser_context
    meeting_id, generation_id = open_reading_meeting(live, page)
    catalog = page.locator("#speaker-catalog-list")
    panel = page.locator("#transcript-mutation-error")
    reloads = page.locator("[data-reload-latest]")  # every reload action on the page
    name = rename_speaker(page, 0)
    name.fill("My name")

    # Another client renames the same speaker first, so the save below meets a real 409.
    other = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-generations/{generation_id}"
        "/speakers/SPEAKER_00",
        headers={"Origin": live.url},
        data={"expected_source_version": 1, "display_name": "Their name"},
    )
    assert other.ok
    catalog.get_by_role("button", name="Lưu tên").click()
    expect(catalog.locator(".speaker-form-status")).to_contain_text("Bản nháp vẫn được giữ")

    # The rename is still open: the reload sits in its form and the panel stays out of it.
    expect(catalog.locator(".speaker-name-form [data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    expect(panel).to_be_hidden()

    # Esc closes it. A chip is one button, so the reload moves to the conflict panel.
    name.press("Escape")
    expect(catalog.locator("input")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(panel).to_be_visible()
    expect(panel.get_by_role("button", name="Tải lại bản mới nhất")).to_have_count(1)
    expect(reloads).to_have_count(1)
    expect(catalog.locator("[data-reload-latest]")).to_have_count(0)

    # So does Hủy, and so does opening another speaker's rename, which closes the one it leaves.
    speaker_chip(page, 0).click()
    catalog.get_by_role("button", name="Hủy").click()
    expect(panel.locator("[data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    speaker_chip(page, 0).click()
    speaker_chip(page, 1).click()
    expect(catalog.locator("input")).to_have_value("Alice")
    expect(panel.locator("[data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)

    # Reopened, the form carries the reload again and the panel steps aside. The review still shows
    # the name this page last saw; the reload brings the other client's and keeps the draft.
    speaker_chip(page, 0).click()
    expect(catalog.locator("input")).to_have_value("My name")
    review = catalog.locator(".transcript-draft-review")
    expect(review).to_contain_text("Bản mới nhất đã lưu: Speaker 1")
    expect(catalog.locator(".speaker-name-form [data-reload-latest]")).to_have_count(1)
    expect(reloads).to_have_count(1)
    expect(panel).to_be_hidden()
    catalog.get_by_role("button", name="Tải lại bản mới nhất").click()
    expect(review).to_contain_text("Bản mới nhất đã lưu: Their name")
    expect(catalog.locator("input")).to_have_value("My name")
    expect(reloads).to_have_count(0)

    # The latest name is on screen, so closing the form again does not offer another reload.
    catalog.get_by_role("button", name="Hủy").click()
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text(["bản nháp"])
    expect(panel).to_be_hidden()
    expect(reloads).to_have_count(0)


def test_esc_and_cancel_keep_a_conflict_marked_speaker_name_draft(live, browser_context):
    context, page = browser_context
    show_a_past_generation(page)
    meeting_id, generation_id = open_reading_meeting(live, page)
    catalog = page.locator("#speaker-catalog-list")
    name = rename_speaker(page, 0)
    name.fill("My name")

    # Another client renames the same speaker first, so the save below meets a real 409.
    other = context.request.patch(
        f"{live.url}/api/meetings/{meeting_id}/transcript-generations/{generation_id}"
        "/speakers/SPEAKER_00",
        headers={"Origin": live.url},
        data={"expected_source_version": 1, "display_name": "Their name"},
    )
    assert other.ok
    catalog.get_by_role("button", name="Lưu tên").click()
    expect(catalog.locator(".speaker-form-status")).to_contain_text("Bản nháp vẫn được giữ")

    # Esc closes the rename but keeps the name: the chip stays marked, reopening brings it back
    # with the review of the conflict.
    name.press("Escape")
    expect(catalog.locator("input")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text(
        ["đặt tên", "bản nháp"]
    )
    speaker_chip(page, 0).click()
    expect(catalog.locator("input")).to_have_value("My name")
    expect(catalog.locator(".transcript-draft-review")).to_contain_text(
        "Bản nháp chưa lưu: My name"
    )

    # So does Hủy; the name is then listed for recovery while another transcript is on screen.
    catalog.get_by_role("button", name="Hủy").click()
    expect(catalog.locator("input")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text(
        ["đặt tên", "bản nháp"]
    )
    page.locator("#generation-selector").select_option("browser-past-generation")
    recovery = page.locator("#transcript-draft-recovery")
    expect(recovery).to_be_visible()
    expect(recovery.locator(".transcript-draft-value")).to_have_text("Bản nháp chưa lưu: My name")


def test_origin_and_model_measures_show_only_in_the_open_editors_details(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    anywhere = ".segment-original-speaker, .segment-model-measures, details.segment-details"
    expect(page.locator(anywhere)).to_have_count(0)

    row = page.locator(".transcript-segment").nth(1)
    row.locator(".segment-edit-button").click()
    details = row.locator("details.segment-details")
    expect(page.locator("details.segment-details")).to_have_count(1)
    expect(details).not_to_have_attribute("open", "")
    expect(details.locator(".segment-original-speaker")).to_be_hidden()
    details.locator("summary").click()
    expect(details.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Alice")
    measures = details.locator(".segment-model-measures")
    expect(measures).to_contain_text("avg_logprob -1.400")
    expect(measures).to_contain_text("no_speech_prob 0.100")
    expect(measures).to_contain_text("compression_ratio 1.200")
    expect(page.locator(".segment-original-speaker")).to_have_count(1)
    expect(page.locator(".segment-model-measures")).to_have_count(1)

    # Closing the editor takes the details away with it.
    row.get_by_role("button", name="Hủy").click()
    expect(page.locator(anywhere)).to_have_count(0)


def test_speakers_are_chips_with_inline_rename_restore_and_add(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    patches = record_patches(page)
    chips = page.locator("#speaker-catalog-list .speaker-chip")
    expect(chips).to_have_count(2)
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Speaker 1")
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text("đặt tên")
    expect(speaker_chip(page, 1).locator(".speaker-chip-name")).to_have_text("Alice")
    expect(speaker_chip(page, 1).locator(".speaker-chip-mark")).to_have_count(0)
    expect(page.locator("#speaker-catalog-list input")).to_have_count(0)

    # Esc cancels an inline rename: nothing is sent and focus is back on the chip.
    speaker_chip(page, 0).click()
    name = page.locator("#speaker-catalog-list input")
    expect(name).to_be_focused()
    expect(name).to_have_value("Speaker 1")
    name.fill("Typed then abandoned")
    page.keyboard.press("Escape")
    expect(page.locator("#speaker-catalog-list input")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Speaker 1")
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text("đặt tên")
    assert patches == []

    # So does Hủy, for anyone without an Esc key.
    speaker_chip(page, 0).click()
    page.locator("#speaker-catalog-list input").fill("Typed then cancelled")
    page.locator("#speaker-catalog-list").get_by_role("button", name="Hủy").click()
    expect(page.locator("#speaker-catalog-list input")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text("đặt tên")
    assert patches == []

    # Opening another chip leaves a typed name behind as a marked draft, restored on reopen.
    speaker_chip(page, 0).click()
    page.locator("#speaker-catalog-list input").fill("Half typed")
    speaker_chip(page, 1).click()
    expect(page.locator("#speaker-catalog-list input")).to_have_count(1)
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Speaker 1")
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text(
        ["đặt tên", "bản nháp"]
    )
    page.keyboard.press("Escape")
    speaker_chip(page, 0).click()
    expect(page.locator("#speaker-catalog-list input")).to_have_value("Half typed")

    # Enter saves: the chip carries the new name, loses its mark and keeps focus.
    page.locator("#speaker-catalog-list input").fill("Morgan")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH" and "/speakers/SPEAKER_00" in response.url
        )
    ) as saved:
        page.keyboard.press("Enter")
    assert saved.value.status == 200
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Morgan")
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_count(0)
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(page.locator(".transcript-segment").nth(0).locator(".segment-speaker")).to_have_text(
        "Morgan"
    )
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu tên người nói.")

    # The default name can still be restored, which brings the invitation back.
    speaker_chip(page, 0).click()
    page.get_by_role("button", name="Khôi phục tên mặc định").click()
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Speaker 1")
    expect(speaker_chip(page, 0).locator(".speaker-chip-mark")).to_have_text("đặt tên")
    expect(speaker_chip(page, 0)).to_be_focused()
    expect(page.locator(".transcript-segment").nth(0).locator(".segment-speaker")).to_have_text(
        "Speaker 1"
    )

    # Adding a speaker is still there, and a blank name gets the system default and its mark.
    add_name = page.locator("#speaker-add-name")
    add_name.fill("")
    add_name.press("Enter")
    expect(page.locator("#speaker-add-status")).to_have_text("Đã thêm người nói.")
    expect(chips).to_have_count(3)
    expect(chips.last.locator(".speaker-chip-name")).to_have_text(re.compile(r"^Speaker \d+$"))
    expect(chips.last.locator(".speaker-chip-mark")).to_have_text("đặt tên")
    expect(chips.last).to_be_focused()


def test_locked_source_disables_edit_buttons_and_chips_with_the_reason(live, browser_context):
    _context, page = browser_context
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(
        live.settings.data_dir, title="Locked reading"
    )
    accept_notes_task(
        live.settings.data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="browser-reading-lock",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
        output_language="en",
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    reason = "Tác vụ notes đang dùng nguồn; chỉnh sửa tạm thời bị khóa."
    expect(page.locator("#generation-mode")).to_contain_text(reason)
    # The running notes task opens the notes tab, where the lock is announced ...
    expect(page.locator("#notes-lock-status")).to_be_visible()
    # ... and the transcript tab shows the controls it disables.
    page.locator("#meeting-tab-transcript").click()
    expect(page.locator("#generation-mode")).to_be_visible()
    edits = page.locator(".segment-edit-button")
    expect(edits).to_have_count(2)
    for control in (edits.first, edits.last, page.locator(".speaker-chip").first):
        expect(control).to_be_disabled()
        expect(control).to_have_attribute("aria-describedby", "generation-mode")
    edits.first.click(force=True)
    page.locator(".speaker-chip").first.click(force=True)
    expect(page.locator(".segment-text-editor, #speaker-catalog-list input")).to_have_count(0)


def test_lock_arriving_mid_edit_disables_saving_but_not_cancel(live, browser_context):
    _context, page = browser_context
    meeting_id, generation_id = open_reading_meeting(live, page)
    patches = record_patches(page)
    row = page.locator(".transcript-segment").nth(0)
    editor = edit_segment(page)
    editor.fill("Half typed when the lock came.")

    # A notes task takes the source; the next read of the meeting tells the page.
    accept_notes_task(
        live.settings.data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id="browser-mid-edit-lock",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
        output_language="en",
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    for control in (editor, row.get_by_role("button", name="Lưu đoạn"), row.locator("select")):
        expect(control).to_be_disabled()
    cancel = row.get_by_role("button", name="Hủy")
    expect(cancel).to_be_enabled()

    # Ctrl+Enter from Hủy, the one control still usable, saves nothing under the lock.
    cancel.focus()
    page.keyboard.press("Control+Enter")
    settle(page)
    expect(editor).to_have_value("Half typed when the lock came.")

    # Hủy still closes it; Sửa is disabled by the lock, so focus stays on the row's timestamp.
    cancel.click()
    expect(row.locator(".segment-text-editor")).to_have_count(0)
    expect(row.locator(".segment-edit-button")).to_be_disabled()
    expect(row.locator(".segment-edit-button")).to_have_attribute(
        "aria-describedby", "generation-mode"
    )
    expect(row.locator(".segment-timestamp")).to_be_focused()
    assert patches == []


def test_past_transcript_has_no_edit_buttons_or_rename_chips(live, browser_context):
    context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Past reading", "2026-10-14")
    _publish_browser_generation(live, meeting_id)
    generation = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()[
        "transcription"
    ]["generation"]
    past = json.loads(json.dumps(generation))
    past["id"] = "browser-past-generation"
    past["generation_number"] = 0
    past["segments"][0]["id"] = "b" * 32

    def route_with_history(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation_history"] = [json.loads(json.dumps(past))]
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", route_with_history)
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator(".segment-edit-button")).to_have_count(1)
    expect(page.locator("#speaker-catalog-list button.speaker-chip")).to_have_count(1)

    page.locator("#generation-selector").select_option(past["id"])
    expect(page.locator("#generation-title")).to_have_text("Transcript #0")
    expect(page.locator("#generation-mode")).to_have_text("Bản lịch sử · Chỉ đọc.")
    expect(page.locator(".segment-text")).to_have_text("A saved sentence.")
    for control in (
        ".segment-edit-button",
        ".segment-text-editor",
        ".segment-speaker-select",
        "#speaker-catalog-list button",
    ):
        expect(page.locator(control)).to_have_count(0)
    expect(page.locator("#speaker-catalog-list .speaker-chip")).to_have_text("Speaker 1")
    expect(page.locator("#speaker-add-form")).to_be_hidden()
    # The origin stays reachable, but only behind a collapsed "Chi tiết", never inline.
    expect(page.locator("details.segment-details")).not_to_have_attribute("open", "")
    expect(page.locator(".segment-original-speaker")).to_be_hidden()
    expect(page.locator(".segment-original-speaker")).to_have_text("Gán ban đầu: Speaker 1")


def test_read_rows_have_two_tab_stops_and_one_shared_live_region(live, browser_context):
    _context, page = browser_context
    open_reading_meeting(live, page)
    stops = page.evaluate(
        """() => [...document.querySelectorAll(".transcript-segment")].map((row) =>
          [...row.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
            .filter((el) => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length)
            .length)"""
    )
    assert stops == [2, 2, 2]

    # Walking with Tab: timestamp, Sửa, then the next row's timestamp, Sửa, and so on.
    page.locator(".segment-timestamp").first.focus()
    walk = []
    for _ in range(5):
        page.keyboard.press("Tab")
        walk.append(
            page.evaluate(
                """() => {
                  const el = document.activeElement;
                  const kind = el.classList.contains("segment-edit-button") ? "edit"
                    : el.classList.contains("segment-timestamp") ? "time" : el.tagName;
                  const row = el.closest(".transcript-segment");
                  return [kind, row ? [...row.parentElement.children].indexOf(row) : -1];
                }"""
            )
        )
    assert walk == [["edit", 0], ["time", 1], ["edit", 1], ["time", 2], ["edit", 2]]

    # No row owns a live region, open editor included: one region announces every save.
    rows = page.locator(".transcript-segment")
    in_rows = "[role=status], [role=alert], [aria-live], output"
    expect(rows.locator(in_rows)).to_have_count(0)
    shared = page.locator("#transcript-mutation-status")
    expect(shared).to_have_attribute("aria-live", "polite")
    page.evaluate("window.__sharedRegion = document.querySelector('#transcript-mutation-status')")
    for index, text in ((0, "First save."), (1, "Second save.")):
        rows.nth(index).locator(".segment-edit-button").click()
        expect(rows.locator(in_rows)).to_have_count(0)
        rows.nth(index).locator(".segment-text-editor").fill(text)
        rows.nth(index).get_by_role("button", name="Lưu đoạn").click()
        expect(rows.nth(index).locator(".segment-text")).to_have_text(text)
        expect(shared).to_have_text("Đã lưu.")
        expect(page.locator("[aria-live], [role=status]").filter(has_text="Đã lưu.")).to_have_count(
            1
        )
    assert page.evaluate(
        "window.__sharedRegion === document.querySelector('#transcript-mutation-status')"
    )


CONTRAST_JS = r"""(selectors) => {
  const parse = (value) => {
    const parts = value.match(/[\d.]+/g).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const over = (top, bottom) => ({
    r: top.r * top.a + bottom.r * (1 - top.a),
    g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a),
    a: 1,
  });
  const luminance = ({ r, g, b }) => {
    const linear = [r, g, b].map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  };
  const ratios = {};
  for (const selector of selectors) {
    const element = document.querySelector(selector);
    if (!element) {
      ratios[selector] = null;
      continue;
    }
    const chain = [];
    for (let node = element; node; node = node.parentElement) chain.unshift(node);
    let backdrop = { r: 255, g: 255, b: 255, a: 1 };
    for (const node of chain) {
      backdrop = over(parse(getComputedStyle(node).backgroundColor), backdrop);
    }
    const text = over(parse(getComputedStyle(element).color), backdrop);
    const [high, low] = [luminance(text), luminance(backdrop)].sort((a, b) => b - a);
    ratios[selector] = (high + 0.05) / (low + 0.05);
  }
  return ratios;
}"""
OVERFLOW_JS = """() => ({
  page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  wide: [...document.querySelectorAll("#transcription-panel *")]
    .filter((el) => el.getClientRects().length
      && el.getBoundingClientRect().right > document.documentElement.clientWidth + 0.5)
    .map((el) => el.className || el.tagName),
})"""


def tab_until(page, predicate, limit=120):
    """Tab as a keyboard user does until `predicate`, a JS test of the focused element, holds."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate(f"({predicate})(document.activeElement)"):
            return
    raise AssertionError(f"Tab never reached: {predicate}")


def assert_focus_ring(page):
    ring = page.evaluate(
        """() => {
          const style = getComputedStyle(document.activeElement);
          return [style.outlineStyle, parseFloat(style.outlineWidth)];
        }"""
    )
    assert ring[0] == "solid" and ring[1] >= 2, ring


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_reading_rows_editor_and_chips_fit_360px_in_both_themes(live, signed_in, scheme):
    _context, page = signed_in(
        color_scheme=scheme, viewport={"width": 360, "height": 740}, reduced_motion="reduce"
    )
    meeting_id = seed_meeting(
        live.settings.data_dir, "Narrow reading", "2026-10-14", duration_seconds=30
    )
    long_text = "Sựkiệnkhôngbaogiờngắtdòng" * 6 + " rồi một đoạn lời khá dài. " * 6
    _publish_browser_generation(
        live,
        meeting_id,
        segments=[
            {
                "id": "1" * 32,
                "start_seconds": 1,
                "end_seconds": 3,
                "text": long_text,
                "original_speaker_id": "SPEAKER_01",
                "avg_logprob": -1.4,
            },
            {
                "id": "2" * 32,
                "start_seconds": 4,
                "end_seconds": 6,
                "text": "Một đoạn ngắn.",
                "original_speaker_id": "SPEAKER_00",
            },
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 4,
                "last_seen_seconds": 6,
            },
            {
                "speaker_id": "SPEAKER_01",
                "display_name": "Nguyễn Thị Thanh Hương (Giám đốc điều hành khu vực)",
                "first_seen_seconds": 1,
                "last_seen_seconds": 3,
            },
        ],
    )
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator(".segment-edit-button").first).to_be_enabled()
    assert page.evaluate(OVERFLOW_JS) == {"page": 0, "wide": []}

    # Keyboard focus is a solid 2px ring on the chip and on the Sửa button.
    tab_until(page, "el => el.classList.contains('speaker-chip')")
    assert_focus_ring(page)
    tab_until(page, "el => el.classList.contains('segment-edit-button')")
    assert_focus_ring(page)

    # The motion rule reaches the new controls.
    for selector in (".segment-edit-button", ".speaker-chip"):
        duration = page.locator(selector).first.evaluate(
            "el => parseFloat(getComputedStyle(el).transitionDuration)"
        )
        assert duration <= 0.001, selector

    # Open the first editor from the keyboard; its controls all show the ring too.
    page.keyboard.press("Enter")
    expect(page.locator(".segment-text-editor")).to_be_focused()
    assert_focus_ring(page)
    for _ in range(3):  # Lưu đoạn, Hủy, then the "Chi tiết" summary
        page.keyboard.press("Tab")
        assert_focus_ring(page)
    page.keyboard.press("Enter")
    expect(page.locator("details.segment-details")).to_have_attribute("open", "")

    # Leave a draft in the second row, return to the first, and start a rename in a chip.
    rows = page.locator(".transcript-segment")
    rows.nth(1).locator(".segment-edit-button").click()
    rows.nth(1).locator(".segment-text-editor").fill("Nháp ngắn.")
    rows.nth(0).locator(".segment-edit-button").click()
    rows.nth(0).locator("details summary").click()
    speaker_chip(page, 1).click()  # "Speaker 1": the long name was heard first, so it comes first
    page.locator("#speaker-catalog-list input").fill("Half typed")
    speaker_chip(page, 0).click()
    expect(rows.nth(1).locator(".segment-draft-mark")).to_have_text("Bản nháp chưa lưu")
    expect(speaker_chip(page, 1).locator(".speaker-chip-mark")).to_have_text(
        ["đặt tên", "bản nháp"]
    )
    expect(page.locator(".speaker-name-form")).to_have_count(1)
    assert page.evaluate(OVERFLOW_JS) == {"page": 0, "wide": []}

    # Every piece of new text clears 4.5:1 in this theme.
    ratios = page.evaluate(
        CONTRAST_JS,
        [
            ".segment-text",
            ".segment-speaker",
            ".segment-review-cue",
            ".segment-draft-mark",
            ".segment-edit-button",
            ".segment-timestamp",
            ".segment-speaker-control span",
            ".segment-details summary",
            ".segment-original-speaker",
            ".segment-model-measures",
            ".speaker-chip-name",
            ".speaker-chip-mark",
            ".speaker-name-form label",
        ],
    )
    too_low = {name: ratio for name, ratio in ratios.items() if ratio is None or ratio < 4.5}
    assert not too_low, too_low


def test_rename_label_is_at_least_12px_and_a_long_speaker_id_fits_360px(live, signed_in):
    context, page = signed_in(viewport={"width": 360, "height": 740})
    meeting_id, generation_id = open_reading_meeting(live, page)
    # A speaker added by hand has the longest id the app makes: MANUAL_ and 32 hex digits.
    added = context.request.post(
        f"{live.url}/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers",
        headers={"Origin": live.url},
        data={"expected_source_version": 1, "display_name": ""},
    )
    assert added.ok
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".segment-edit-button").first).to_be_enabled()

    speaker_chip(page, 2).click()
    label = page.locator(".speaker-name-form label")
    expect(label).to_contain_text("MANUAL_")
    assert label.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 12
    assert label.evaluate("el => el.scrollWidth - el.clientWidth") <= 0
    assert page.evaluate(OVERFLOW_JS) == {"page": 0, "wide": []}


def test_failed_speaker_add_reapply_keeps_focus_on_its_button(live, browser_context):
    _context, page = browser_context
    meeting_id = seed_meeting(live.settings.data_dir, "Reapply focus", duration_seconds=30)
    _publish_browser_generation(live, meeting_id)
    page.route(
        re.compile(r"/api/meetings/[^/]+/transcript-generations/[^/]+/speakers$"),
        lambda route: route.fulfill(
            status=500, content_type="application/json", body=json.dumps({"detail": "boom"})
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript", wait_until="domcontentloaded")
    add_name = page.locator("#speaker-add-name")
    add_name.fill("Zed")
    add_name.press("Enter")
    expect(page.locator("#speaker-add-status")).not_to_have_text("")
    reapply = page.get_by_role("button", name="Thêm vào transcript hiện tại")
    expect(reapply).to_be_enabled()
    reapply.click()
    status = page.locator("#transcript-mutation-status")
    expect(status).not_to_have_text("Đang lưu thay đổi transcript.")
    expect(reapply).to_be_enabled()
    expect(reapply).to_be_focused()
