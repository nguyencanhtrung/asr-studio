"""Check that Automation re-entry reads the other client's committed ASR default."""

from contextlib import suppress

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect
from test_browser_automatic_processing import _open_automatic_processing, _processing_request
from test_browser_history import live as live  # noqa: F401
from test_browser_history import signed_in as signed_in

pytestmark = pytest.mark.browser
pytest_plugins = ("test_browser_asr_model",)


def test_returning_to_automation_reads_asr_default_saved_by_another_client(live, signed_in, worker):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    assert _processing_request(owner, "/api/asr-models")["default_model"] is None
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-asr-owner")
    expect(summary).to_have_attribute("data-state", "ready")
    expect(summary).to_contain_text("Lần chép lời Tự động dùng")
    reader.get_by_role("tab", name="Thiết bị").click()

    selected = _processing_request(
        owner, "/api/asr-models/default", "PUT", {"asr_model": "whisper-large-v3"}
    )
    assert selected["default_model"] == "whisper-large-v3"
    workflow = _processing_request(owner, "/api/processing-preferences")
    assert workflow["asr"]["choice"] == "whisper-large-v3"
    assert _processing_request(owner, "/api/asr-models")["default_model"] == "whisper-large-v3"

    reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(reader.locator("#automatic-preferences-version")).to_have_text(f"v{workflow['version']}")
    expect(summary).to_contain_text("Lần chép lời Tự động dùng", timeout=2200)
    expect(summary).not_to_contain_text("Cài đặt sở hữu hiện là")


def test_later_automation_entry_fences_a_held_older_asr_catalog(live, signed_in, worker):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    older = _processing_request(owner, "/api/asr-models")
    assert older["default_model"] is None
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-asr-owner")
    expect(summary).to_have_attribute("data-state", "ready")
    automation = reader.get_by_role("tab", name="Xử lý tự động")
    devices = reader.get_by_role("tab", name="Thiết bị")
    devices.click()
    held = []
    reads = []
    url = f"{live.url}/api/asr-models"

    def pause_first(route):
        reads.append(route.request.url)
        if not held:
            held.append(route)
        else:
            route.continue_()

    reader.route(url, pause_first)
    with reader.expect_request(url, timeout=2700):
        automation.click()
    expect(summary).to_have_attribute("data-state", "loading")
    devices.click()
    _processing_request(owner, "/api/asr-models/default", "PUT", {"asr_model": "whisper-large-v3"})
    workflow = _processing_request(owner, "/api/processing-preferences")
    automation.click()
    summary.evaluate("""(element) => {
        window.asrSummaryMessages = [];
        new MutationObserver(() => window.asrSummaryMessages.push(element.textContent))
            .observe(element, {childList: true, subtree: true, characterData: true});
    }""")
    with suppress(PlaywrightError):
        held[0].fulfill(status=200, json=older)

    expect(reader.locator("#automatic-preferences-version")).to_have_text(f"v{workflow['version']}")
    expect(summary).to_contain_text("Lần chép lời Tự động dùng", timeout=3300)
    expect(summary).not_to_contain_text("Cài đặt sở hữu hiện là")
    assert len(reads) == 2
    assert not any(
        "Cài đặt sở hữu hiện là" in text for text in reader.evaluate("window.asrSummaryMessages")
    )


def test_automation_owner_refresh_waits_for_held_workflow_version_before_claiming_drift(
    live, signed_in, worker
):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    _processing_request(
        owner,
        "/api/notes-preferences",
        "PUT",
        {"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "low"},
    )
    _open_automatic_processing(reader)
    asr_summary = reader.locator("#automatic-preferences-asr-owner")
    notes_summary = reader.locator("#automatic-preferences-notes-owner")
    expect(asr_summary).to_have_attribute("data-state", "ready")
    expect(notes_summary).to_contain_text("gpt-6.1-sol")
    reader.get_by_role("tab", name="Thiết bị").click()

    _processing_request(
        owner,
        "/api/notes-preferences",
        "PUT",
        {"provider": "chatgpt", "model": "gpt-5.6-luna", "reasoning_effort": "high"},
    )
    _processing_request(owner, "/api/asr-models/default", "PUT", {"asr_model": "whisper-large-v3"})
    latest = _processing_request(owner, "/api/processing-preferences")
    workflow_url = f"{live.url}/api/processing-preferences"
    asr_url = f"{live.url}/api/asr-models"
    held = []
    reader.route(workflow_url, lambda route: held.append(route))
    with (
        reader.expect_response(asr_url, timeout=3000),
        reader.expect_request(workflow_url, timeout=3000),
    ):
        reader.get_by_role("tab", name="Xử lý tự động").click()
    assert len(held) == 1
    expect(notes_summary).to_contain_text("gpt-5.6-luna · effort high")
    expect(notes_summary).not_to_contain_text("Lưu để áp dụng cài đặt sở hữu")
    expect(asr_summary).not_to_contain_text("Cài đặt sở hữu hiện là")
    expect(reader.locator("#automatic-preferences-save")).to_be_disabled()

    held[0].continue_()
    expect(reader.locator("#automatic-preferences-version")).to_have_text(f"v{latest['version']}")
    expect(asr_summary).to_contain_text("Lần chép lời Tự động dùng")
    expect(notes_summary).not_to_contain_text("Lưu để áp dụng cài đặt sở hữu")
