"""The meeting page is two WAI-ARIA tabs, Meeting notes and Transcript & bản ghi (real browser).

Each test names the acceptance criterion of issue #46 it proves. Data is seeded straight into the
temporary database, so the page reads it through the real endpoints.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_history import FIXED_NOW, open_calendar
from test_browser_history import live as live
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes import _seed_saved_notes, _task_source_id, _write_test_recording
from test_browser_notes import fake_notes_provider as fake_notes_provider
from test_browser_notes import signed_in as signed_in
from test_browser_notes_provider import accept
from test_browser_task_progress import end_notes_run, running_notes, running_transcription

from asr_studio.notes import get_notes_source
from asr_studio.storage import database_path

pytestmark = pytest.mark.browser

TABS = ("notes", "transcript")
NOTES_TAB = "#meeting-tab-notes"
TRANSCRIPT_TAB = "#meeting-tab-transcript"
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}
NARROW = {"width": 360, "height": 740}
MINIMUM = {"width": 320, "height": 640}
OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"


def seed_review_transcript(data_dir, *, title="Review counts"):
    """A completed transcript of three segments, two of which show the "Cần kiểm tra" cue.

    One is flagged uncertain with no reasons, one carries a review reason only, one is plain.
    """
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir, title=title)
    third = uuid.uuid4().hex
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE transcript_segments SET uncertain = 1 WHERE id = ?", (segment_ids[0],)
        )
        connection.execute(
            "UPDATE transcript_segments SET review_reasons = ? WHERE id = ?",
            (json.dumps(["overlap"]), segment_ids[1]),
        )
        connection.execute(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, 2, 8.0, 9.0, 'A plain closing line.', "
            "'SPEAKER_00', 'SPEAKER_00', -0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
            (third, generation_id),
        )
        connection.execute(
            "UPDATE transcript_generations SET segment_count = 3 WHERE id = ?", (generation_id,)
        )
    return meeting_id


def seed_failed_notes(live):
    """A meeting with a transcript whose only notes run failed: no saved notes, none running."""
    meeting_id, task_id, owner_id = running_notes(live)
    end_notes_run(live, task_id, owner_id, "failed")
    return meeting_id, task_id


def seed_accepted_notes(live):
    """A transcript with a notes task a worker has accepted but not yet claimed."""
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    accept(live.settings.data_dir, meeting_id, generation_id, request_id="tabs-accepted")
    return meeting_id


SCENARIOS = {
    "no-transcript": (lambda live: seed_meeting(live.settings.data_dir, "Fresh"), "transcript"),
    "transcribing": (lambda live: running_transcription(live)[0], "transcript"),
    "transcript-only": (
        lambda live: seed_completed_transcript(live.settings.data_dir)[0],
        "transcript",
    ),
    "notes-failed": (lambda live: seed_failed_notes(live)[0], "transcript"),
    "notes-saved": (lambda live: _seed_saved_notes(live.settings.data_dir)[0], "notes"),
    "notes-running": (lambda live: running_notes(live)[0], "notes"),
    "notes-accepted": (seed_accepted_notes, "notes"),
}


def open_meeting(page, live, meeting_id, suffix=""):
    page.goto(f"{live.url}/meetings/{meeting_id}{suffix}")
    expect(page.locator('#meeting-tabs [role="tab"][aria-selected="true"]')).to_have_count(
        1, timeout=10_000
    )


def expect_tab(page, name):
    """The named tab is the one selected, and only its panel shows."""
    other = "transcript" if name == "notes" else "notes"
    expect(page.locator(f"#meeting-tab-{name}")).to_have_attribute("aria-selected", "true")
    expect(page.locator(f"#meeting-tab-{other}")).to_have_attribute("aria-selected", "false")
    expect(page.locator(f"#meeting-panel-{name}")).to_be_visible()
    expect(page.locator(f"#meeting-panel-{other}")).to_be_hidden()


def wait_for(page, condition, timeout=10.0):
    """Poll a Python condition while the page's events (routes, requests) keep being served."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        page.wait_for_timeout(25)


def fragment(page):
    return urlsplit(page.url).fragment


def focused_id(page):
    return page.evaluate("document.activeElement.id")


def rgb(token):
    return "rgb({}, {}, {})".format(*(int(token[i : i + 2], 16) for i in (1, 3, 5)))


# ---- AC 1: WAI-ARIA tablist under the header, arrows / Home / End, one tab stop ----


def test_the_meeting_page_has_a_wai_aria_tablist_under_the_header(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)

    tablist = page.get_by_role("tablist")
    expect(tablist).to_have_count(1)
    tabs = tablist.get_by_role("tab")
    expect(tabs).to_have_count(2)
    expect(tabs.nth(0)).to_have_text("Meeting notes")
    expect(tabs.nth(1)).to_contain_text("Transcript & bản ghi")
    expect(page.get_by_role("tab", name="Meeting notes", exact=True)).to_have_count(1)
    expect(page.get_by_role("tab", name=re.compile(r"^Transcript & bản ghi"))).to_have_count(1)

    for name in TABS:
        tab = page.locator(f"#meeting-tab-{name}")
        panel = page.locator(f"#meeting-panel-{name}")
        assert tab.get_attribute("role") == "tab"
        assert tab.get_attribute("aria-controls") == f"meeting-panel-{name}"
        assert panel.get_attribute("role") == "tabpanel"
        assert panel.get_attribute("aria-labelledby") == f"meeting-tab-{name}"
    assert page.locator('#meeting-tabs [role="tab"][aria-selected="true"]').count() == 1
    expect_tab(page, "transcript")
    # The visible panel is named by its tab, as assistive technology reads it.
    expect(page.get_by_role("tabpanel", name=re.compile("^Transcript & bản ghi"))).to_be_visible()

    # The tablist sits under the header (title, metadata, status strip) and above both panels.
    order = page.evaluate(
        """() => {
          const top = selector => document.querySelector(selector).getBoundingClientRect().top;
          return [
            top("#meeting-title"), top("#meeting-status-strip"), top("#meeting-tabs"),
            top("#meeting-panel-transcript"),
          ];
        }"""
    )
    assert order == sorted(order) and len(set(order)) == 4, order


