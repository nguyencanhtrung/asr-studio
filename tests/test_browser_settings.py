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


@pytest.mark.parametrize("width", [1440, 390], ids=["1440", "390"])
def test_gear_opens_settings_and_escape_returns_focus(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 800})
    page.goto(f"{live.url}/")
    gear = page.get_by_role("button", name="Cài đặt")
    dialog = page.locator("#connections-dialog")
    expect(gear).to_be_visible()
    expect(gear).to_have_attribute("aria-haspopup", "dialog")
    if width > 680:
        # Beside GitHub in the sidebar footer, on the same line and before it.
        github = page.locator(".sidebar-footer-meta .github-link")
        gear_box, github_box = gear.bounding_box(), github.bounding_box()
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

    page.get_by_role("button", name="Cài đặt").click()
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
    page.get_by_role("button", name="Cài đặt").click()
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
    page.get_by_role("button", name="Cài đặt").click()
    expect(page.locator("#asr-status")).to_have_text(
        "Máy chủ không trả về model nào. Mở lại Cài đặt để thử lại."
    )
    expect(asr_rows(page)).to_have_count(0)


def test_qwen_says_plainly_that_it_needs_a_gpu_when_the_worker_is_on_cpu(
    live, signed_in, cpu_worker
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    expect(asr_rows(page)).to_have_count(3)
    qwen = asr_rows(page).nth(2)
    expect(qwen.locator('.asr-badge[data-kind="status"]')).to_have_text("Cần GPU NVIDIA")
    expect(qwen).to_contain_text("Model này chỉ chạy trên GPU NVIDIA, còn worker đang chạy CPU.")
    expect(qwen.locator("input")).to_have_count(0)


def test_asr_section_says_so_when_no_worker_can_report_readiness(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
    expect(asr_rows(page)).to_have_count(3)
    for row in asr_rows(page).all():
        expect(row.locator('.asr-badge[data-kind="status"]')).to_have_text("Chưa kiểm tra được")
        expect(row).to_contain_text("Worker chép lời chưa sẵn sàng")


def test_asr_section_reports_a_failed_read(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.route(f"{live.url}/api/asr-models", lambda route: route.fulfill(status=500, json={}))
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt").click()
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

    gear.click()
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
    expect(page.get_by_role("heading", name="AI provider", level=3)).to_be_visible()
    expect(page.get_by_role("heading", name="ASR models", level=3)).to_be_visible()
    expect(asr_rows(page)).to_have_count(3)
    expect(asr_rows(page).nth(2)).to_contain_text("Not prepared")
    expect(asr_rows(page).nth(0).locator('.asr-badge[data-kind="default"]')).to_have_text("Default")
    text = page.locator("#connections-dialog").inner_text()
    assert VIETNAMESE.findall(text) == [], text
    context.close()
