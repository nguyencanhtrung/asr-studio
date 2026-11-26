from __future__ import annotations

import pytest
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes import _seed_saved_notes
from test_browser_notes import signed_in as signed_in
from test_browser_notes_provider import accept, claim

from asr_studio.tasks import fail_task, update_task_progress

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("language", ["vi", "en"])
def test_saved_notes_prioritize_content_and_keep_processing_details(
    live, signed_in, width, scheme, language
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    page.emulate_media(color_scheme=scheme)
    page.evaluate("lang => localStorage.setItem('asr-studio:lang', lang)", language)
    meeting, _revision, _snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting}#notes")
    preview = page.locator("#notes-preview")
    expect(preview.locator(":scope > *").first).to_be_visible()
    expect(page.locator("#notes-panel")).to_have_accessible_name("Meeting notes")
    if width == 1440:
        assert preview.bounding_box()["y"] < 650
        first_paragraph = preview.locator("li, p").first.bounding_box()
        assert first_paragraph["y"] + first_paragraph["height"] < 900
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    details = page.locator("#notes-task-details")
    expect(details).not_to_have_attribute("open")
    details.locator("summary").press("Enter")
    expect(details.locator("summary")).to_be_focused()
    expect(page.locator("#notes-steps")).to_be_visible()
    # An ordinary refresh must respect the reader's expanded disclosure.
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(details).to_have_attribute("open", "")
    details.locator("summary").press("Enter")
    expect(details).not_to_have_attribute("open")
    expect(page.locator("#notes-export")).to_be_visible()
    expect(page.locator("#notes-copy-email")).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_running_and_failed_details_and_controls_precede_saved_document(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    task = accept(
        live.settings.data_dir,
        meeting,
        snapshot["generation_id"],
        request_id="layout-progress",
    )
    owner = claim(live.settings.data_dir, task["id"])
    assert update_task_progress(
        live.settings.data_dir,
        task["id"],
        owner_id=owner,
        stage="chunking",
        elapsed_seconds=12,
        transcript_end_seconds=0,
        segment_count=0,
        progress_current=1,
        progress_total=3,
        progress_unit="chunks",
    )
    page.goto(f"{live.url}/meetings/{meeting}#notes")
    details = page.locator("#notes-task-details")
    expect(page.locator("#notes-task-progress")).to_contain_text("1/3")
    expect(details).to_have_attribute("open", "")
    expect(details.locator("summary")).to_be_hidden()
    cancel = page.locator("#notes-cancel")
    expect(cancel).to_be_enabled()
    cancel.focus()
    expect(cancel).to_be_focused()
    for selector in ("#notes-run", "#notes-task-details"):
        assert (
            page.locator(selector).bounding_box()["y"]
            < page.locator("#notes-preview").bounding_box()["y"]
        )
    assert fail_task(
        live.settings.data_dir,
        task["id"],
        owner_id=owner,
        error_code="provider_quota",
        message="Private provider error must not be rendered.",
        elapsed_seconds=12,
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(page.locator("#notes-status")).to_contain_text("hạn mức", timeout=10_000)
    expect(page.locator("#notes-steps")).to_be_visible()
    expect(details).to_have_attribute("open", "")
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(cancel).to_be_hidden()
    expect(page.locator("#notes-panel")).not_to_contain_text("Private provider error")
    assert (
        page.locator("#notes-run").bounding_box()["y"]
        < page.locator("#notes-preview").bounding_box()["y"]
    )