def test_only_the_selected_tab_is_in_the_tab_order(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)

    for selected, other in (("transcript", "notes"), ("notes", "transcript")):
        expect_tab(page, selected)
        assert page.locator(f"#meeting-tab-{selected}").get_attribute("tabindex") == "0"
        assert page.locator(f"#meeting-tab-{other}").get_attribute("tabindex") == "-1"
        page.locator("#meeting-actions").focus()
        page.keyboard.press("Tab")  # the closed actions menu holds no tab stops
        assert focused_id(page) == f"meeting-tab-{selected}"
        page.keyboard.press("Tab")  # the next stop is inside the panel, never the other tab
        assert focused_id(page) != f"meeting-tab-{other}"
        assert (
            page.evaluate("document.activeElement.closest('[role=tabpanel]')?.id")
            == f"meeting-panel-{selected}"
        )
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Shift+Tab")
        assert focused_id(page) == "meeting-actions"
        page.locator(f"#meeting-tab-{other}").click()


def test_arrow_home_and_end_keys_change_the_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    page.locator(TRANSCRIPT_TAB).focus()

    for key, expected in (
        ("ArrowRight", "notes"),  # past the last tab wraps to the first
        ("ArrowRight", "transcript"),
        ("ArrowLeft", "notes"),
        ("ArrowLeft", "transcript"),  # before the first tab wraps to the last
        ("Home", "notes"),
        ("End", "transcript"),
    ):
        page.keyboard.press(key)
        expect_tab(page, expected)
        assert focused_id(page) == f"meeting-tab-{expected}", key  # focus follows the selection
        assert fragment(page) == expected, key


def test_other_keys_and_modified_arrows_leave_the_tab_alone(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    page.locator(TRANSCRIPT_TAB).focus()
    for key in ("ArrowDown", "ArrowUp", "a", "Alt+ArrowLeft", "Control+ArrowRight", "Shift+Home"):
        page.keyboard.press(key)
    expect_tab(page, "transcript")
    assert fragment(page) == ""


# ---- AC 2: the Transcript tab holds the recording, player, transcription, speakers, segments ----


def test_transcript_tab_holds_the_recording_block_and_the_transcript_with_counts(live, signed_in):
    _context, page = signed_in()
    meeting_id = seed_review_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")

    panel = page.locator("#meeting-panel-transcript")
    for selector in (
        "#audio-heading",
        ".player-bar #meeting-audio",
        "#transcription-panel",
        "#start-transcription",
        "#transcript-generation",
        "#speaker-catalog",
        "#speaker-catalog-list .speaker-chip",
        "#transcript-segments > li",
    ):
        expect(panel.locator(selector).first).to_be_visible()
    expect(panel.locator("#transcript-segments > li")).to_have_count(3)
    expect(panel.locator("#notes-panel")).to_have_count(0)

    # The label counts the segments and those that show the "Cần kiểm tra" cue. A literal space sets
    # the counts off from the name, so the text and the accessible name read the same in any engine.
    label = page.locator(TRANSCRIPT_TAB)
    expect(label).to_have_text("Transcript & bản ghi 3 đoạn · 2 cần kiểm tra")
    expect(label).to_have_accessible_name("Transcript & bản ghi 3 đoạn · 2 cần kiểm tra")
    # Both kinds of cue count: one with a reason, and an uncertain one that only says to listen.
    cues = panel.locator(".segment-review-cue")
    expect(cues).to_have_count(2)
    expect(cues.filter(has_text="Cần kiểm tra · Có lời nói chồng lấn")).to_have_count(1)
    expect(cues.filter(has_text="Nên nghe lại")).to_have_count(1)


def test_the_transcript_label_has_no_counts_until_there_is_a_transcript(live, signed_in):
    _context, page = signed_in()
    meeting_id = seed_meeting(live.settings.data_dir, "No transcript yet")
    open_meeting(page, live, meeting_id)
    expect(page.locator(TRANSCRIPT_TAB)).to_have_text("Transcript & bản ghi")
    expect(page.locator("#meeting-tab-count")).to_have_text("")


def test_the_counts_follow_the_transcript_that_is_shown(live, signed_in):
    """Switching to a past transcript shows its own counts, the same ones its rows show."""
    _context, page = signed_in()
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    # A newer complete generation with one segment, so the first one becomes history.
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        new_task = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, transcript_end_seconds, segment_count) "
            "VALUES (?, ?, ?, ?, 'transcription', '/models/whisper-small', 'whisper-small', "
            "'cpu', 'int8', 'en', 'succeeded', 'complete', 'transcript_only', 30.0, 5.0, 1)",
            (new_task, f"seed-{new_task}", f"hash-{new_task}", meeting_id),
        )
        newer = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, language_probability, uncertainty_policy, "
            "segment_count, diarization_model_name, regular_turn_count, exclusive_turn_count, "
            "alignment_policy) VALUES (?, ?, ?, 2, 'complete', 'en', 'whisper-small', 0.9, "
            "'test-policy', 1, 'community-1', 1, 0, 'test-alignment')",
            (newer, meeting_id, new_task),
        )
        connection.execute(
            "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
            "first_seen_seconds, last_seen_seconds) VALUES (?, 'SPEAKER_00', 'Morgan', 1.0, 5.0)",
            (newer,),
        )
        connection.execute(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, 0, 1.0, 2.0, 'Only line.', 'SPEAKER_00', "
            "'SPEAKER_00', -0.2, 0.05, 1.0, 1, '[]', 0, 1.0, 1)",
            (uuid.uuid4().hex, newer),
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = ? WHERE id = ?", (newer, meeting_id)
        )
    open_meeting(page, live, meeting_id)
    expect(page.locator(TRANSCRIPT_TAB)).to_contain_text("1 đoạn · 1 cần kiểm tra")
    page.locator("#generation-selector").select_option(generation_id)
    expect(page.locator("#transcript-segments > li")).to_have_count(2)
    expect(page.locator(TRANSCRIPT_TAB)).to_contain_text("2 đoạn · 0 cần kiểm tra")


