"""One notes action that names the saved choice, and the speaker-name reminder (real browser).

Each test names the acceptance criterion of issue #47 it proves. Data is seeded straight into the
temporary database and the provider API is answered by routes, so no account or network is used.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from test_browser_history import assert_focus_ring, show_a_past_generation, speaker_chip
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_meeting_tabs import expect_tab, fragment, wait_for
from test_browser_notes import _seed_saved_notes
from test_browser_notes_provider import (
    ANTIGRAVITY_MODEL,
    ANTIGRAVITY_OTHER_MODEL,
    CHATGPT_MODEL,
    announce_choice_change,
    capture_runs,
    complete,
    mock_providers,
    save_preference,
)
from test_browser_notes_provider import fake_notes_provider as fake_notes_provider
from test_browser_task_progress import CONTRAST_JS, overflow, record_requests, running_notes

from asr_studio.storage import database_path

pytestmark = pytest.mark.browser

NARROW = {"width": 360, "height": 740}
# Per provider: the saved model, then the names the action shows for it.
CHOICES = {
    "chatgpt": (CHATGPT_MODEL, "ChatGPT", "GPT 6.1 Sol"),
    "antigravity": (ANTIGRAVITY_MODEL, "Google Antigravity", "Gemini 3 Pro High"),
}
# The notes tab has one primary button at most, and it is the action.
PRIMARY = "#meeting-panel-notes .primary-button:visible"


def save_choice(page, live, provider, **statuses):
    """Save a model per provider, `provider`'s last so it is the saved choice; answer the API."""
    other = "antigravity" if provider == "chatgpt" else "chatgpt"
    for name in (other, provider):
        save_preference(
            page, live, name, CHOICES[name][0], "high" if name == "chatgpt" else "default"
        )
    mock_providers(page, live, **statuses)


def open_notes(page, live, meeting_id):
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#meeting-panel-notes")).to_be_visible()


def name_speakers(live, generation_id, *names):
    """Give the seeded transcript these speakers, in the order they were first heard."""
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE generation_speakers SET display_name = ? WHERE generation_id = ?",
            (names[0], generation_id),
        )
        for index, name in enumerate(names[1:], start=1):
            connection.execute(
                "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
                "first_seen_seconds, last_seen_seconds) VALUES (?, ?, ?, ?, 5.0)",
                (generation_id, f"SPEAKER_{index:02d}", name, 1.0 + index),
            )


def post_run(page):
    """Press the action and return the accepted run."""
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted:
        page.locator("#notes-start").click()
    assert accepted.value.status == 202
    return accepted.value.json()


# ---- AC 1: one primary action, from the saved choice ----


@pytest.mark.parametrize("provider", ["chatgpt", "antigravity"])
def test_one_primary_action_names_the_saved_choice(live, signed_in, fake_notes_provider, provider):
    model, provider_name, model_name = CHOICES[provider]
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, provider)
    runs = capture_runs(page)
    open_notes(page, live, meeting_id)

    start = page.locator("#notes-start")
    expect(start).to_have_text(f"Tạo notes · English · {provider_name} {model_name}")
    expect(start).to_be_enabled()
    expect(page.locator(PRIMARY)).to_have_count(1)
    expect(page.locator(PRIMARY)).to_have_attribute("id", "notes-start")

    # The language is part of the name, and it is what a press sends.
    page.locator("#notes-change").click()
    page.locator("#notes-output-language").select_option("vi")
    expect(start).to_have_text(f"Tạo notes · Tiếng Việt · {provider_name} {model_name}")
    post_run(page)
    assert runs[0]["provider"] == provider
    assert runs[0]["output_language"] == "vi"
    assert runs[0]["expected_model"] == model


def test_with_notes_the_action_is_a_secondary_button_in_the_notes_toolbar(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)

    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    start = page.get_by_role("group", name="Công cụ notes").locator("#notes-start")
    expect(start).to_have_text("Tạo lại notes · English · ChatGPT GPT 6.1 Sol")
    expect(start).to_be_enabled()
    expect(start).to_have_class(re.compile(r"\bsecondary-button\b"))
    expect(start).not_to_have_class(re.compile(r"\bprimary-button\b"))
    expect(page.locator(PRIMARY)).to_have_count(0)
    expect(page.locator("#notes-create #notes-start")).to_have_count(0)


