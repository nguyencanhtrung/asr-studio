"""Real-browser checks for the Settings dialog opened from the sidebar gear (#158)."""

from __future__ import annotations

import os
import re

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from test_browser_connections import browser_runtime as browser_runtime  # noqa: F401
from test_browser_connections import signed_in_page
from test_browser_history import _prepare_browser_asr_model
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes_provider import (
    ANTIGRAVITY_MODEL,
    CHATGPT_MODEL,
    accept,
    claim,
    mock_providers,
    save_preference,
)

from asr_studio import __version__
from asr_studio.runtime import RuntimeProfile, write_runtime_profile
from asr_studio.tasks import get_task
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser
VIETNAMESE = re.compile(r"[À-ỹ]")


def start_worker(live, device):
    """A healthy worker on this device whose default model is prepared, as the server sees it."""
    data_dir = live.settings.data_dir
    default_model = _prepare_browser_asr_model(data_dir.parent / "whisper-large-v3")
    object.__setattr__(live.settings, "asr_gpu_model_path", default_model)
    lock = acquire_worker_lock(data_dir)
    assert lock is not None
    write_runtime_profile(data_dir, RuntimeProfile(device, "float16", default_model))
    write_heartbeat(data_dir, "idle")
    return lock


@pytest.fixture
def gpu_worker(live):
    lock = start_worker(live, "cuda")
    yield
    os.close(lock)


@pytest.fixture
def cpu_worker(live):
    lock = start_worker(live, "cpu")
    yield
    os.close(lock)


def overflow(page):
    return page.evaluate("document.documentElement.scrollWidth - innerWidth")


SECTIONS = ["AI provider", "ASR Model", "Theme", "About"]


def open_section(page, name, gear="Cài đặt"):
    page.get_by_role("button", name=gear).click()
    page.get_by_role("tab", name=name).click()


@pytest.mark.parametrize("width", [1440, 390], ids=["1440", "390"])
def test_gear_opens_settings_and_escape_returns_focus(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 800})
    page.goto(f"{live.url}/")
    gear = page.get_by_role("button", name="Cài đặt")
    dialog = page.locator("#connections-dialog")
    expect(gear).to_be_visible()
    expect(gear).to_have_attribute("aria-haspopup", "dialog")
    assert gear.evaluate("element => getComputedStyle(element).borderTopStyle") == "none"
    if width > 680:
        # Beside GitHub in the sidebar footer, on the same line, before it and the same size.
        github = page.locator(".sidebar-footer-meta .github-link")
        gear_box, github_box = gear.bounding_box(), github.bounding_box()
        assert gear_box["width"] == github_box["width"]
        # Icons and Version share one centre line, the footer's own.
        version_centre = page.evaluate(
            """() => {
              const range = document.createRange();
              range.selectNodeContents(document.querySelector(".version-link"));
              const { left, right } = range.getBoundingClientRect();
              return (left + right) / 2;
            }"""
        )
        footer = page.locator(".sidebar-footer-meta").bounding_box()
        icons_centre = (gear_box["x"] + github_box["x"] + github_box["width"]) / 2
        assert abs(icons_centre - version_centre) <= 0.5, (icons_centre, version_centre)
        assert abs(version_centre - (footer["x"] + footer["width"] / 2)) <= 0.5
        assert github_box["x"] - gear_box["x"] - gear_box["width"] <= 10  # a pair, not two far ends
        assert (
            abs(gear_box["y"] + gear_box["height"] / 2 - github_box["y"] - github_box["height"] / 2)
            <= 1
        )
        assert gear_box["x"] + gear_box["width"] <= github_box["x"] + 0.5

    # Mouse.
    gear.click()
    expect(dialog).to_be_visible()
    expect(page.get_by_role("heading", name="Cài đặt", level=2)).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(gear).to_be_focused()

    # Keyboard: Tab reaches the gear with a visible focus ring, Enter opens, Esc returns.
    page.locator("body").click(position={"x": 1, "y": 1})
    gear.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(gear).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    page.keyboard.press("Enter")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(gear).to_be_focused()