# ---- AC 3: the Notes tab holds all of the notes ----


def test_notes_tab_holds_the_whole_notes_part(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "notes")

    panel = page.locator("#meeting-panel-notes")
    in_panel = (
        "#notes-panel",
        "#notes-change",
        "#notes-output-language",
        "#notes-provider",
        "#notes-model",
        "#notes-start",
        "#notes-cancel",
        "#notes-status",
        "#notes-task-details",
        "#notes-steps",
        "#notes-error",
        "#notes-saved-work",
        "#notes-revision-selector",
        "#notes-mode-view",
        "#notes-mode-write",
        "#notes-save",
        "#notes-copy",
        "#notes-export",
    )
    for selector in in_panel:
        assert panel.locator(selector).count() == 1, selector
        assert page.locator(f"#meeting-panel-transcript {selector}").count() == 0, selector
    # The saved source is not part of the notes: its card sits in the Transcript tab.
    assert panel.locator("#notes-source-view").count() == 0
    assert page.locator("#meeting-panel-transcript > #notes-source-view").count() == 1
    # The three selects sit behind "Đổi…".
    expect(panel.locator("#notes-change")).to_be_visible()
    panel.locator("#notes-change").click()
    for selector in (
        "#notes-output-language",
        "#notes-provider",
        "#notes-model",
        "#notes-start",
        "#notes-revision-selector",
        "#notes-mode-view",
        "#notes-mode-write",
        "#notes-save",
        "#notes-copy",
        "#notes-export",
        "#notes-preview",
    ):
        expect(panel.locator(selector)).to_be_visible()
    # Nothing of the transcript or the recording is in this tab.
    for selector in (
        "#transcription-panel",
        ".player-bar",
        "#meeting-audio",
        "#transcript-segments",
    ):
        assert panel.locator(selector).count() == 0, selector
        expect(page.locator(selector)).to_be_hidden()


def test_notes_progress_and_errors_show_in_the_notes_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_notes(live)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "notes")
    expect(page.locator("#notes-steps")).to_be_visible()
    expect(page.locator("#notes-cancel")).to_be_visible()
    end_notes_run(live, task_id, owner_id, "failed")
    # A failed run reads in the status line of the tab that is open.
    expect(page.locator("#notes-status")).to_contain_text("tạm thời không khả dụng", timeout=10_000)
    expect(page.locator("#notes-status")).to_be_visible()


# ---- AC 4: no hash, the tab follows the meeting's state ----


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_default_tab_follows_the_state_of_the_meeting(live, signed_in, scenario):
    seed, expected = SCENARIOS[scenario]
    _context, page = signed_in()
    meeting_id = seed(live)
    open_meeting(page, live, meeting_id)
    expect_tab(page, expected)
    # Choosing the default does not rewrite the address.
    assert page.url == f"{live.url}/meetings/{meeting_id}"


