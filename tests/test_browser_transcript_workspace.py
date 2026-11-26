"""Transcript #283 layout and context against the real disposable application."""

from uuid import uuid4

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_history import (
    READING_SEGMENTS,
    READING_SPEAKERS,
    _publish_browser_generation,
    open_reading_meeting,
)
from test_browser_history import live as live
from test_browser_history import ready_rerun_worker as ready_rerun_worker
from test_browser_history import signed_in as signed_in
from test_browser_notes import _write_test_recording

from asr_studio.tasks import (
    claim_next_task,
    publish_complete_generation,
    update_task_progress,
)

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("language", ["vi", "en"])
def test_transcript_first_view_and_context_preserve_editor(live, signed_in, theme, language):
    _context, page = signed_in(viewport={"width": 1440, "height": 900}, color_scheme=theme)
    page.evaluate("lang => localStorage.setItem('asr-studio:lang', lang)", language)
    open_reading_meeting(live, page, title="Transcript workspace")
    page.evaluate("document.fonts.ready")
    search = page.locator("#transcript-search").bounding_box()
    first = page.locator(".transcript-segment").first
    box = first.bounding_box()
    panel = page.locator("#transcript-context")
    assert 0 <= search["y"] < box["y"]
    assert box["y"] + box["height"] <= 900
    assert panel.bounding_box()["x"] > box["x"] + box["width"]
    expect(page.locator("#transcript-processing")).not_to_have_attribute("open", "")
    action = page.locator("#selected-segment-edit")
    expect(action).to_have_text("Edit · 0:01.00" if language == "en" else "Sửa · 0:01.00")
    expect(action).to_have_accessible_name(
        "Details · Edit · 0:01.00" if language == "en" else "Chi tiết · Sửa · 0:01.00"
    )
    expect(first.locator(".segment-edit-button")).to_have_accessible_name(
        "Edit the segment starting at 0:01.00"
        if language == "en"
        else "Sửa đoạn bắt đầu tại 0:01.00"
    )
    expect(page.locator("#transcript-context-summary")).to_have_text(
        "Details · Speaker" if language == "en" else "Chi tiết · Người nói"
    )
    expect(page.locator("#selected-segment-text")).to_have_text("We open with the launch plan.")
    page.locator("#meeting-audio").evaluate("audio => { audio.playbackRate = 1.5; }")
    second = page.locator(".transcript-segment").nth(1)
    second.locator(".segment-edit-button").focus()
    expect(page.locator("#selected-segment-text")).to_have_text(
        second.locator(".segment-text").inner_text()
    )
    page.locator("#selected-segment-edit").click()
    editor = second.locator("textarea")
    expect(editor).to_be_focused()
    assert editor.locator("xpath=ancestor::form").get_attribute(
        "data-generation-id"
    ) == panel.get_attribute("data-generation-id")
    assert editor.locator("xpath=ancestor::form").get_attribute(
        "data-segment-id"
    ) == second.get_attribute("data-segment-id")
    editor.fill("Keep this draft while selecting another row.")
    # Re-entering the same editor from context focuses it without rebuilding its draft.
    page.locator("#selected-segment-edit").click()
    expect(editor).to_be_focused()
    expect(editor).to_have_value("Keep this draft while selecting another row.")
    first.locator(".segment-timestamp").focus()
    expect(editor).to_have_value("Keep this draft while selecting another row.")
    expect(first.locator(".segment-timestamp")).to_be_focused()
    expect(page.locator("#selected-segment-text")).to_have_text("We open with the launch plan.")
    assert page.locator("#meeting-audio").evaluate("audio => audio.playbackRate") == 1.5
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_mobile_context_disclosure_keyboard_and_rename_failure(live, signed_in, theme):
    _context, page = signed_in(viewport={"width": 390, "height": 844}, color_scheme=theme)
    open_reading_meeting(live, page)
    panel = page.locator("#transcript-context")
    expect(panel).not_to_have_attribute("open", "")
    summary = panel.locator(":scope > summary")
    summary.focus()
    summary.press("Enter")
    expect(summary).to_be_focused()
    expect(panel).to_have_attribute("open", "")
    chip = page.locator("#speaker-catalog-list .speaker-chip").first
    chip.click()
    name = page.locator("#speaker-catalog-list input").first
    name.fill("Draft name")
    page.route(
        "**/speakers/*", lambda route: route.fulfill(status=503, json={"detail": "unavailable"})
    )
    form = name.locator("xpath=ancestor::form")
    submit = form.locator("button[type=submit]")
    submit.click()
    expect(submit).to_be_focused()
    expect(name).to_have_value("Draft name")
    expect(form.get_by_role("status")).to_contain_text("Máy chủ chưa thể hoàn tất yêu cầu")
    expect(page.locator("#transcript-mutation-status")).to_contain_text(
        "Máy chủ chưa thể hoàn tất yêu cầu"
    )
    expect(panel).to_have_attribute("open", "")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    summary.focus()
    summary.press("Enter")
    expect(panel).not_to_have_attribute("open", "")
    expect(summary).to_be_focused()


