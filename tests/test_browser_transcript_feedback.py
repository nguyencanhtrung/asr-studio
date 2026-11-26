"""Real-browser regressions for transcript completion and rerun feedback."""

from __future__ import annotations

import shutil
from uuid import uuid4

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from seeding import prepare_recording, seed_meeting
from test_browser_asr_model import _choose_default
from test_browser_history import (
    READING_SEGMENTS,
    READING_SPEAKERS,
    _prepare_browser_asr_model,
    _publish_browser_generation,
)
from test_browser_history import live as live
from test_browser_history import ready_rerun_worker as ready_rerun_worker
from test_browser_history import signed_in as signed_in
from test_browser_notes import _write_test_recording

from asr_studio.tasks import claim_next_task, publish_complete_generation, update_task_progress

pytestmark = pytest.mark.browser


def test_completion_status_stays_in_the_accessibility_tree_when_search_keeps_focus(
    live, signed_in, ready_rerun_worker, tmp_path
):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting = seed_meeting(
        live.settings.data_dir, "Completion feedback", "2026-10-14", duration_seconds=30
    )
    _publish_browser_generation(
        live,
        meeting,
        segments=READING_SEGMENTS,
        speakers=READING_SPEAKERS,
    )
    _write_test_recording(live.settings.data_dir, meeting, tmp_path)
    page.goto(f"{live.url}/meetings/{meeting}#transcript")
    page.evaluate("document.fonts.ready")
    first_row = page.locator(".transcript-segment").first.bounding_box()
    assert first_row and first_row["y"] + first_row["height"] <= 900

    page.locator("#start-transcription").click()
    submit = page.locator("#transcription-rerun-submit")
    expect(submit).to_be_enabled()
    with page.expect_response(
        lambda response: response.url.endswith("/api/tasks") and response.request.method == "POST"
    ) as accepted:
        submit.press("Enter")
    assert accepted.value.status == 202
    task = accepted.value.json()
    search = page.locator("#transcript-search")
    search.focus()
    expect(search).to_be_focused()

    owner = "browser-completion-feedback"
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

    details = page.locator("#transcript-processing")
    expect(details).not_to_have_attribute("open", "")
    expect(search).to_be_focused()
    success = "Transcript đã được lưu hoàn chỉnh."
    status = page.get_by_role("status").filter(has_text=success)
    expect(status).to_have_count(1)
    assert success in page.locator("#meeting-panel-transcript").aria_snapshot()

    page.evaluate(
        """() => {
            const status = document.querySelector('#transcription-status');
            window.__statusMutationCount = 0;
            new MutationObserver(mutations => {
                window.__statusMutationCount += mutations.length;
            }).observe(status, {childList: true, characterData: true, subtree: true});
        }"""
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    page.evaluate("() => new Promise(resolve => queueMicrotask(resolve))")
    assert page.evaluate("() => window.__statusMutationCount") == 0


def test_unready_saved_default_keeps_rerun_disabled_with_truthful_server_guidance(
    live, signed_in, ready_rerun_worker
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    pho = _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large")
    object.__setattr__(live.settings, "asr_phowhisper_model_path", pho)
    _choose_default(page, live, "pho-whisper-large")
    meeting_id, _generation_id, _segments = seed_completed_transcript(data_dir)
    prepare_recording(data_dir, meeting_id)
    shutil.rmtree(pho)

    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#generation-title")).to_be_visible()
    capability = page.request.get(f"{live.url}/api/asr-models").json()
    automatic = next(row for row in capability["asr_models"] if row["id"] == "auto")
    assert automatic["chosen"] is True
    assert automatic["ready"] is False
    assert automatic["reason"] == "not_prepared"

    details = page.locator("#transcript-processing")
    expect(details).not_to_have_attribute("open", "")
    start = page.get_by_role("button", name="Chạy lại", exact=True)
    expect(start).to_be_disabled()
    reason = (
        "Model này chưa được chuẩn bị trên máy. Chạy lệnh sau trong thư mục ứng dụng "
        "(chỉ tải model, không gửi bản ghi), rồi tải lại trang."
    )
    status = page.get_by_role("status").filter(has_text=reason)
    expect(status).to_have_count(1)
    expect(status).to_be_visible()
    expect(status).to_contain_text(reason)
    first_row = page.locator(".transcript-segment").first.bounding_box()
    status_box = status.bounding_box()
    assert first_row and status_box
    assert status_box["y"] + status_box["height"] <= first_row["y"]
    assert reason in page.locator("#meeting-panel-transcript").aria_snapshot()
    expect(details.locator(":scope > summary")).to_have_accessible_description(reason)

    details.locator(":scope > summary").click()
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    expect(selector).to_be_visible()
    expect(selector).to_have_value("auto")
    expect(selector).to_be_enabled()
    expect(page.locator("#transcription-model-guidance")).to_be_visible()
    expect(page.locator("#transcription-model-guidance")).to_contain_text(reason)


def test_active_rerun_feedback_keeps_cancel_before_a_long_reading_list(
    live, signed_in, ready_rerun_worker, tmp_path
):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting = seed_meeting(
        live.settings.data_dir, "Long rerun feedback", "2026-10-14", duration_seconds=30
    )
    segments = [
        dict(
            READING_SEGMENTS[0],
            id=uuid4().hex,
            start_seconds=index,
            end_seconds=index + 0.8,
            text=f"Saved transcript segment {index + 1}.",
        )
        for index in range(30)
    ]
    _publish_browser_generation(live, meeting, segments=segments, speakers=READING_SPEAKERS)
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
    status = page.locator("#transcription-status")
    cancel = page.locator("#cancel-transcription")
    # The 202 response acknowledges the server write before the client has
    # necessarily consumed its body and rendered the active task controls.
    expect(cancel).to_be_visible()
    expect(cancel).to_be_enabled()
    expect(status).to_be_focused()
    status_box = status.bounding_box()
    first_row = page.locator(".transcript-segment").first.bounding_box()
    assert status_box and first_row
    assert status_box["y"] + status_box["height"] <= first_row["y"]
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    expect(cancel).to_be_in_viewport()