def test_the_default_tab_waits_for_the_notes_answer_and_never_shows_the_wrong_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    held = []
    page.route(f"{live.url}/api/meetings/{meeting_id}/notes", lambda route: held.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_be_visible()
    expect(page.locator("#meeting-tabs")).to_be_visible()
    wait_for(page, lambda: held)
    # Tabs are there, none chosen, nothing painted: no flash of either panel. The transcript panel
    # is in place but transparent, so its controls work already if it turns out to be the one.
    expect(page.locator('#meeting-tabs [role="tab"][aria-selected="true"]')).to_have_count(0)
    expect(page.locator("#meeting-panel-notes")).to_be_hidden()
    assert (
        page.locator("#meeting-panel-transcript").evaluate(
            "element => getComputedStyle(element).opacity"
        )
        == "0"
    )
    # Before a tab is chosen, the first tab is still reachable with Tab.
    assert page.locator(NOTES_TAB).get_attribute("tabindex") == "0"
    assert page.locator(TRANSCRIPT_TAB).get_attribute("tabindex") == "-1"
    for route in held:
        route.continue_()
    expect_tab(page, "notes")


def test_the_transcript_controls_work_while_the_default_waits(live, signed_in):
    """Nothing is hidden from the keyboard or the pointer while the notes read is out."""
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    held = []
    page.route(f"{live.url}/api/meetings/{meeting_id}/notes", lambda route: held.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}")
    wait_for(page, lambda: held)
    expect(page.locator('#meeting-tabs [role="tab"][aria-selected="true"]')).to_have_count(0)
    edit = page.locator(".segment-edit-button").first
    expect(edit).to_be_enabled()
    edit.focus()
    expect(edit).to_be_focused()
    for route in held:
        route.continue_()
    expect_tab(page, "transcript")
    expect(edit).to_be_focused()  # the answer changed nothing visible, so it moved nothing


def test_a_failed_notes_read_falls_back_to_the_transcript_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes",
        lambda route: route.fulfill(status=503, json={"detail": "Seeded failure"}),
    )
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")


