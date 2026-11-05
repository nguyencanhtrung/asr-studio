"""Real-browser checks for installing an ASR model from Settings (#159).

The installer service is the real `Installer` running in a thread beside the real app; only its
download child is a script, so a test decides when bytes arrive and when the job finishes.
"""

from __future__ import annotations

import re
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_asr_models import _whisper_dir
from test_browser_history import _prepare_browser_asr_model
from test_browser_history import live as live  # noqa: F401
from test_browser_history import signed_in as signed_in  # noqa: F401
from test_browser_settings import asr_rows, open_section, overflow
from test_browser_settings import cpu_worker as cpu_worker  # noqa: F401
from test_browser_settings import gpu_worker as gpu_worker  # noqa: F401
from test_model_install import CHILD, PHO, QWEN

from asr_studio import model_installer
from asr_studio.model_install import InstallStore
from asr_studio.model_installer import Installer

pytestmark = pytest.mark.browser

FREE = {"bytes": 500 * 10**9}


@pytest.fixture(autouse=True)
def free_space(monkeypatch):
    FREE["bytes"] = 500 * 10**9
    monkeypatch.setattr(
        model_installer.shutil, "disk_usage", lambda _path: SimpleNamespace(free=FREE["bytes"])
    )


def _run_installer(live, tmp_path):
    """The installer service beside the app, with a download child the test steers."""
    models = tmp_path / "models"
    models.mkdir()
    settings = live.settings
    object.__setattr__(settings, "asr_phowhisper_model_path", models / PHO)
    object.__setattr__(settings, "asr_qwen_model_path", models / QWEN)
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    source = _whisper_dir(tmp_path / "source")
    rig = SimpleNamespace(mode="gate", models=models, gate=tmp_path / "gate")

    def argv(_model, staging):
        return [
            sys.executable,
            str(script),
            rig.mode,
            str(source),
            str(staging),
            str(tmp_path / "pids"),
        ]

    rig.service = Installer(
        settings, InstallStore(settings.install_dir), argv=argv, poll_seconds=0.05
    )
    rig.thread = threading.Thread(target=rig.service.serve, daemon=True)
    rig.thread.start()
    deadline = time.monotonic() + 10
    while not InstallStore(settings.install_dir).installer()["available"]:
        assert time.monotonic() < deadline, "installer did not start"
        time.sleep(0.05)
    return rig


def _stop_installer(rig):
    rig.service.stopping.set()
    rig.thread.join(10)


@pytest.fixture
def installer(live, gpu_worker, tmp_path):  # noqa: F811
    rig = _run_installer(live, tmp_path)
    yield rig
    _stop_installer(rig)


@pytest.fixture
def cpu_installer(live, cpu_worker, tmp_path):  # noqa: F811
    rig = _run_installer(live, tmp_path)
    yield rig
    _stop_installer(rig)


def status(row):
    return row.locator('.asr-badge[data-kind="status"]')


def pho_row(page):
    return asr_rows(page).filter(has_text="PhoWhisper")


def open_asr(page, live):
    page.goto(f"{live.url}/")
    open_section(page, "ASR Model")
    expect(asr_rows(page)).to_have_count(4)


def staging(installer):
    return sorted(path.name for path in installer.models.glob(".install-*"))