def test_the_action_moves_into_the_toolbar_when_the_first_notes_arrive(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)

    start = page.locator("#notes-start")
    expect(page.locator("#notes-create #notes-start")).to_have_count(1)
    expect(start).to_have_class(re.compile(r"\bprimary-button\b"))
    expect(page.locator("#notes-saved-work")).to_be_hidden()
    task = post_run(page)
    # While it runs the action keeps its place, disabled, beside the cancel that stops the run.
    expect(page.locator("#notes-cancel")).to_be_visible()
    expect(start).to_be_disabled()
    page.locator("#notes-change").focus()

    complete(live.settings.data_dir, task["id"], "# First\n\nNotes.\n")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-toolbar #notes-start")).to_have_count(1)
    expect(page.locator("#notes-create #notes-start")).to_have_count(0)
    expect(start).to_have_text("Tạo lại notes · English · ChatGPT GPT 6.1 Sol")
    expect(start).to_have_class(re.compile(r"\bsecondary-button\b"))
    expect(page.locator(PRIMARY)).to_have_count(0)
    expect(page.locator("#notes-cancel")).to_be_hidden()
    # Moving the group must not drop the keyboard focus that was inside it.
    expect(page.locator("#notes-change")).to_be_focused()


def test_a_run_in_progress_keeps_its_button_and_its_cancel(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = running_notes(live)
    # Not connected: the prompt to connect is for the next run, not for the one in progress.
    save_choice(page, live, "chatgpt", chatgpt="disconnected")
    page.goto(f"{live.url}/meetings/{meeting_id}")

    # Wait for the loaded page first. Until the saved choice is read the button is already disabled
    # and the hidden cancel already has its text, so the checks below would hold without the guard.
    cancel = page.locator("#notes-cancel")
    start = page.locator("#notes-start")
    expect(cancel).to_be_visible()
    expect(start).to_have_text(re.compile(r"^Tạo notes · English · ChatGPT"))
    expect(cancel).to_have_text("Hủy tạo notes")
    expect(start).to_be_disabled()
    expect(start).to_have_attribute("data-action", "start")
    expect(page.get_by_text("Kết nối ChatGPT để tạo notes")).to_have_count(0)
    cancel.click()
    expect(cancel).to_have_text("Đang dừng…")
    expect(cancel).to_be_disabled()


# ---- AC 2: "Đổi…" opens the choice and the description of what is sent ----


def test_change_opens_the_three_selects_and_the_description_of_what_is_sent(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)
    expect(page.locator("#notes-start")).to_be_enabled()

    change = page.get_by_role("button", name="Đổi…")
    choice = page.locator("#notes-choice")
    expect(change).to_have_attribute("aria-controls", "notes-choice")
    expect(change).to_have_attribute("aria-expanded", "false")
    for selector in ("#notes-choice", "#notes-output-language", "#notes-provider", "#notes-model"):
        expect(page.locator(selector)).to_be_hidden()
    # The description is no longer always there.
    expect(page.locator("#notes-content-policy")).to_be_hidden()

    requests = record_requests(page)
    change.click()
    expect(change).to_have_attribute("aria-expanded", "true")
    expect(choice).to_be_visible()
    expect(choice.get_by_role("combobox")).to_have_count(3)
    expect(choice.get_by_label("Ngôn ngữ đầu ra")).to_have_value("en")
    expect(choice.get_by_label("Nhà cung cấp")).to_have_value("chatgpt")
    expect(choice.get_by_label("Model")).to_have_value(CHATGPT_MODEL)
    description = page.locator("#notes-content-policy")
    expect(description).to_be_visible()
    expect(description).to_contain_text("được gửi tới ChatGPT")
    expect(description).to_contain_text("File audio/video, tên cuộc họp và thông tin tài khoản")

    change.click()
    expect(change).to_have_attribute("aria-expanded", "false")
    expect(choice).to_be_hidden()
    assert requests == []  # showing and hiding the choice reads and writes nothing

    # From the keyboard: Enter opens it, Space closes it, and focus stays on the button.
    change.focus()
    page.keyboard.press("Enter")
    expect(choice).to_be_visible()
    page.keyboard.press("Space")
    expect(choice).to_be_hidden()
    expect(change).to_be_focused()


def test_choices_made_under_change_are_saved_and_the_action_follows(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)
    start = page.locator("#notes-start")
    expect(start).to_have_text("Tạo notes · English · ChatGPT GPT 6.1 Sol")

    page.locator("#notes-change").click()
    page.locator("#notes-provider").select_option("antigravity")
    expect(page.locator("#notes-preference-status")).to_contain_text(
        "Đã lưu lựa chọn Google Antigravity"
    )
    expect(start).to_have_text("Tạo notes · English · Google Antigravity Gemini 3 Pro High")
    page.locator("#notes-model").select_option(ANTIGRAVITY_OTHER_MODEL)
    expect(start).to_have_text("Tạo notes · English · Google Antigravity Claude Sonnet Test")

    # It was saved on the server, so a reload shows it, with the choice closed again.
    page.reload()
    expect(start).to_have_text("Tạo notes · English · Google Antigravity Claude Sonnet Test")
    expect(page.locator("#notes-choice")).to_be_hidden()


def test_the_choice_is_closed_again_on_another_meeting(live, signed_in):
    _context, page = signed_in()
    first, *_ = seed_completed_transcript(live.settings.data_dir, title="Alpha review")
    second, *_ = seed_completed_transcript(live.settings.data_dir, title="Beta sync")
    save_choice(page, live, "chatgpt")
    open_notes(page, live, first)
    expect(page.locator("#notes-start")).to_be_enabled()
    page.locator("#notes-change").click()
    expect(page.locator("#notes-choice")).to_be_visible()

    # From the sidebar the page stays loaded, so only the change of meeting can close it.
    page.locator(f'a[href="/meetings/{second}"]').first.click()
    expect(page.locator("#meeting-title")).to_have_text("Beta sync")
    page.locator("#meeting-tab-notes").click()  # no saved notes: it opened on the transcript
    expect_tab(page, "notes")
    expect(page.locator("#notes-change")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#notes-choice")).to_be_hidden()
    expect(page.locator("#notes-start")).to_be_enabled()


# ---- AC 3: no connected provider or model: the action connects instead ----


@pytest.mark.parametrize(
    ("provider", "status"), [("chatgpt", "disconnected"), ("antigravity", "reconnect_required")]
)
def test_an_unconnected_provider_replaces_the_action_with_connecting(
    live, signed_in, provider, status
):
    _model, provider_name, _model_name = CHOICES[provider]
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    # The other provider is connected; the page must still name the saved one and not switch.
    save_choice(page, live, provider, **{provider: status})
    runs = capture_runs(page)
    open_notes(page, live, meeting_id)

    start = page.locator("#notes-start")
    expect(start).to_have_text(f"Kết nối {provider_name} để tạo notes")
    expect(start).to_be_enabled()
    expect(page.get_by_role("button", name=re.compile("^Tạo notes"))).to_have_count(0)
    expect(page.locator(PRIMARY)).to_have_count(1)
    expect(page.locator("#notes-provider")).to_have_value(provider)
    # It says why, without opening "Đổi…".
    expect(page.locator("#notes-run-plan")).to_be_visible()

    start.click()
    dialog = page.locator("#connections-dialog")
    expect(dialog).to_be_visible()
    body = page.locator(".connections-body")
    if provider == "antigravity":
        # Landed on the Google Antigravity section, as its header button does: focus on its
        # heading, and the dialog scrolled down to it once its data has loaded.
        expect(page.locator("#antigravity-title")).to_be_focused()
        wait_for(page, lambda: body.evaluate("element => element.scrollTop") > 0)
        expect(page.locator("#antigravity-title")).to_be_in_viewport()
    else:
        # The ChatGPT section is the top of the dialog.
        assert body.evaluate("element => element.scrollTop") == 0
        expect(page.locator("#connection-sign-in")).to_be_in_viewport()
        expect(page.locator("#antigravity-title")).not_to_be_focused()
    assert runs == []  # connecting is not a run

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(start).to_be_focused()  # back on the button that opened it


def test_a_missing_model_opens_choice_and_nothing_picks_one_for_the_operator(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", "model-that-was-removed")
    mock_providers(page, live)
    runs = capture_runs(page)
    open_notes(page, live, meeting_id)

    start = page.locator("#notes-start")
    expect(start).to_have_text("Chọn hoặc đổi model để tạo notes.")
    expect(page.locator("#notes-run-plan")).to_be_visible()
    expect(page.locator("#notes-run-plan")).to_contain_text("không tự chọn thay")
    expect(page.locator("#notes-model")).to_have_value("")
    # The action opened "Đổi…" and focused the model; picking it brings the action back.
    start.click()
    expect(page.locator("#notes-choice")).to_be_visible()
    expect(page.locator("#notes-model")).to_be_focused()
    page.locator("#notes-model").select_option(ANTIGRAVITY_MODEL)
    expect(start).to_have_text("Tạo notes · English · Google Antigravity Gemini 3 Pro High")
    assert runs == []


def test_once_connected_the_prompt_turns_back_into_the_action_on_the_same_button(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, "chatgpt", chatgpt="disconnected")
    open_notes(page, live, meeting_id)
    start = page.locator("#notes-start")
    expect(start).to_have_text("Kết nối ChatGPT để tạo notes")
    start.click()
    dialog = page.locator("#connections-dialog")
    expect(dialog).to_be_visible()

    mock_providers(page, live)  # ChatGPT is connected now, as the dialog reports it
    announce_choice_change(page)
    expect(start).to_have_text("Tạo notes · English · ChatGPT GPT 6.1 Sol")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(start).to_be_enabled()
    expect(start).to_be_focused()  # the dialog hands focus back to a control that is still there


def test_a_stored_retry_keeps_its_button_when_the_provider_is_not_connected(live, signed_in):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_choice(page, live, "chatgpt", chatgpt="disconnected")
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=503, json={"detail": {"code": "temporary", "message": "Retry."}})

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    stored = {
        "request_id": "stored-retry-1",
        "output_language": "en",
        "provider": "chatgpt",
        "expected_generation_id": generation_id,
        "expected_source_version": 1,
        "expected_metadata_version": 1,
    }
    page.evaluate(
        "([key, value]) => localStorage.setItem(key, value)",
        [f"asr-studio:notes-request-v2:{meeting_id}", json.dumps(stored)],
    )
    page.reload()

    # A request that may have been accepted is verified under its own ID, not swapped for a prompt
    # to connect. The button is enabled only once the saved choice is read, so wait for that.
    start = page.locator("#notes-start")
    expect(start).to_be_enabled()
    expect(start).to_have_text("Xác minh yêu cầu notes")
    expect(start).to_have_attribute("data-action", "start")
    expect(page.locator("#notes-provider")).to_be_disabled()
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")):
        start.click()
    assert bodies and bodies[0]["request_id"] == stored["request_id"]
    expect(page.locator("#connections-dialog")).to_be_hidden()


# ---- AC 4: speakers still carrying a default name ----


def test_default_speaker_names_are_listed_with_a_way_to_name_them_first(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    # Only "Speaker" and a number is the system's own name; the rest were typed by a person.
    name_speakers(live, generation_id, "Speaker 1", "Speaker 2 (host)", "Speaker 10")
    save_choice(page, live, "chatgpt")
    runs = capture_runs(page)
    open_notes(page, live, meeting_id)

    reminder = page.locator("#notes-speaker-reminder")
    expect(reminder).to_be_visible()
    expect(reminder).to_contain_text("Notes sẽ dùng tên người nói hiện tại: Speaker 1, Speaker 10.")
    expect(reminder).not_to_contain_text("(host)")
    # It does not block creating notes.
    expect(page.locator("#notes-start")).to_be_enabled()

    link = reminder.get_by_role("link", name="Đặt tên trước")
    expect(link).to_be_visible()
    link.click()
    expect_tab(page, "transcript")
    assert fragment(page) == "transcript"
    expect(page.locator("#speaker-catalog-title")).to_be_focused()
    assert page.evaluate("Boolean(document.activeElement.closest('#speaker-catalog'))")
    # The names it listed are the chips that invite one.
    marked = page.locator("#speaker-catalog-list > li").filter(has_text="đặt tên")
    expect(marked).to_have_count(2)
    expect(marked.locator(".speaker-chip-name")).to_have_text(["Speaker 1", "Speaker 10"])
    assert runs == []

    page.go_back()
    expect_tab(page, "notes")
    post_run(page)
    assert len(runs) == 1


def test_the_reminder_follows_the_names_and_is_gone_once_every_speaker_is_named(live, signed_in):
    _context, page = signed_in()
    named, *_ = seed_completed_transcript(live.settings.data_dir, title="Named speakers")
    meeting_id, generation_id, _ = seed_completed_transcript(
        live.settings.data_dir, title="Unnamed speaker"
    )
    name_speakers(live, generation_id, "Speaker 1")
    save_choice(page, live, "chatgpt")

    open_notes(page, live, named)
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator("#notes-speaker-reminder")).to_be_hidden()

    open_notes(page, live, meeting_id)
    reminder = page.locator("#notes-speaker-reminder")
    expect(reminder).to_contain_text("hiện tại: Speaker 1.")
    reminder.get_by_role("link", name="Đặt tên trước").click()
    chip = speaker_chip(page, 0)
    expect(chip).to_be_enabled()
    chip.click()
    page.locator("#speaker-catalog-list input").fill("Morgan")
    page.get_by_role("button", name="Lưu tên").click()
    expect(speaker_chip(page, 0).locator(".speaker-chip-name")).to_have_text("Morgan")
    page.locator("#meeting-tab-notes").click()
    expect(reminder).to_be_hidden()
    expect(page.locator("#notes-start")).to_be_enabled()


def test_a_run_holds_the_source_so_the_reminder_steps_aside(live, signed_in, fake_notes_provider):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    name_speakers(live, generation_id, "Speaker 1")
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)
    expect(page.locator("#notes-speaker-reminder")).to_be_visible()

    post_run(page)
    expect(page.locator("#notes-steps")).to_be_visible()
    # Names cannot change while the run reads the source, so there is nothing to do first.
    expect(page.locator("#notes-speaker-reminder")).to_be_hidden()


def test_naming_first_shows_the_current_transcript_when_a_past_one_is_open(live, signed_in):
    _context, page = signed_in()
    show_a_past_generation(page)
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    name_speakers(live, generation_id, "Speaker 1")
    save_choice(page, live, "chatgpt")
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect_tab(page, "transcript")
    page.locator("#generation-selector").select_option("browser-past-generation")
    expect(page.locator("#speaker-catalog-list button.speaker-chip")).to_have_count(0)  # reads only

    page.locator("#meeting-tab-notes").click()
    page.locator("#notes-speaker-reminder").get_by_role("link", name="Đặt tên trước").click()
    expect_tab(page, "transcript")
    expect(page.locator("#generation-selector")).to_have_value(generation_id)
    expect(speaker_chip(page, 0)).to_be_enabled()
    expect(page.locator("#speaker-catalog-title")).to_be_focused()


# ---- AC 6: 360px, both themes, keyboard focus, reduced motion ----


def assert_fits_and_reads(page):
    """No horizontal overflow, the action's name whole inside its button, and every piece of new
    text clears 4.5:1 in the theme on screen."""
    assert overflow(page) == 0
    wide = page.evaluate(
        """() => [...document.querySelectorAll("#meeting-panel-notes *")]
          .filter(el => el.getClientRects().length
            && el.getBoundingClientRect().right > document.documentElement.clientWidth + 0.5)
          .map(el => el.id || el.className || el.tagName)"""
    )
    assert wide == []
    # An ancestor hides what spills out of the button, so every box can fit while its name is cut.
    assert page.locator("#notes-start").evaluate(
        "el => el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1"
    )
    texts = (
        "#notes-start, #notes-change, .notes-speaker-reminder, .notes-speaker-reminder a, "
        "#notes-run-plan, #notes-preference-status, #notes-content-policy, #notes-choice-reload, "
        ".notes-output-language-control label, .notes-output-language-control select"
    )
    assert min(page.evaluate(CONTRAST_JS, texts)) >= 4.5


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_action_area_fits_360px_in_both_themes(live, signed_in, scheme):
    # Reduced motion, so no colour is read mid-fade.
    _context, page = signed_in(color_scheme=scheme, viewport=NARROW, reduced_motion="reduce")
    first, generation_id, _ = seed_completed_transcript(live.settings.data_dir, title="First")
    name_speakers(live, generation_id, "Speaker 1", "Speaker 2")
    saved, *_ = _seed_saved_notes(live.settings.data_dir)
    # The longest provider name makes the longest action name.
    save_choice(page, live, "antigravity")
    start = page.locator("#notes-start")

    open_notes(page, live, first)
    expect(start).to_have_text("Tạo notes · English · Google Antigravity Gemini 3 Pro High")
    expect(start).to_be_enabled()
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    expect(page.locator("#notes-speaker-reminder")).to_be_visible()
    assert_fits_and_reads(page)
    page.locator("#notes-change").click()
    expect(page.locator("#notes-content-policy")).to_be_visible()
    assert_fits_and_reads(page)

    # With notes the group sits in the toolbar, and the action is secondary.
    open_notes(page, live, saved)
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(start).to_have_text("Tạo lại notes · English · Google Antigravity Gemini 3 Pro High")
    assert_fits_and_reads(page)
    page.locator("#notes-change").click()
    assert_fits_and_reads(page)

    # And the prompt to connect, in the same place.
    mock_providers(page, live, antigravity="disconnected")
    announce_choice_change(page)
    expect(start).to_have_text("Kết nối Google Antigravity để tạo notes")
    assert_fits_and_reads(page)


def test_at_360px_the_action_takes_the_whole_row_and_change_sits_below_it(live, signed_in):
    _context, page = signed_in(viewport=NARROW, reduced_motion="reduce")
    meeting_id, *_ = seed_completed_transcript(live.settings.data_dir)
    # The short name of connecting: a long name fills the row on its own and hides a missing rule.
    save_choice(page, live, "antigravity", antigravity="disconnected")
    open_notes(page, live, meeting_id)
    start = page.locator("#notes-start")
    expect(start).to_have_text("Kết nối Google Antigravity để tạo notes")

    # One read: the status strip above can still settle and move both boxes between two reads.
    action, row, change = page.evaluate(
        """() => ["#notes-start", ".notes-run-actions", "#notes-change"].map((selector) => {
          const { x, y, width, height } = document.querySelector(selector).getBoundingClientRect();
          return { x, y, width, height };
        })"""
    )
    assert abs(action["width"] - row["width"]) <= 1, (action, row)
    assert change["y"] >= action["y"] + action["height"] - 1, (action, change)


def test_with_notes_the_action_group_is_the_last_full_width_row_of_the_toolbar(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-start")).to_have_text(
        "Tạo lại notes · English · ChatGPT GPT 6.1 Sol"
    )

    toolbar = page.locator("#notes-toolbar").bounding_box()
    group = page.locator("#notes-run").bounding_box()
    tools = page.locator(".notes-toolbar-actions").bounding_box()
    assert abs(group["width"] - toolbar["width"]) <= 1, (group, toolbar)
    assert group["y"] >= tools["y"] + tools["height"] - 1, (group, tools)


def test_keyboard_focus_is_visible_on_the_new_controls_and_they_add_no_motion(live, signed_in):
    _context, page = signed_in(reduced_motion="reduce")
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    name_speakers(live, generation_id, "Speaker 1")
    save_choice(page, live, "chatgpt")
    open_notes(page, live, meeting_id)
    expect(page.locator("#notes-start")).to_be_enabled()

    # Tab through the area as a keyboard user does, from the tab above it.
    page.locator("#meeting-tab-notes").focus()
    for focused in ("notes-start", "notes-change", "notes-name-speakers"):
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == focused
        assert_focus_ring(page)

    # Enter on "Đặt tên trước" moves focus to the catalog heading, and the ring shows there too.
    page.keyboard.press("Enter")
    expect(page.locator("#speaker-catalog-title")).to_be_focused()
    assert_focus_ring(page)
    page.go_back()
    expect(page.locator("#notes-change")).to_be_visible()

    # The motion rule reaches them, and the choice and the reminder animate nothing.
    for selector in ("#notes-start", "#notes-change", "#notes-name-speakers"):
        duration = page.locator(selector).evaluate(
            "el => parseFloat(getComputedStyle(el).transitionDuration)"
        )
        assert duration <= 0.001, selector
    page.locator("#notes-change").click()
    for selector in ("#notes-choice", "#notes-speaker-reminder", "#notes-run-plan"):
        assert (
            page.locator(selector).evaluate("el => getComputedStyle(el).animationName") == "none"
        ), selector