def test_provider_buttons_still_open_the_same_dialog_at_their_section(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 700})
    page.goto(f"{live.url}/")
    page.locator("#launcher-antigravity").click()
    expect(page.locator("#connections-dialog")).to_be_visible()
    expect(page.locator("#antigravity-title")).to_be_focused()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Cài đặt").click()
    body = page.locator(".connections-body")
    assert body.evaluate("element => element.scrollTop") == 0


def test_settings_edits_follow_the_sidebar_and_notes_panel_without_reload(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    start = page.locator("#notes-start")
    expect(start).to_have_text("Tạo notes · English · ChatGPT GPT 6.1 Sol")
    expect(page.locator("#launcher-chatgpt")).to_have_attribute("data-state", "connected")

    page.get_by_role("button", name="Cài đặt").click()
    expect(page.locator("#connection-status")).to_have_text("Đã kết nối")
    page.locator("#connection-model").select_option("gpt-6-luna")
    page.locator("#connection-effort").select_option("low")
    page.get_by_role("button", name="Lưu mô hình và effort").click()
    expect(page.locator("#connection-model-status")).to_contain_text("Đã lưu GPT 6 Luna")
    page.keyboard.press("Escape")
    expect(start).to_have_text("Tạo notes · English · ChatGPT GPT 6 Luna")
    expect(page.locator("#notes-run-plan")).to_contain_text("effort low")
    expect(page.locator("#launcher-chatgpt")).to_have_attribute("data-state", "connected")


def test_settings_runs_login_model_effort_verify_and_disconnect(browser_runtime):
    live, browser = browser_runtime
    context, page, api = signed_in_page(browser, live, viewport={"width": 1280, "height": 900})
    api.preferences = {"provider": None, "models": {"chatgpt": "gpt-6.1-sol"}}
    api.poll_statuses = ["pending", "connected"]
    dialog = page.locator("#connections-dialog")
    launcher = page.locator("#launcher-chatgpt")

    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("button", name="Continue with ChatGPT").click()
    expect(page.locator("#connection-status")).to_have_text("Đã kết nối", timeout=8000)
    expect(launcher).to_have_attribute("data-state", "connected")

    page.locator("#connection-model").select_option("gpt-6-luna")
    page.locator("#connection-effort").select_option("low")
    page.get_by_role("button", name="Lưu mô hình và effort").click()
    expect(page.locator("#connection-model-status")).to_contain_text("Đã lưu GPT 6 Luna")
    assert api.preferences["models"] == {"chatgpt": "gpt-6-luna"}
    assert api.preferences["reasoning_effort"] == "low"

    page.get_by_role("button", name="Chạy kiểm tra bằng dữ liệu tổng hợp").click()
    expect(page.locator("#connection-verification-result")).to_contain_text(
        "Đã xác minh GPT 6 Luna"
    )

    page.get_by_role("button", name="Ngắt kết nối ChatGPT").click()
    page.get_by_role("button", name="Ngắt kết nối", exact=True).click()
    expect(page.locator("#connection-status")).to_have_text("Chưa kết nối")
    expect(launcher).to_have_attribute("aria-label", "Kết nối OpenAI: chưa kết nối")
    expect(dialog).to_be_visible()
    context.close()


def test_a_busy_provider_keeps_its_message_and_the_running_task_keeps_its_model(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    running = accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="settings-running",
        provider="antigravity",
    )
    claim(live.settings.data_dir, running["id"])
    mock_providers(page, live)
    providers = [
        {"id": "chatgpt", "status": "connected", "connection_version": 7},
        {
            "id": "antigravity",
            "status": "connected",
            "connection_version": 3,
            "in_use": True,
            "in_use_reason": "notes",
        },
    ]
    page.route(
        f"{live.url}/api/providers", lambda route: route.fulfill(json={"providers": providers})
    )
    page.goto(f"{live.url}/")

    page.get_by_role("button", name="Cài đặt").click()
    # The provider in use shows the existing message and cannot be changed from here.
    reason = page.locator("#antigravity-mutation-reason")
    expect(reason).to_contain_text("đang được một tác vụ notes khác sử dụng")
    expect(page.locator("#antigravity-model")).to_be_disabled()
    expect(page.locator("#antigravity-model-save")).to_be_disabled()
    expect(page.locator("#antigravity-disconnect")).to_be_disabled()

    # The other provider is free; changing it leaves the running task as it was accepted.
    page.locator("#connection-model").select_option("gpt-6-luna")
    page.get_by_role("button", name="Lưu mô hình và effort").click()
    expect(page.locator("#connection-model-status")).to_contain_text("Đã lưu GPT 6 Luna")
    task = get_task(live.settings.data_dir, running["id"])
    assert task["status"] == "running"
    assert task["model"] == ANTIGRAVITY_MODEL == running["model"]
    assert task["reasoning_effort"] == running["reasoning_effort"]


def asr_rows(page):
    return page.locator("#asr-models > li")


def test_asr_section_shows_real_readiness_and_a_copyable_prepare_command(
    live, signed_in, gpu_worker
):
    context, page = signed_in(viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    object.__setattr__(
        live.settings,
        "asr_phowhisper_model_path",
        _prepare_browser_asr_model(live.settings.data_dir.parent / "pho-whisper-large"),
    )
    page.goto(f"{live.url}/")
    catalog = page.request.get(f"{live.url}/api/asr-models").json()["asr_models"]
    qwen = next(model for model in catalog if model["id"] == "qwen3-asr-1.7b")

    open_section(page, "ASR Model")
    expect(asr_rows(page)).to_have_count(3)
    names = [row.locator("strong").inner_text() for row in asr_rows(page).all()]
    assert names == ["Whisper large-v3", "PhoWhisper-large", "Qwen3-ASR-1.7B"]
    whisper, pho, qwen_row = asr_rows(page).all()
    expect(page.locator("#asr-default")).to_contain_text("whisper-large-v3")
    expect(whisper.locator('.asr-badge[data-kind="default"]')).to_have_text("Mặc định")
    expect(pho.locator('.asr-badge[data-kind="default"]')).to_have_count(0)
    expect(whisper.locator('.asr-badge[data-kind="status"]')).to_have_text("Sẵn sàng")
    expect(pho.locator('.asr-badge[data-kind="status"]')).to_have_text("Sẵn sàng")
    expect(whisper.locator("input")).to_have_count(0)

    expect(qwen_row.locator('.asr-badge[data-kind="status"]')).to_have_text("Chưa chuẩn bị")
    expect(qwen_row).to_contain_text("chưa được chuẩn bị")
    command = qwen_row.get_by_label("Lệnh chuẩn bị model")
    assert command.input_value() == qwen["prepare_command"]
    assert "--asr-model qwen3-asr-1.7b" in qwen["prepare_command"]
    # The model name is at the end of the command: all of it shows, none is scrolled away.
    assert command.evaluate(
        "e => e.scrollHeight <= e.clientHeight && e.scrollWidth <= e.clientWidth"
    )
    qwen_row.get_by_role("button", name="Sao chép").click()
    expect(qwen_row.locator(".asr-command-result")).to_have_text("Đã sao chép lệnh.")
    assert page.evaluate("navigator.clipboard.readText()") == qwen["prepare_command"]


def test_asr_copy_refused_selects_the_command_and_says_so(live, signed_in, gpu_worker):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', "
        "{value: {writeText: () => Promise.reject(new Error('denied'))}});"
    )
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(asr_rows(page)).to_have_count(3)
    qwen_row = asr_rows(page).nth(2)
    qwen_row.get_by_role("button", name="Sao chép").click()
    expect(qwen_row.locator(".asr-command-result")).to_have_text(
        "Không sao chép được. Lệnh đã được chọn; nhấn Ctrl+C."
    )
    assert page.evaluate("document.activeElement.id") == "asr-command-qwen3-asr-1.7b"
    selected = page.evaluate(
        "document.activeElement.value.slice("
        "document.activeElement.selectionStart, document.activeElement.selectionEnd)"
    )
    assert selected == page.locator("#asr-command-qwen3-asr-1\\.7b").input_value()


@pytest.mark.parametrize("models", [[], [None, 5, {"id": "evil"}]], ids=["empty", "unusable"])
def test_asr_section_says_so_when_the_catalog_has_no_usable_model(live, signed_in, models):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.route(
        f"{live.url}/api/asr-models", lambda route: route.fulfill(json={"asr_models": models})
    )
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(page.locator("#asr-status")).to_have_text(
        "Máy chủ không trả về model nào. Mở lại Cài đặt để thử lại."
    )
    expect(asr_rows(page)).to_have_count(0)


def test_qwen_says_plainly_that_it_needs_a_gpu_when_the_worker_is_on_cpu(
    live, signed_in, cpu_worker
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(asr_rows(page)).to_have_count(3)
    qwen = asr_rows(page).nth(2)
    expect(qwen.locator('.asr-badge[data-kind="status"]')).to_have_text("Cần GPU NVIDIA")
    expect(qwen).to_contain_text("Model này chỉ chạy trên GPU NVIDIA, còn worker đang chạy CPU.")
    expect(qwen.locator("input")).to_have_count(0)


def test_asr_section_says_so_when_no_worker_can_report_readiness(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(asr_rows(page)).to_have_count(3)
    expect(page.locator("#asr-status")).to_contain_text("Worker chép lời chưa sẵn sàng")
    expect(page.locator("#asr-default")).to_be_hidden()
    for row in asr_rows(page).all():
        expect(row.locator('.asr-badge[data-kind="status"]')).to_have_text("Chưa kiểm tra được")
        expect(row).not_to_contain_text("Worker chép lời chưa sẵn sàng")  # said once, above


def test_asr_section_reports_a_failed_read(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.route(f"{live.url}/api/asr-models", lambda route: route.fulfill(status=500, json={}))
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(page.locator("#asr-status")).to_contain_text("Không tải được danh sách model")
    expect(asr_rows(page)).to_have_count(0)


@pytest.mark.parametrize("width", [390, 360, 320], ids=["390", "360", "320"])
def test_phone_settings_open_from_the_top_bar_and_the_dialog_scrolls(
    live, signed_in, gpu_worker, width
):
    _context, page = signed_in(viewport={"width": width, "height": 600})
    mock_providers(page, live)
    page.goto(f"{live.url}/")
    gear = page.get_by_role("button", name="Cài đặt")
    expect(gear).to_be_visible()
    box = gear.bounding_box()
    assert box["y"] + box["height"] <= 70, box  # in the top bar, not in a footer
    assert 34 <= box["width"] <= 40 and abs(box["width"] - box["height"]) <= 0.5
    sidebar = page.locator(".sidebar").bounding_box()
    assert box["x"] + box["width"] <= sidebar["x"] + sidebar["width"] + 0.5
    assert overflow(page) <= 0
    expect(page.get_by_role("link", name=re.compile("ASR Studio"))).to_be_visible()

    # Tab order equals the visual order: language, OpenAI, Google, then the gear.
    page.locator(".sidebar-masthead .lang-toggle").focus()
    for expected in ("#launcher-chatgpt", "#launcher-antigravity", "#settings-open"):
        page.keyboard.press("Tab")
        expect(page.locator(expected)).to_be_focused()

    open_section(page, "ASR Model")
    dialog = page.locator("#connections-dialog")
    expect(dialog).to_be_visible()
    expect(asr_rows(page)).to_have_count(3)
    assert overflow(page) <= 0
    body = page.locator(".connections-body")
    assert body.evaluate("element => element.scrollHeight > element.clientHeight")
    body.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    last, shell = (
        asr_rows(page).last.bounding_box(),
        page.locator(".connections-shell").bounding_box(),
    )
    assert last["y"] + last["height"] <= shell["y"] + shell["height"] + 0.5
    assert shell["x"] >= 0 and shell["x"] + shell["width"] <= width
    assert overflow(page) <= 0


def test_settings_is_complete_in_english(live, signed_in, gpu_worker):
    context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.evaluate("localStorage.setItem('asr-studio:lang', 'en')")
    mock_providers(page, live)
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Settings").click()
    expect(page.get_by_role("heading", name="Settings", level=2)).to_be_visible()
    text = page.locator("#connections-dialog").inner_text()
    assert VIETNAMESE.findall(text) == [], text
    for name in ("Theme", "About"):
        page.get_by_role("tab", name=name).click()
        expect(page.get_by_role("heading", name=name, level=3)).to_be_visible()
        text = page.locator("#connections-dialog").inner_text()
        assert VIETNAMESE.findall(text) == [], text
    expect(page.locator("#settings-panel-about")).to_contain_text("Trung Nguyen")
    page.get_by_role("tab", name="ASR Model").click()
    expect(asr_rows(page)).to_have_count(3)
    expect(asr_rows(page).nth(2)).to_contain_text("Not prepared")
    expect(asr_rows(page).nth(0).locator('.asr-badge[data-kind="default"]')).to_have_text("Default")
    text = page.locator("#connections-dialog").inner_text()
    assert VIETNAMESE.findall(text) == [], text
    context.close()


@pytest.mark.parametrize("width", [1440, 1024, 768, 681], ids=str)
def test_sections_sit_left_of_the_content_and_each_shows_alone(live, signed_in, gpu_worker, width):
    _context, page = signed_in(viewport={"width": width, "height": 800})
    mock_providers(page, live)
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    tabs = page.locator("#settings-tabs [role=tab]")
    assert [tab.inner_text() for tab in tabs.all()] == SECTIONS
    shell = page.locator(".connections-shell")
    size = shell.bounding_box()
    body = page.locator(".connections-body")
    for name in SECTIONS:
        tab = page.get_by_role("tab", name=name)
        tab.click()
        expect(tab).to_have_attribute("aria-selected", "true")
        panels = page.locator("[role=tabpanel]:visible")
        expect(panels).to_have_count(1)
        expect(panels.get_by_role("heading", name=name, level=3)).to_be_visible()
        assert shell.bounding_box() == size, name  # moving between sections never resizes it
        tab_box, content = tab.bounding_box(), body.bounding_box()
        assert tab_box["x"] + tab_box["width"] <= content["x"] + 0.5, name
        assert content["y"] <= tab_box["y"] <= content["y"] + content["height"], name
        assert overflow(page) <= 0, name

    # Only the content column scrolls.
    page.get_by_role("tab", name="AI provider").click()
    assert body.evaluate("element => element.scrollHeight > element.clientHeight")
    assert shell.evaluate("element => element.scrollHeight <= element.clientHeight")


def test_sections_move_with_the_keyboard_and_tab_goes_into_the_content(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="AI provider").focus()
    for key, name in (
        ("ArrowDown", "ASR Model"),
        ("ArrowRight", "Theme"),
        ("End", "About"),
        ("ArrowDown", "AI provider"),
        ("ArrowUp", "About"),
        ("Home", "AI provider"),
        ("ArrowLeft", "About"),
        ("ArrowUp", "Theme"),
    ):
        page.keyboard.press(key)
        tab = page.get_by_role("tab", name=name)
        expect(tab).to_be_focused()
        expect(tab).to_have_attribute("aria-selected", "true")
        assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    assert page.locator('#settings-tabs [role=tab][tabindex="0"]').count() == 1
    page.keyboard.press("Tab")
    expect(page.locator("#settings-panel-theme")).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.name") == "settings-language"


def test_tab_reaches_a_section_that_holds_no_control(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.route(f"{live.url}/api/asr-models", lambda route: route.fulfill(json={"asr_models": []}))
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="AI provider").focus()
    page.keyboard.press("ArrowDown")
    expect(page.locator("#asr-status")).to_contain_text("Máy chủ không trả về model nào")
    page.keyboard.press("Tab")
    expect(page.locator("#settings-panel-asr")).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"


def test_in_forced_colors_only_the_selected_section_draws_a_border(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.emulate_media(forced_colors="active")
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    color = "element => getComputedStyle(element).borderTopColor"
    selected = page.get_by_role("tab", name="AI provider").evaluate(color)
    canvas = page.locator(".connections-shell").evaluate("e => getComputedStyle(e).backgroundColor")
    for name in ("ASR Model", "Theme", "About"):
        assert page.get_by_role("tab", name=name).evaluate(color) == canvas, name
    assert selected != canvas


def test_a_disconnected_provider_hides_what_only_an_account_can_use(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    expect(page.locator("#connection-state-badge")).to_have_text("Chưa kết nối")
    expect(page.locator("#antigravity-badge")).to_have_text("Chưa kết nối")
    expect(page.get_by_role("heading", name="ChatGPT", level=4)).to_be_visible()
    for hidden in (
        "#connection-ledger",
        "#antigravity-ledger",
        "#connection-verification-section",
        "#antigravity-verification-section",
    ):
        expect(page.locator(hidden)).to_be_hidden()
    expect(page.locator("#connection-sign-in")).to_be_visible()
    expect(page.locator("#antigravity-sign-in")).to_be_visible()
    # A disconnect whose remote revocation is unconfirmed sends the user to this page.
    expect(page.locator(".connection-usage-link")).to_be_visible()


def test_the_open_section_has_focus_so_arrows_work_at_once(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("tab", name="AI provider")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("tab", name="ASR Model")).to_be_focused()
    orientation = page.locator("#settings-tabs")
    expect(orientation).to_have_attribute("aria-orientation", "vertical")
    page.set_viewport_size({"width": 390, "height": 700})
    expect(orientation).to_have_attribute("aria-orientation", "horizontal")


def test_selected_choices_differ_by_more_than_colour_and_columns_are_even(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.emulate_media(reduced_motion="reduce")  # reads final colours, not a 150ms transition
    page.goto(f"{live.url}/")
    open_section(page, "Theme")
    border = "e => getComputedStyle(e).borderTopColor"
    weight = "e => getComputedStyle(e).fontWeight"
    selected, plain = page.get_by_role("tab", name="Theme"), page.get_by_role("tab", name="About")
    page.wait_for_timeout(50)  # even a reduced transition settles only on the next frame
    assert selected.evaluate(border) != plain.evaluate(border)
    assert int(selected.evaluate(weight)) >= int(plain.evaluate(weight)) + 200
    modes = page.locator("#settings-panel-theme fieldset").nth(1).locator("label")
    widths = {round(label.bounding_box()["width"]) for label in modes.all()}
    assert len(widths) == 1, widths
    checked = modes.first
    other = modes.nth(1)
    assert checked.evaluate(border) != other.evaluate(border)
    page.get_by_role("radio", name="Theo hệ thống").focus()
    page.keyboard.press("ArrowRight")
    ring = "e => getComputedStyle(e).outlineStyle"
    assert other.evaluate(ring) == "solid"


def test_each_opener_lands_on_ai_provider_and_a_new_section_starts_at_its_top(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 700})
    page.goto(f"{live.url}/")
    gear = page.get_by_role("button", name="Cài đặt")
    ai = page.get_by_role("tab", name="AI provider")
    body = page.locator(".connections-body")

    open_section(page, "About")
    page.keyboard.press("Escape")
    gear.click()
    expect(ai).to_have_attribute("aria-selected", "true")
    body.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    page.get_by_role("tab", name="ASR Model").click()
    assert body.evaluate("element => element.scrollTop") == 0
    page.get_by_role("tab", name="About").click()
    page.keyboard.press("Escape")

    # From another section, Google still lands on its own part of AI provider.
    page.locator("#launcher-antigravity").click()
    expect(ai).to_have_attribute("aria-selected", "true")
    expect(page.locator("#antigravity-title")).to_be_focused()
    expect(page.locator("#antigravity-title")).to_be_in_viewport()
    page.keyboard.press("Escape")
    expect(page.locator("#launcher-antigravity")).to_be_focused()
    page.locator("#launcher-chatgpt").click()
    expect(ai).to_have_attribute("aria-selected", "true")
    assert body.evaluate("element => element.scrollTop") == 0


def test_display_mode_shows_what_is_in_use_and_follows_every_other_way_of_changing_it(
    live, signed_in
):
    context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.emulate_media(color_scheme="light")
    page.goto(f"{live.url}/")
    root = page.locator("html")
    stored = "localStorage.getItem('asr-studio:theme')"
    system = page.get_by_role("radio", name="Theo hệ thống")
    light = page.get_by_role("radio", name="Sáng")
    dark = page.get_by_role("radio", name="Tối")

    open_section(page, "Theme")
    expect(system).to_be_checked()
    expect(root).to_have_attribute("data-theme", "light")

    dark.check()
    expect(root).to_have_attribute("data-theme", "dark")
    assert page.evaluate(stored) == "dark"

    page.reload()
    expect(page.locator("#connections-dialog")).to_be_hidden()  # only a language change reopens it
    expect(root).to_have_attribute("data-theme", "dark")

    # The sidebar toggle changes it from outside the dialog.
    page.locator(".sidebar-masthead [data-theme-toggle]").click()
    expect(root).to_have_attribute("data-theme", "light")
    open_section(page, "Theme")
    expect(light).to_be_checked()

    # Back to the system default: nothing stored, and it follows the OS again.
    system.check()
    assert page.evaluate(stored) is None
    page.emulate_media(color_scheme="dark")
    expect(root).to_have_attribute("data-theme", "dark")
    expect(system).to_be_checked()

    # Another tab of the same browser.
    other = context.new_page()
    other.goto(f"{live.url}/")
    open_section(other, "Theme")
    other.get_by_role("radio", name="Sáng").check()
    expect(light).to_be_checked()
    expect(root).to_have_attribute("data-theme", "light")
    other.close()


def test_language_choice_reloads_into_settings_at_theme_once(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/")
    dialog = page.locator("#connections-dialog")
    root = page.locator("html")
    open_section(page, "Theme")
    expect(page.get_by_role("radio", name="Tiếng Việt")).to_be_checked()

    with page.expect_navigation():
        page.get_by_role("radio", name="English").check()
    expect(root).to_have_attribute("lang", "en")
    expect(dialog).to_be_visible()
    expect(page.get_by_role("tab", name="Theme")).to_have_attribute("aria-selected", "true")
    english = page.get_by_role("radio", name="English")
    expect(english).to_be_checked()
    expect(english).to_be_focused()
    expect(page.get_by_role("radio", name="Vietnamese")).not_to_be_checked()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("button", name="Settings")).to_be_focused()

    # It reopens once: a later reload is an ordinary page load.
    assert page.evaluate("sessionStorage.getItem('asr-studio:settings-section')") is None
    page.reload()
    expect(root).to_have_attribute("lang", "en")
    expect(dialog).to_be_hidden()

    # The sidebar toggle changes it from outside, and does not open Settings.
    with page.expect_navigation():
        page.locator(".sidebar-masthead [data-lang-toggle]").click()
    expect(root).to_have_attribute("lang", "vi")
    expect(dialog).to_be_hidden()
    open_section(page, "Theme")
    expect(page.get_by_role("radio", name="Tiếng Việt")).to_be_checked()


def test_language_choice_keeps_the_page_language_when_it_cannot_be_stored(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.add_init_script(
        "const setItem = Storage.prototype.setItem;"
        "Storage.prototype.setItem = function (key, value) {"
        "  if (key === 'asr-studio:lang') throw new Error('blocked');"
        "  return setItem.call(this, key, value);"
        "};"
    )
    page.goto(f"{live.url}/")
    open_section(page, "Theme")
    page.evaluate("window.notReloaded = true")
    page.get_by_role("radio", name="English").click()
    expect(page.get_by_role("radio", name="Tiếng Việt")).to_be_checked()
    assert page.evaluate("window.notReloaded") is True
    assert page.evaluate("sessionStorage.getItem('asr-studio:settings-section')") is None
    expect(page.locator("html")).to_have_attribute("lang", "vi")


def test_language_still_applies_when_the_return_to_settings_cannot_be_stored(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.add_init_script(
        "const setItem = Storage.prototype.setItem;"
        "Storage.prototype.setItem = function (key, value) {"
        "  if (key === 'asr-studio:settings-section') throw new Error('blocked');"
        "  return setItem.call(this, key, value);"
        "};"
    )
    page.goto(f"{live.url}/")
    open_section(page, "Theme")
    with page.expect_navigation():
        page.get_by_role("radio", name="English").click()  # check() would wait on the closed dialog
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#connections-dialog")).to_be_hidden()


def test_about_names_the_software_its_real_version_and_the_developer(live, signed_in):
    context, page = signed_in(viewport={"width": 1280, "height": 800})
    page.goto(f"{live.url}/")
    open_section(page, "About")
    panel = page.locator("#settings-panel-about")
    expect(panel).to_contain_text("ASR Studio chép lời và ghi chú cuộc họp")
    expect(panel).to_contain_text("Nguyễn Cảnh Trung")
    expect(page.locator("#settings-version")).to_have_text(__version__)
    link = panel.get_by_role("link", name="bibo.id.vn")
    assert link.get_attribute("href") == "https://bibo.id.vn"
    assert link.get_attribute("target") == "_blank"
    assert {"noopener", "noreferrer"} <= set(link.get_attribute("rel").split())

    context.route("https://bibo.id.vn/**", lambda route: route.fulfill(body="site"))
    with context.expect_page() as opened:
        link.click()
    assert opened.value.url.rstrip("/") == "https://bibo.id.vn"
    expect(page.locator("#connections-dialog")).to_be_visible()


@pytest.mark.parametrize("width", [390, 360, 320], ids=str)
def test_phone_sections_become_a_row_above_the_content(live, signed_in, gpu_worker, width):
    _context, page = signed_in(viewport={"width": width, "height": 640})
    mock_providers(page, live)
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    shell = page.locator(".connections-shell").bounding_box()
    body = page.locator(".connections-body")

    # Both sections are longer than the column here, so a kept scroll would really show.
    page.get_by_role("tab", name="ASR Model").click()
    expect(asr_rows(page)).to_have_count(3)
    body.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    assert body.evaluate("element => element.scrollTop") > 0
    page.get_by_role("tab", name="AI provider").click()
    assert body.evaluate("element => element.scrollTop") == 0

    for name in SECTIONS:
        tab = page.get_by_role("tab", name=name)
        tab.click()
        box, content = tab.bounding_box(), body.bounding_box()
        assert box["y"] + box["height"] <= content["y"] + 0.5, name
        assert shell["x"] <= box["x"] and box["x"] + box["width"] <= shell["x"] + shell["width"]
        assert box["height"] >= 44, name
        assert tab.evaluate("element => element.scrollWidth <= element.clientWidth"), name
        assert body.evaluate("element => element.scrollWidth <= element.clientWidth"), name
        assert overflow(page) <= 0, name
    # The prepare command takes the full width with Copy under it, styled like other buttons.
    page.get_by_role("tab", name="ASR Model").click()
    qwen = asr_rows(page).nth(2)
    command, copy = qwen.locator("textarea"), qwen.get_by_role("button", name="Sao chép")
    assert command.evaluate(
        "e => e.scrollHeight <= e.clientHeight && e.scrollWidth <= e.clientWidth"
    )
    area, button = command.bounding_box(), copy.bounding_box()
    assert area["y"] + area["height"] <= button["y"], (area, button)
    assert copy.evaluate("e => getComputedStyle(e).fontWeight") == "680"

    # Tidy rows: one row when all four fit, otherwise two even rows, never one left alone.
    rows = {}
    for tab in page.locator("#settings-tabs [role=tab]").all():
        rows.setdefault(round(tab.bounding_box()["y"]), []).append(tab)
    assert sorted(len(row) for row in rows.values()) == ([4] if width > 360 else [2, 2]), rows
    page.get_by_role("tab", name="Theme").click()
    for radio in page.locator("#settings-panel-theme label").all():
        label = radio.bounding_box()
        assert label["x"] + label["width"] <= shell["x"] + shell["width"]