def test_without_a_notes_answer_the_page_falls_back_to_the_transcript_after_a_while(
    live, signed_in
):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    start = datetime.fromisoformat(FIXED_NOW)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(minutes=1))  # from here only run_for moves time
    held = []
    page.route(f"{live.url}/api/meetings/{meeting_id}/notes", lambda route: held.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-tabs")).to_be_visible()
    wait_for(page, lambda: held)
    expect(page.locator('#meeting-tabs [role="tab"][aria-selected="true"]')).to_have_count(0)
    page.clock.run_for(3_000)
    expect(page.locator('#meeting-tabs [role="tab"][aria-selected="true"]')).to_have_count(0)
    page.clock.run_for(1_500)  # 4 s without an answer: the transcript is the safe place to be
    expect_tab(page, "transcript")
    assert (
        page.locator("#meeting-panel-transcript").evaluate(
            "element => getComputedStyle(element).opacity"
        )
        == "1"
    )


# ---- AC 5: #notes / #transcript, pushState, Back, reload, routes ----


def test_a_hash_selects_its_tab_over_the_default(live, signed_in):
    _context, page = signed_in()
    saved_id, *_ = _seed_saved_notes(live.settings.data_dir)
    plain_id, *_ = seed_completed_transcript(live.settings.data_dir, title="No notes yet")

    open_meeting(page, live, saved_id, "#transcript")
    expect_tab(page, "transcript")
    open_meeting(page, live, saved_id, "#notes")
    expect_tab(page, "notes")
    open_meeting(page, live, plain_id, "#notes")
    expect_tab(page, "notes")
    expect(page.locator("#notes-start")).to_be_visible()
    open_meeting(page, live, plain_id, "#transcript")
    expect_tab(page, "transcript")
    # A hash that names no tab changes nothing.
    open_meeting(page, live, saved_id, "#elsewhere")
    expect_tab(page, "notes")


def test_changing_tab_pushes_a_hash_entry_and_back_and_forward_restore_it(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    entries = page.evaluate("history.length")

    page.locator(NOTES_TAB).click()
    expect_tab(page, "notes")
    assert fragment(page) == "notes"
    assert page.evaluate("history.length") == entries + 1  # pushState, not replaceState
    page.locator(TRANSCRIPT_TAB).click()
    expect_tab(page, "transcript")
    assert fragment(page) == "transcript"
    assert page.evaluate("history.length") == entries + 2
    # Clicking the tab that is already selected adds nothing.
    page.locator(TRANSCRIPT_TAB).click()
    assert page.evaluate("history.length") == entries + 2

    page.go_back()
    expect_tab(page, "notes")
    assert fragment(page) == "notes"
    page.go_back()
    expect_tab(page, "transcript")
    assert page.url == f"{live.url}/meetings/{meeting_id}"
    page.go_forward()
    expect_tab(page, "notes")
    page.go_forward()
    expect_tab(page, "transcript")


def test_reload_keeps_the_chosen_tab_and_the_route_still_serves_the_page(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "notes")
    page.locator(TRANSCRIPT_TAB).click()
    expect_tab(page, "transcript")
    page.reload()
    expect_tab(page, "transcript")
    assert fragment(page) == "transcript"
    page.locator(NOTES_TAB).click()
    page.reload()
    expect_tab(page, "notes")

    # The routes are unchanged: the meeting path and a saved-source link still serve the page.
    for path in (
        f"/meetings/{meeting_id}",
        f"/meetings/{meeting_id}?notes_source=a&segment=b",
    ):
        response = page.context.request.get(f"{live.url}{path}")
        assert response.status == 200, path
        assert 'id="meeting-tabs"' in response.text(), path


def test_back_returns_to_the_tab_that_was_shown_even_after_the_default_changed(
    live, signed_in, fake_notes_provider
):
    """Opened on the transcript (no notes), then notes start: the default would now be Notes.

    The entry Back returns to remembers the tab it showed, whatever the default says by then.
    """
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    page.locator(NOTES_TAB).click()
    expect(page.locator("#notes-start")).to_be_enabled()
    page.locator("#notes-start").click()
    expect(page.locator("#notes-cancel")).to_be_visible(timeout=10_000)
    page.go_back()
    expect_tab(page, "transcript")
    page.go_forward()
    expect_tab(page, "notes")


def test_a_hash_typed_in_the_address_bar_changes_the_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    page.evaluate("location.hash = '#notes'")
    expect_tab(page, "notes")
    page.evaluate("location.hash = '#transcript'")
    expect_tab(page, "transcript")


def test_the_tab_survives_a_trip_to_the_calendar_and_back(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    page.locator(TRANSCRIPT_TAB).click()
    expect_tab(page, "transcript")
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    page.locator("#calendar-back").click()
    expect_tab(page, "transcript")
    assert page.url == f"{live.url}/meetings/{meeting_id}#transcript"


def test_a_meeting_opened_from_the_sidebar_picks_its_own_default(live, signed_in):
    _context, page = signed_in()
    with_notes, *_ = _seed_saved_notes(live.settings.data_dir, title="Has notes")
    seed_completed_transcript(live.settings.data_dir, title="Has none")
    open_meeting(page, live, with_notes)
    expect_tab(page, "notes")
    page.locator("#recent-list a").filter(has_text="Has none").click()
    expect(page.locator("#meeting-title")).to_have_text("Has none")
    expect_tab(page, "transcript")
    page.locator("#recent-list a").filter(has_text="Has notes").click()
    expect(page.locator("#meeting-title")).to_have_text("Has notes")
    expect_tab(page, "notes")


def failed_notes_source(live):
    """A saved notes source (snapshot) of a meeting that has no saved notes and none running."""
    meeting_id, task_id = seed_failed_notes(live)
    snapshot = get_notes_source(
        live.settings.data_dir, _task_source_id(live.settings.data_dir, task_id)
    )
    return (
        meeting_id,
        snapshot["id"],
        snapshot["segments"][0]["id"],
        snapshot["segments"][0]["text"],
    )


def test_a_saved_source_link_opens_the_transcript_tab_unless_a_hash_says_otherwise(live, signed_in):
    _context, page = signed_in()
    meeting_id, _revision, snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    link = f"?notes_source={snapshot['id']}&segment={segment_ids[0]}"
    text = snapshot["segments"][0]["text"]

    # With no hash the default would be notes (it has saved notes); the link wins: the transcript,
    # where the card sits under the player.
    open_meeting(page, live, meeting_id, link)
    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-source-text")).to_have_text(text)
    assert parse_qs(urlsplit(page.url).query) == {
        "notes_source": [snapshot["id"]],
        "segment": [segment_ids[0]],
    }

    # A hash wins over the link: the source stays loaded in the transcript tab, which is not shown.
    open_meeting(page, live, meeting_id, f"{link}#notes")
    expect_tab(page, "notes")
    expect(page.locator("#notes-source-view")).to_be_hidden()
    expect(page.locator("#notes-source-text")).to_have_text(text, timeout=10_000)
    page.locator(TRANSCRIPT_TAB).click()
    expect(page.locator("#notes-source-view")).to_be_visible()
    # The query stays when the tab changes.
    assert parse_qs(urlsplit(page.url).query) == {
        "notes_source": [snapshot["id"]],
        "segment": [segment_ids[0]],
    }
    assert fragment(page) == "transcript"


def test_closing_the_source_view_keeps_the_tab_in_the_address(live, signed_in):
    _context, page = signed_in()
    meeting_id, snapshot_id, segment_id, _text = failed_notes_source(live)
    link = f"?notes_source={snapshot_id}&segment={segment_id}#transcript"
    open_meeting(page, live, meeting_id, link)
    expect(page.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    page.locator("#notes-source-close").click()
    expect(page.locator("#notes-source-view")).to_be_hidden()
    assert page.url == f"{live.url}/meetings/{meeting_id}#transcript"
    page.reload()
    expect_tab(page, "transcript")


def test_the_seek_button_moves_the_player_that_sits_in_the_same_tab(live, signed_in, tmp_path):
    _context, page = signed_in()
    meeting_id, revision, _snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    link = f"?notes_source={revision['source_snapshot_id']}&segment={segment_ids[0]}"
    open_meeting(page, live, meeting_id, link)
    expect_tab(page, "transcript")  # the card and the player it moves are in the same tab
    expect(page.locator("#notes-source-status")).to_contain_text("Đã tua bản ghi", timeout=10_000)
    audio = page.locator("#meeting-audio")
    expect(audio).to_be_visible()
    page.evaluate("document.querySelector('#meeting-audio').pause()")
    page.evaluate("document.querySelector('#meeting-audio').currentTime = 20")
    address, entries = page.url, page.evaluate("history.length")

    page.locator("#notes-source-seek").click()
    wait_for(
        page,
        lambda: page.evaluate(
            "Math.abs(document.querySelector('#meeting-audio').currentTime - 1) < 0.1"
        ),
    )
    expect_tab(page, "transcript")  # nothing to switch to
    assert focused_id(page) == "notes-source-seek"
    assert (page.url, page.evaluate("history.length")) == (address, entries)  # no new entry
    assert audio.evaluate("element => element.paused")  # the button seeks; a chip click also plays


# ---- AC 6: no transcript yet, the Notes tab says so and offers the way to the transcript ----


def test_notes_tab_without_a_transcript_says_so_and_offers_the_transcript_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id = seed_meeting(live.settings.data_dir, "No transcript", "2026-10-19")
    open_meeting(page, live, meeting_id, "#notes")
    expect_tab(page, "notes")

    empty = page.locator("#notes-empty")
    expect(empty).to_be_visible()
    expect(empty.get_by_role("heading", name="Cần transcript trước")).to_be_visible()
    # No picker and no disabled create button: the one control is the way to the transcript.
    panel = page.locator("#meeting-panel-notes")
    expect(panel.locator("select:visible")).to_have_count(0)
    expect(panel.locator("button:visible")).to_have_count(1)
    for selector in (
        "#notes-start",
        "#notes-change",
        "#notes-output-language",
        "#notes-provider",
        "#notes-model",
    ):
        expect(page.locator(selector)).to_be_hidden()
    expect(panel.get_by_role("button", name=re.compile("Transcript & bản ghi"))).to_be_visible()

    panel.get_by_role("button", name=re.compile("Transcript & bản ghi")).click()
    expect_tab(page, "transcript")
    assert fragment(page) == "transcript"
    assert focused_id(page) == "meeting-tab-transcript"
    expect(page.locator("#start-transcription")).to_be_visible()
    page.go_back()
    expect_tab(page, "notes")


def test_the_empty_state_says_when_the_transcript_is_being_made(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = running_transcription(live)
    open_meeting(page, live, meeting_id, "#notes")
    expect(page.locator("#notes-empty")).to_be_visible()
    expect(page.locator("#notes-empty-copy")).to_contain_text("Đang chép lời")
    expect(page.locator("#notes-start")).to_be_hidden()
    expect(page.locator("#notes-empty-open-transcript")).to_be_visible()


def test_the_empty_state_gives_way_to_the_controls_once_there_is_a_transcript(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id, "#notes")
    expect(page.locator("#notes-empty")).to_be_hidden()
    for selector in ("#notes-start", "#notes-change"):
        expect(page.locator(selector)).to_be_visible()
    page.locator("#notes-change").click()
    for selector in ("#notes-output-language", "#notes-provider", "#notes-model"):
        expect(page.locator(selector)).to_be_visible()
    expect(page.locator("#notes-start")).to_be_enabled()


# ---- AC 7: starting notes from anywhere shows the Notes tab ----


def test_starting_notes_from_the_transcript_tab_selects_the_notes_tab(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    expect(page.locator("#notes-start")).to_be_enabled()
    # Nothing in the transcript tab starts notes today, so press the start control from where
    # a future one would be: the tab must still follow the run.
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted:
        page.evaluate("document.querySelector('#notes-start').click()")
    assert accepted.value.status == 202
    expect_tab(page, "notes")
    assert fragment(page) == "notes"
    expect(page.locator("#notes-steps")).to_be_visible()
    expect(page.locator("#notes-cancel")).to_be_visible()
    page.go_back()
    expect_tab(page, "transcript")


def test_a_refused_start_does_not_move_the_tab(live, signed_in):
    _context, page = signed_in()
    meeting_id = seed_meeting(live.settings.data_dir, "Nothing to start")
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    page.evaluate("document.querySelector('#notes-start').click()")  # no transcript: refused
    expect_tab(page, "transcript")


# ---- AC 8: the name once, and the status strip ----


def test_the_meeting_name_shows_once_on_the_meeting_page(live, signed_in):
    _context, page = signed_in()
    title = "Quarterly planning"
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir, title=title)
    open_meeting(page, live, meeting_id)

    def shown(name):
        return page.evaluate(
            """name => [...document.querySelectorAll("#workspace *")].filter(element =>
              element.getClientRects().length > 0
              && [...element.childNodes].some(node =>
                node.nodeType === Node.TEXT_NODE && node.textContent.includes(name))).length""",
            name,
        )

    for tab in TABS:
        page.locator(f"#meeting-tab-{tab}").click()
        assert shown(title) == 1
    expect(page.locator("#meeting-title")).to_have_text(title)
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")
    assert page.title() == f"{title} · ASR Studio"
    # The one place is the focus target of the page.
    assert page.locator("#meeting-title").get_attribute("tabindex") == "-1"

    page.locator("#meeting-actions").click()

    page.locator("#edit-meeting").click()
    page.locator("#edit-title").fill("Renamed planning")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#meeting-title")).to_have_text("Renamed planning")
    expect(page.locator("#page-title")).to_have_text("Cuộc họp")
    assert shown("Renamed planning") == 1
    assert page.title() == "Renamed planning · ASR Studio"


@pytest.mark.parametrize(
    ("scenario", "recording", "transcript", "notes"),
    [
        (
            "no-transcript",
            "Bản ghi · Đã lưu · 1 giây",
            "Transcript · Chưa chép lời",
            "Notes · Chưa có",
        ),
        (
            "transcribing",
            "Bản ghi · Đã lưu · 2 phút",
            "Transcript · Đang chép lời",
            "Notes · Chưa có",
        ),
        (
            "transcript-only",
            "Bản ghi · Đã lưu · 30 giây",
            "Transcript · Hoàn tất",
            "Notes · Chưa có",
        ),
        ("notes-saved", "Bản ghi · Đã lưu · 30 giây", "Transcript · Hoàn tất", "Notes · 1 bản"),
        (
            "notes-running",
            "Bản ghi · Đã lưu · 30 giây",
            "Transcript · Hoàn tất",
            "Notes · Đang tạo",
        ),
        ("notes-failed", "Bản ghi · Đã lưu · 30 giây", "Transcript · Hoàn tất", "Notes · Thất bại"),
    ],
)
def test_the_header_has_a_short_status_strip_of_recording_transcript_and_notes(
    live, signed_in, scenario, recording, transcript, notes
):
    _context, page = signed_in()
    meeting_id = SCENARIOS[scenario][0](live)
    open_meeting(page, live, meeting_id)
    strip = page.locator("#meeting-status-strip")
    expect(strip).to_be_visible()
    items = strip.locator("li")
    expect(items).to_have_count(3)
    # Recording, transcript, notes: in that order, each one short line, also while it loads.
    expect(items.nth(0)).to_have_text(recording)
    expect(items.nth(1)).to_have_text(transcript)
    expect(items.nth(2)).to_have_text(notes)
    assert page.evaluate(
        "[...document.querySelectorAll('#meeting-status-strip li')]"
        ".every(item => item.getBoundingClientRect().height < 60)"
    )


def test_the_status_strip_follows_a_run_that_ends(live, signed_in):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_notes(live)
    open_meeting(page, live, meeting_id)
    expect(page.locator("#status-notes")).to_have_text("Notes · Đang tạo")
    expect(page.locator("#status-notes")).to_have_attribute("data-state", "active")
    end_notes_run(live, task_id, owner_id, "failed")
    expect(page.locator("#status-notes")).to_have_text("Notes · Thất bại", timeout=10_000)
    expect(page.locator("#status-notes")).to_have_attribute("data-state", "problem")


# ---- AC 9: phone tablist stays under the header; provider launchers stay ----


@pytest.mark.parametrize("viewport", [PHONE, NARROW, MINIMUM], ids=["390", "360", "320"])
def test_on_a_phone_the_tablist_is_right_under_the_header_and_the_provider_launchers_stay(
    live, signed_in, viewport
):
    _context, page = signed_in(viewport=viewport)
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    geometry = page.evaluate(
        """() => {
          const box = selector => document.querySelector(selector).getBoundingClientRect();
          const tabs = document.querySelector("#meeting-tabs");
          return {
            before: tabs.previousElementSibling.id,
            gap: box("#meeting-tabs").top - box("#meeting-status-strip").bottom,
            panel: box("#meeting-panel-transcript").top - box("#meeting-tabs").bottom,
            title: box("#meeting-title").top,
            launchers: box(".connection-launchers").bottom,
          };
        }"""
    )
    assert geometry["before"] == "meeting-status-strip"
    assert 0 <= geometry["gap"] <= 32, geometry  # nothing sits between the header and the tabs
    assert 0 <= geometry["panel"] <= 32, geometry
    assert geometry["launchers"] <= geometry["title"], (
        geometry
    )  # the buttons stay above the meeting

    expect(page.locator(".workspace-header .connection-launchers")).to_have_count(0)
    launchers = page.locator(".sidebar-footer .connection-launcher")
    expect(launchers).to_have_count(2)
    expect(launchers.nth(0)).to_contain_text("OpenAI")
    expect(launchers.nth(1)).to_contain_text("Google")
    expect(page.locator("#launcher-chatgpt")).to_have_attribute(
        "aria-label", re.compile("^Kết nối OpenAI")
    )
    expect(page.locator("#launcher-antigravity")).to_have_attribute(
        "aria-label", re.compile("^Kết nối Google")
    )
    for selector in ("#launcher-chatgpt", "#launcher-antigravity"):
        expect(page.locator(selector)).to_be_visible()
        assert page.evaluate(OVERFLOW) <= 0
    page.locator("#launcher-chatgpt").click()
    expect(page.locator("#connections-dialog")).to_be_visible()


# ---- AC 11: 360 px, both themes, keyboard focus, reduced motion ----


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [NARROW, MINIMUM], ids=["360", "320"])
def test_neither_tab_overflows_the_page_sideways(live, signed_in, viewport, scheme):
    _context, page = signed_in(viewport=viewport, color_scheme=scheme)
    meeting_id = seed_review_transcript(
        live.settings.data_dir, title="A meeting with a rather long name to wrap on a narrow screen"
    )
    open_meeting(page, live, meeting_id)
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    for tab in ("transcript", "notes", "transcript"):
        page.locator(f"#meeting-tab-{tab}").click()
        expect_tab(page, tab)
        assert page.evaluate(OVERFLOW) <= 0, tab
        bar = page.locator("#meeting-tabs").bounding_box()
        assert bar is not None and bar["x"] >= 0 and bar["x"] + bar["width"] <= viewport["width"]
        for name in TABS:  # a label that wraps still fits inside its own tab
            box = page.locator(f"#meeting-tab-{name}").bounding_box()
            assert (
                box is not None and box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]
            )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_tab_focus_is_visible_and_the_selected_tab_is_marked_by_more_than_colour(
    live, signed_in, scheme
):
    _context, page = signed_in(color_scheme=scheme, reduced_motion="reduce")  # no colour mid-fade
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    tokens = page.evaluate(
        """() => Object.fromEntries(["--accent", "--ink-2"].map(name => [
          name, getComputedStyle(document.documentElement).getPropertyValue(name).trim()]))"""
    )
    accent, muted = rgb(tokens["--accent"]), rgb(tokens["--ink-2"])

    page.locator("#meeting-actions").focus()
    page.keyboard.press("Tab")  # keyboard focus, so :focus-visible applies
    assert focused_id(page) == "meeting-tab-transcript"
    ring = page.locator(TRANSCRIPT_TAB).evaluate(
        """element => {
          const style = getComputedStyle(element);
          return { style: style.outlineStyle, width: parseFloat(style.outlineWidth),
                   color: style.outlineColor };
        }"""
    )
    assert ring["style"] == "solid" and ring["width"] >= 2 and ring["color"] == accent, ring

    def look(name):
        return page.locator(f"#meeting-tab-{name}").evaluate(
            """element => {
              const style = getComputedStyle(element);
              return { color: style.color, underline: style.borderBottomColor,
                       underlineWidth: parseFloat(style.borderBottomWidth),
                       weight: Number(style.fontWeight) };
            }"""
        )

    # A style change settles on the next frame, so wait for the selection's colours to arrive.
    wait_for(page, lambda: look("transcript")["underline"] == accent)
    selected, other = look("transcript"), look("notes")
    assert selected["color"] == accent and selected["underline"] == accent
    assert selected["underlineWidth"] >= 2
    assert other["color"] == muted and other["underline"] == "rgba(0, 0, 0, 0)"
    assert selected["weight"] > other["weight"]  # not colour alone
    # Selecting the other tab moves the marks with it.
    page.keyboard.press("ArrowLeft")
    wait_for(page, lambda: look("notes")["underline"] == accent)
    assert look("transcript")["underline"] != accent


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_tab_labels_keep_readable_contrast(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme)
    meeting_id = seed_review_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    ratios = page.evaluate(
        r"""() => {
          const channels = value => value.match(/[\d.]+/g).map(Number);
          const linear = c => (c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
          const luminance = ([r, g, b]) =>
            0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
          const backdrop = element => {
            for (let node = element; node; node = node.parentElement) {
              const [r, g, b, a = 1] = channels(getComputedStyle(node).backgroundColor);
              if (a > 0) return [r, g, b];
            }
            return [255, 255, 255];
          };
          return ["#meeting-tab-notes", "#meeting-tab-transcript", "#meeting-tab-count",
                  ".meeting-status-item"].map(selector => {
            const element = document.querySelector(selector);
            const [high, low] = [luminance(channels(getComputedStyle(element).color)),
                                 luminance(backdrop(element))].sort((a, b) => b - a);
            return [selector, (high + 0.05) / (low + 0.05)];
          });
        }"""
    )
    for selector, ratio in ratios:
        assert ratio >= 4.5, (selector, ratio)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_in_forced_colors_only_the_selected_tab_draws_an_underline(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme)
    page.emulate_media(forced_colors="active")
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    expect_tab(page, "transcript")
    marks = page.evaluate(
        """() => {
          const list = document.querySelector("#meeting-tabs");
          const floor = list.getBoundingClientRect().bottom
            - parseFloat(getComputedStyle(list).borderBottomWidth);
          const look = name => {
            const element = document.querySelector(`#meeting-tab-${name}`);
            return { underline: getComputedStyle(element).borderBottomColor,
                     overlap: element.getBoundingClientRect().bottom - floor };
          };
          return { canvas: getComputedStyle(document.body).backgroundColor,
                   notes: look("notes"), transcript: look("transcript") };
        }"""
    )
    # The browser paints a transparent border CanvasText here, which would underline both tabs. The
    # unselected one hides its underline in the canvas colour and stays off the tablist hairline.
    assert marks["notes"]["underline"] == marks["canvas"], marks
    assert marks["transcript"]["underline"] != marks["canvas"], marks
    assert marks["notes"]["overlap"] <= 0.5 and marks["transcript"]["overlap"] <= 0.5, marks


def test_the_tab_transition_follows_reduced_motion(live, signed_in):
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    for preference in ("no-preference", "reduce"):
        _context, page = signed_in(reduced_motion=preference)
        open_meeting(page, live, meeting_id)
        duration = page.locator(TRANSCRIPT_TAB).evaluate(
            "element => parseFloat(getComputedStyle(element).transitionDuration)"
        )
        if preference == "reduce":
            assert duration <= 0.001, duration
        else:
            assert duration > 0.05, duration
        page.context.close()


def test_the_pinned_player_and_its_clearance_belong_to_the_transcript_tab(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    padding = "getComputedStyle(document.documentElement).scrollPaddingTop"
    expect_tab(page, "transcript")
    expect(page.locator(".player-bar")).to_be_visible()
    assert page.evaluate(padding) == "72px"
    assert page.locator(".player-bar").evaluate("e => getComputedStyle(e).position") == "sticky"
    page.locator(NOTES_TAB).click()
    expect_tab(page, "notes")
    expect(page.locator(".player-bar")).to_be_hidden()
    assert page.evaluate(padding) not in {
        "64px",
        "72px",
    }  # no player is pinned, so nothing is reserved
    open_calendar(page)
    expect(page.locator("#calendar-view")).to_be_visible()
    assert page.evaluate(padding) not in {"64px", "72px"}


def test_changing_tab_is_a_view_change_and_makes_no_request(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    open_meeting(page, live, meeting_id)
    page.wait_for_load_state("networkidle")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    for tab in ("transcript", "notes", "transcript"):
        page.locator(f"#meeting-tab-{tab}").click()
        expect_tab(page, tab)
    page.keyboard.press("ArrowLeft")
    expect_tab(page, "notes")
    assert requests == []