def test_install_from_settings_shows_real_progress_then_ready_without_a_restart(
    live, signed_in, installer
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    row = pho_row(page)
    expect(status(row)).to_have_text("Chưa chuẩn bị")
    expect(row.get_by_role("button", name="Cài đặt model")).to_be_enabled()
    expect(row).to_contain_text("Cần 8,8 GiB trống")  # the space it needs, before any download
    expect(row.get_by_label("Lệnh chuẩn bị model")).to_have_count(0)
    # Qwen needs a GPU and this worker has one: it offers the same install, with its own size.
    expect(
        asr_rows(page).filter(has_text="Qwen3-ASR").get_by_role("button", name="Cài đặt model")
    ).to_be_enabled()

    row.get_by_role("button", name="Cài đặt model").click()

    expect(status(row)).to_have_text("Đang cài đặt")
    expect(row.locator("progress")).to_be_visible()
    expect(row).to_contain_text("Đang tải model…")
    expect(row.locator(".asr-progress-detail")).to_have_text(
        re.compile(r"^\d+ KiB / 5[.,]8 GiB$")
    )  # real bytes that arrived
    expect(row.get_by_role("button", name="Hủy")).to_be_enabled()
    assert not (installer.models / PHO).exists()  # nothing half-written is visible to the worker
    assert len(staging(installer)) == 1

    installer.gate.write_text("go", encoding="utf-8")

    expect(status(row)).to_have_text("Sẵn sàng", timeout=15_000)
    expect(row.get_by_role("button")).to_have_count(0)
    # The same running app lists it as ready and a new meeting can pick it: no restart.
    catalog = page.request.get(f"{live.url}/api/asr-models").json()["asr_models"]
    assert next(m for m in catalog if m["id"] == PHO)["ready"] is True
    meeting_id = seed_meeting(live.settings.data_dir, "After install")
    offered = page.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert next(m for m in offered["transcription"]["asr_models"] if m["id"] == PHO)["ready"]
    assert staging(installer) == []


def test_cancel_stops_the_install_and_the_row_can_start_again(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    row = pho_row(page)
    row.get_by_role("button", name="Cài đặt model").click()
    cancel = row.get_by_role("button", name="Hủy")
    expect(cancel).to_be_enabled()
    expect(row.locator(".asr-progress-detail")).to_contain_text("/")
    cancel.focus()
    # The bar and the byte count move every second; the button under the keyboard must not.
    before = row.locator(".asr-progress-detail").inner_text()
    page.wait_for_timeout(1800)
    assert row.locator(".asr-progress-detail").inner_text() != before
    expect(cancel).to_be_focused()
    # From the click until the row flips the focus never falls to the page.
    page.evaluate(
        """() => {
          window.focusLost = 0;
          setInterval(() => {
            if (document.activeElement === document.body) window.focusLost++;
          }, 5);
        }"""
    )
    cancel.click()

    expect(row.get_by_role("button", name="Cài đặt model")).to_be_enabled(timeout=15_000)
    expect(status(row)).to_have_text("Chưa chuẩn bị")
    expect(row.locator("progress")).to_have_count(0)
    assert staging(installer) == []  # the partial download is gone
    assert not (installer.models / PHO).exists()
    # The click that replaced Hủy moved the focus to the button that took its place.
    expect(row.get_by_role("button", name="Cài đặt model")).to_be_focused()
    assert page.evaluate("window.focusLost") == 0


def test_a_failed_install_says_why_and_thử_lại_finishes_it(live, signed_in, installer):
    installer.mode = "20"
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    row = pho_row(page)
    row.get_by_role("button", name="Cài đặt model").click()

    expect(row.get_by_role("alert")).to_contain_text("Mất kết nối khi tải model", timeout=15_000)
    expect(status(row)).to_have_text("Chưa chuẩn bị")
    expect(row.locator("progress")).to_have_count(0)
    assert staging(installer) == []

    installer.mode = "ok"
    row.get_by_role("button", name="Thử lại").click()
    expect(status(row)).to_have_text("Sẵn sàng", timeout=15_000)


def test_too_little_free_space_is_told_before_any_download(live, signed_in, installer):
    FREE["bytes"] = 2 * 10**9
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    row = pho_row(page)

    expect(row).to_contain_text("Không đủ dung lượng: cần 8,8 GiB, còn 1,9 GiB")
    expect(row.get_by_role("button", name="Cài đặt model")).to_be_disabled()
    assert staging(installer) == []


def test_qwen_names_the_gpu_requirement_before_any_download_on_a_cpu_machine(
    live, signed_in, cpu_installer
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    qwen = asr_rows(page).filter(has_text="Qwen3-ASR")

    expect(status(qwen)).to_have_text("Cần GPU NVIDIA")
    expect(qwen.get_by_role("button")).to_have_count(0)
    refused = page.request.post(
        f"{live.url}/api/asr-models/{QWEN}/install", headers={"Origin": live.url}
    )
    assert (refused.status, refused.json()["detail"]["code"]) == (409, "gpu_required")
    assert staging(cpu_installer) == []


def test_without_an_installer_the_row_keeps_the_copyable_command(live, signed_in, gpu_worker):  # noqa: F811
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    row = pho_row(page)

    expect(row.get_by_label("Lệnh chuẩn bị model")).to_be_visible()
    expect(row.get_by_role("button", name="Sao chép")).to_be_visible()
    expect(row.get_by_role("button", name="Cài đặt model")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390], ids=["320", "390"])
def test_the_install_block_fits_a_phone_and_keeps_the_type_floor(live, signed_in, installer, width):
    _context, page = signed_in(viewport={"width": width, "height": 640})
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Cài đặt", exact=True).first.click()
    page.get_by_role("tab", name="ASR Model").click()
    row = pho_row(page)
    row.get_by_role("button", name="Cài đặt model").click()
    expect(row.locator("progress")).to_be_visible()

    assert overflow(page) == 0
    dialog = page.locator(".connections-body")
    assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth")
    # Each poll rebuilds the row: read it in one tick from the document, not through a held node.
    sizes = page.evaluate(
        "[...document.querySelectorAll('.asr-install, .asr-install *')]"
        ".map(n => parseFloat(getComputedStyle(n).fontSize))"
    )
    assert sizes and min(sizes) >= 12
    button = row.get_by_role("button", name="Hủy")
    expect(button).to_have_css("font-size", "14px")
    box = button.bounding_box()
    assert box["height"] >= 40 and box["x"] + box["width"] <= width
    installer.gate.write_text("go", encoding="utf-8")


def test_the_page_reads_the_install_only_while_settings_is_open_and_a_job_runs(
    live, signed_in, installer
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    reads = []
    page.on("request", lambda r: reads.append(r.url) if r.url.endswith("/api/asr-models") else None)
    open_asr(page, live)
    pho_row(page).get_by_role("button", name="Cài đặt model").click()
    expect(pho_row(page).locator("progress")).to_be_visible()
    page.wait_for_timeout(1000 * 2.5)
    while_running = len(reads)
    assert while_running >= 3  # it keeps reading while the job runs

    page.keyboard.press("Escape")
    page.wait_for_timeout(1000 * 0.5)
    after_close = len(reads)
    page.wait_for_timeout(1000 * 2.5)
    assert len(reads) == after_close  # a closed dialog reads nothing

    installer.gate.write_text("go", encoding="utf-8")
    page.get_by_role("button", name="Cài đặt", exact=True).first.click()
    page.get_by_role("tab", name="ASR Model").click()
    expect(status(pho_row(page))).to_have_text("Sẵn sàng", timeout=15_000)
    page.wait_for_timeout(1000 * 0.3)
    settled = len(reads)
    page.wait_for_timeout(1000 * 2.5)
    assert len(reads) == settled  # nothing left to wait for: it stops by itself


def test_the_install_text_is_in_english_when_the_page_is(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="ASR Model").click()
    row = pho_row(page)
    expect(row).to_contain_text("of free space needed")
    row.get_by_role("button", name="Install model").click()
    expect(status(row)).to_have_text("Installing")
    expect(row).to_contain_text("Downloading the model…")
    row.get_by_role("button", name="Cancel").click()
    expect(row.get_by_role("button", name="Install model")).to_be_enabled(timeout=15_000)


def test_the_cpu_default_is_named_and_badged_on_a_cpu_machine(live, signed_in, cpu_installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    small = asr_rows(page).filter(has_text="Whisper small")
    large = asr_rows(page).nth(0)  # the small model's note names it too

    expect(page.locator("#asr-default")).to_contain_text("whisper-small")
    expect(small.locator('.asr-badge[data-kind="default"]')).to_have_text("Mặc định")
    expect(status(small)).to_have_text("Sẵn sàng")
    expect(large.locator('.asr-badge[data-kind="default"]')).to_have_count(0)
    expect(large.get_by_role("button", name="Cài đặt model")).to_be_enabled()


def test_a_gpu_machine_can_install_the_small_model_and_it_is_not_the_default(
    live, signed_in, installer
):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    small = asr_rows(page).filter(has_text="Whisper small")

    expect(small.locator('.asr-badge[data-kind="default"]')).to_have_count(0)
    installer.mode = "ok"
    small.get_by_role("button", name="Cài đặt model").click()
    expect(status(small)).to_have_text("Sẵn sàng", timeout=15000)


def default_select(page):
    return page.get_by_label("Model mặc định")


def badge(row):
    return row.locator('.asr-badge[data-kind="default"]')


def test_choose_a_default_in_settings_and_it_survives_a_reload(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    _prepare_browser_asr_model(live.settings.asr_phowhisper_model_path)
    open_asr(page, live)
    pho, large = pho_row(page), asr_rows(page).nth(0)
    select = default_select(page)
    expect(select).to_have_value("auto")
    expect(badge(large)).to_have_text("Mặc định")
    # Not installed and GPU-only rows cannot be picked; this GPU rig has neither Qwen nor small.
    expect(select.locator('option[value="qwen3-asr-1.7b"]')).to_be_disabled()

    select.select_option("pho-whisper-large")

    expect(badge(pho)).to_have_text("Mặc định")
    expect(badge(large)).to_have_count(0)
    expect(page.locator("#asr-default")).to_contain_text("PhoWhisper-large")
    open_asr(page, live)
    expect(default_select(page)).to_have_value("pho-whisper-large")
    expect(badge(pho_row(page))).to_have_text("Mặc định")

    default_select(page).select_option("auto")
    expect(badge(asr_rows(page).nth(0))).to_have_text("Mặc định")
    expect(page.locator("#asr-default")).to_contain_text("whisper-large-v3")


def test_a_default_that_stopped_working_says_so(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    model = _prepare_browser_asr_model(live.settings.asr_phowhisper_model_path)
    open_asr(page, live)
    default_select(page).select_option("pho-whisper-large")
    expect(badge(pho_row(page))).to_have_text("Mặc định")

    shutil.rmtree(model)
    open_asr(page, live)

    expect(default_select(page)).to_have_value("pho-whisper-large")
    expect(page.locator("#asr-default")).to_contain_text("sẽ bị từ chối")


def test_a_refused_default_goes_back_and_says_why(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    model = _prepare_browser_asr_model(live.settings.asr_phowhisper_model_path)
    open_asr(page, live)
    shutil.rmtree(model)  # ready when the page read it, gone when it is chosen

    default_select(page).select_option("pho-whisper-large")

    expect(page.locator("#asr-default-refusal")).to_contain_text("chưa được cài")
    expect(default_select(page)).to_have_value("auto")


@pytest.mark.parametrize("width", [320, 390], ids=["320", "390"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_default_control_fits_a_phone_and_reads_in_english(
    live, signed_in, installer, width, scheme
):
    _context, page = signed_in(viewport={"width": width, "height": 640}, color_scheme=scheme)
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    page.goto(f"{live.url}/")
    page.get_by_role("button", name="Settings", exact=True).first.click()
    page.get_by_role("tab", name="ASR Model").click()
    select = page.get_by_label("Default model")

    expect(select).to_be_visible()
    expect(select.locator('option[value="auto"]')).to_contain_text("Automatic for this hardware")
    assert overflow(page) == 0
    box = select.bounding_box()
    assert box["height"] >= 40 and box["x"] + box["width"] <= width
    select.focus()
    page.keyboard.press("ArrowDown")  # a native select: the keyboard picks it
    expect(select).to_be_focused()


def test_the_default_control_is_locked_while_no_worker_can_answer(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    open_asr(page, live)
    expect(default_select(page)).to_be_disabled()


def test_a_saved_default_that_cannot_be_read_is_reported(live, signed_in, installer):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    (live.settings.data_dir / "asr-default.json").write_text("garbage", encoding="utf-8")
    open_asr(page, live)
    expect(default_select(page)).to_have_value("auto")
    expect(page.locator("#asr-default")).to_contain_text("Không đọc được")
    default_select(page).select_option("whisper-large-v3")
    expect(page.locator("#asr-default")).to_contain_text("Whisper large-v3")