def test_rerun_pending_and_failure_status_stay_visible(live, signed_in, ready_rerun_worker):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    open_reading_meeting(live, page)
    held = []
    page.route("**/api/tasks", lambda route: held.append(route))
    # Open rerun directly while successful processing details remain closed.
    page.locator("#start-transcription").click()
    dialog = page.locator("#transcription-rerun-dialog")
    expect(dialog.locator("#transcription-rerun-submit")).to_be_enabled()
    dialog.locator("#transcription-rerun-submit").click()
    status = page.locator("#transcription-status")
    expect(dialog).not_to_be_visible()
    expect(status).to_be_visible()
    expect(status).to_be_focused()
    assert held
    held[0].fulfill(status=503, json={"detail": "unavailable"})
    expect(page.locator("#transcription-error")).to_be_visible()
    expect(page.locator("#start-transcription")).to_be_focused()
    expect(status).to_be_visible()
    expect(page.locator("#transcript-processing")).to_have_attribute("open", "")


def test_context_selection_keeps_notes_source_and_dirty_draft_identity(live, signed_in):
    from notes_helpers import seed_cited_notes
    from test_browser_notes_citations import CHIP, open_notes

    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting_id, _revision, snapshot, segment_ids = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    source = page.locator("#notes-source-text")
    expect(source).to_have_text(snapshot["segments"][0]["text"])
    source_url = page.url
    mark = page.locator(".transcript-segment.is-notes-source")
    expect(mark).to_have_attribute("data-segment-id", segment_ids[0])
    other = page.locator(".transcript-segment").nth(1)
    other.locator(".segment-edit-button").click()
    editor = other.locator("textarea")
    editor.fill("Unsaved text while inspecting a saved notes source.")
    # Selection is presentation-only: the other source and editor keep their identities.
    page.locator("#selected-segment-edit").focus()
    mark.locator(".segment-timestamp").focus()
    expect(editor).to_have_value("Unsaved text while inspecting a saved notes source.")
    expect(mark).to_have_attribute("data-segment-id", segment_ids[0])
    expect(source).to_have_text(snapshot["segments"][0]["text"])
    assert page.url == source_url
    assert (
        page.locator("#transcript-context").get_attribute("data-generation-id")
        == snapshot["generation_id"]
    )
    assert (
        editor.locator("xpath=ancestor::form").get_attribute("data-generation-id")
        == snapshot["generation_id"]
    )
    assert editor.locator("xpath=ancestor::form").get_attribute("data-segment-id") == segment_ids[1]
    page.locator("#meeting-tab-notes").click()
    expect(page.locator(CHIP).first).to_be_visible()
    page.locator("#meeting-tab-transcript").click()
    expect(editor).to_have_value("Unsaved text while inspecting a saved notes source.")


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("focused_control", ["status", "search"])
def test_completed_rerun_keeps_visible_keyboard_context(
    live, signed_in, ready_rerun_worker, tmp_path, width, focused_control
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting = seed_meeting(live.settings.data_dir, "Rerun focus", "2026-10-14", duration_seconds=30)
    _publish_browser_generation(live, meeting, segments=READING_SEGMENTS, speakers=READING_SPEAKERS)
    _write_test_recording(live.settings.data_dir, meeting, tmp_path)
    page.goto(f"{live.url}/meetings/{meeting}#transcript")
    expect(page.locator(".segment-edit-button").first).to_be_enabled()
    page.locator("#start-transcription").click()
    submit = page.locator("#transcription-rerun-submit")
    expect(submit).to_be_enabled()
    with page.expect_response(
        lambda response: response.url.endswith("/api/tasks") and response.request.method == "POST"
    ) as accepted:
        submit.press("Enter")
    assert accepted.value.status == 202
    task = accepted.value.json()
    status = page.locator("#transcription-status")
    expect(status).to_be_focused()
    search = page.locator("#transcript-search")
    if focused_control == "search":
        search.focus()
        expect(search).to_be_focused()
    owner = "browser-completion-focus"
    claimed = claim_next_task(live.settings.data_dir, owner_id=owner)
    assert claimed and claimed["id"] == task["id"]
    assert update_task_progress(
        live.settings.data_dir,
        task["id"],
        owner_id=owner,
        stage="publishing",
        elapsed_seconds=1,
        transcript_end_seconds=30,
        segment_count=len(READING_SEGMENTS),
    )
    generation = publish_complete_generation(
        live.settings.data_dir,
        task["id"],
        owner_id=owner,
        segments=[dict(segment, id=uuid4().hex) for segment in READING_SEGMENTS],
        speakers=READING_SPEAKERS,
        language_probability=0.9,
        elapsed_seconds=1,
    )
    assert generation
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(page.locator("#transcript-context")).to_have_attribute(
        "data-generation-id", generation["id"]
    )
    details = page.locator("#transcript-processing")
    expect(details).not_to_have_attribute("open", "")
    target = details.locator(":scope > summary") if focused_control == "status" else search
    expect(target).to_be_focused()
    expect(target).to_be_in_viewport()
    assert target.evaluate("""element => {
        const bounds = element.getBoundingClientRect();
        const hit = document.elementFromPoint(bounds.x + bounds.width / 2,
            bounds.y + bounds.height / 2);
        return hit === element || element.contains(hit);
    }""")
    if focused_control == "status":
        assert target.evaluate("element => element.matches(':focus-visible')")
        description = status.text_content().strip()
        assert description
        expect(target).to_have_accessible_description(description)
    page.evaluate("window.scrollBy(0, 180)")
    scroll = page.evaluate("window.scrollY")
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(target).to_be_focused()
    assert page.evaluate("window.scrollY") == pytest.approx(scroll, abs=1)
