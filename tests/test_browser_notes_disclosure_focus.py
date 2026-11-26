from __future__ import annotations

import pytest
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes import _seed_saved_notes
from test_browser_notes import signed_in as signed_in
from test_browser_notes_provider import accept, claim

from asr_studio.tasks import fail_task

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("phase", ["queued", "failed"])
@pytest.mark.parametrize("focused_control", ["summary", "tab"])
def test_external_notes_run_keeps_focus_on_a_visible_control(
    live, signed_in, width, phase, focused_control
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting}#notes")
    details = page.locator("#notes-task-details")
    summary = details.locator("summary")
    expect(summary).to_be_visible()
    initial_focus = summary if focused_control == "summary" else page.locator("#meeting-tab-notes")
    initial_focus.focus()
    expect(initial_focus).to_be_focused()

    task = accept(
        live.settings.data_dir,
        meeting,
        snapshot["generation_id"],
        request_id="external-focus-transition",
    )
    if phase == "failed":
        owner = claim(live.settings.data_dir, task["id"])
        assert fail_task(
            live.settings.data_dir,
            task["id"],
            owner_id=owner,
            error_code="provider_quota",
            message="Fixture provider limit",
            elapsed_seconds=1,
        )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(summary).to_be_hidden()
    expect(details).to_have_attribute("open", "")
    expect(page.locator("#notes-status")).not_to_be_empty()
    target = (
        initial_focus
        if focused_control == "tab"
        else page.locator("#notes-cancel" if phase == "queued" else "#notes-status")
    )
    expect(target).to_be_focused()
    expect(target).to_be_in_viewport()
    assert details.bounding_box()["y"] < page.locator("#notes-saved-work").bounding_box()["y"]
