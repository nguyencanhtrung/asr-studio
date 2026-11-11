"""Saved notes keep their evidence through every later edit (real browser).

Each test names the acceptance criterion of issue #12 it proves. Data is seeded straight into the
temporary database through the production paths, so no account or network is used. A test that
asserts a state which also holds before the page has loaded first waits for a signal that only the
loaded page gives.
"""

from __future__ import annotations

import pytest
from notes_helpers import rerun_transcript, seed_cited_notes, seed_completed_transcript
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import settle
from test_browser_history import signed_in as signed_in
from test_browser_notes import _seed_saved_notes
from test_browser_notes_citations import CHIP, DESKTOP, NARROW, OVERFLOW, open_notes
from test_browser_task_progress import CONTRAST_JS, record_requests, running_notes

from asr_studio.notes import update_note
from asr_studio.recordings import update_meeting
from asr_studio.tasks import finish_canceled_task, update_current_generation_speaker_name

pytestmark = pytest.mark.browser

NO_DATE = "Chưa đặt ngày"
RE_RUN_NOTE = (
    "Transcript hiện tại là một lần chép lời khác với bản chụp này, "
    "nên đoạn được đánh dấu chỉ khớp theo mốc thời gian."
)


def dated_notes(live, date):
    """A meeting with this date (or none) and one saved notes revision citing its transcript."""
    seeded = seed_completed_transcript(
        live.settings.data_dir, title="Snapshot meeting", meeting_date=date
    )
    return seed_cited_notes(live.settings.data_dir, existing=seeded)


# ---- AC 2: a source edit marks the notes as changed; the title alone does not ----


def test_a_transcript_edit_marks_the_saved_notes_as_changed(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = dated_notes(live, "2026-10-19")
    open_notes(live, page, meeting_id)
    meta = page.locator("#notes-revision-meta")
    expect(meta).to_contain_text("Nguồn hiện tại")  # loaded, and nothing has changed yet

    page.locator("#meeting-tab-transcript").click()
    row = page.locator(".transcript-segment").first
    row.locator(".segment-edit-button").click()
    row.locator(".segment-text-editor").fill("Changed after the notes were made.")
    row.locator('button[type="submit"]').click()
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.", timeout=10_000)

    page.locator("#meeting-tab-notes").click()
    expect(meta).to_contain_text("Nguồn đã thay đổi")
    expect(meta).not_to_contain_text("Nguồn hiện tại")


@pytest.mark.parametrize(
    ("date", "fields", "label"),
    [
        ("2026-10-19", {"title": "Renamed"}, "Nguồn hiện tại"),
        ("2026-10-19", {"date": "2026-10-20"}, "Nguồn đã thay đổi"),
        ("2026-10-19", {"date": ""}, "Nguồn đã thay đổi"),
        (None, {"date": "2026-10-20"}, "Nguồn đã thay đổi"),
    ],
    ids=["title only", "date changed", "date cleared", "date added"],
)
def test_only_a_date_edit_marks_the_notes_as_changed_never_the_title(
    live, signed_in, date, fields, label
):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = dated_notes(live, date)
    open_notes(live, page, meeting_id)
    meta = page.locator("#notes-revision-meta")
    expect(meta).to_contain_text("Nguồn hiện tại")

    page.locator("#meeting-actions").click()

    page.get_by_role("button", name="Sửa thông tin").click()
    if "title" in fields:
        page.locator("#edit-title").fill(fields["title"])
    if fields.get("date"):
        page.locator("#edit-date").fill(fields["date"])
    elif "date" in fields:
        page.locator("#edit-clear-date").click()
    # The save makes the page read the notes again; wait for that answer before asserting.
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/api/meetings/{meeting_id}/notes")
            and response.request.method == "GET"
        )
    ) as reread:
        page.get_by_role("button", name="Lưu thay đổi").click()
    reread.value.finished()
    expect(page.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.", timeout=10_000)
    settle(page)
    expect(meta).to_contain_text(label)
    if label == "Nguồn hiện tại":
        expect(meta).not_to_contain_text("Nguồn đã thay đổi")
        expect(page.locator("#meeting-title")).to_have_text("Renamed")


# ---- AC 4: the source card shows the snapshot's date and says how the meeting now differs ----


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "narrow"])
@pytest.mark.parametrize(
    ("date", "shown"), [("2026-10-19", "2026-10-19"), (None, NO_DATE)], ids=["dated", "undated"]
)
def test_the_card_shows_the_meeting_date_the_snapshot_was_made_with(
    live, signed_in, viewport, date, shown
):
    _context, page = signed_in(viewport=viewport)
    meeting_id, _revision, snapshot, _ids = dated_notes(live, date)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])

    expect(page.locator("#notes-source-date")).to_have_text(shown)
    expect(page.locator("#notes-source-difference")).to_be_hidden()
    assert page.evaluate(OVERFLOW) == 0


@pytest.mark.parametrize(
    ("date", "later", "note"),
    [
        (
            "2026-10-19",
            "2026-10-20",
            "Ngày họp hiện tại (2026-10-20) khác ngày họp trong bản chụp (2026-10-19).",
        ),
        (
            "2026-10-19",
            None,
            f"Ngày họp hiện tại ({NO_DATE}) khác ngày họp trong bản chụp (2026-10-19).",
        ),
        (
            None,
            "2026-10-20",
            f"Ngày họp hiện tại (2026-10-20) khác ngày họp trong bản chụp ({NO_DATE}).",
        ),
    ],
    ids=["changed", "cleared", "added"],
)
def test_a_date_edited_after_the_notes_is_said_on_the_card_which_keeps_the_saved_date(
    live, signed_in, date, later, note
):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, _ids = dated_notes(live, date)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    difference = page.locator("#notes-source-difference")
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-date")).to_have_text(date or NO_DATE)
    expect(difference).to_be_hidden()

    update_meeting(
        live.settings.data_dir, meeting_id, expected_version=1, fields={"meeting_date": later}
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )

    expect(difference).to_have_text(note, timeout=10_000)
    expect(page.locator("#notes-source-date")).to_have_text(date or NO_DATE)  # still the snapshot's


@pytest.mark.parametrize(
    ("later", "note"),
    [
        (
            "2026-10-20",
            "Ngày họp hiện tại (2026-10-20) khác ngày họp trong bản chụp (2026-10-19).",
        ),
        (None, f"Ngày họp hiện tại ({NO_DATE}) khác ngày họp trong bản chụp (2026-10-19)."),
    ],
    ids=["changed", "cleared"],
)
def test_a_card_opened_after_a_date_edit_shows_the_date_the_snapshot_was_made_with(
    live, signed_in, later, note
):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, _ids = dated_notes(live, "2026-10-19")
    update_meeting(
        live.settings.data_dir, meeting_id, expected_version=1, fields={"meeting_date": later}
    )
    open_notes(live, page, meeting_id)  # the page reads the meeting with its new date first
    page.locator(CHIP).first.click()

    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-date")).to_have_text("2026-10-19")  # not the current date
    expect(page.locator("#notes-source-difference")).to_have_text(note)


def test_a_date_edited_after_a_transcript_re_run_is_said_beside_the_re_run_note(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = dated_notes(live, "2026-10-19")
    rerun_transcript(live.settings.data_dir, meeting_id)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).last.click()
    difference = page.locator("#notes-source-difference")
    expect(difference).to_have_text(RE_RUN_NOTE)

    update_meeting(
        live.settings.data_dir,
        meeting_id,
        expected_version=1,
        fields={"meeting_date": "2026-10-20"},
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )

    expect(difference).to_have_text(
        f"{RE_RUN_NOTE} Ngày họp hiện tại (2026-10-20) khác ngày họp trong bản chụp (2026-10-19).",
        timeout=10_000,
    )


def test_a_title_edited_after_the_notes_leaves_the_card_without_a_difference(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, _ids = dated_notes(live, "2026-10-19")
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])

    update_meeting(
        live.settings.data_dir, meeting_id, expected_version=1, fields={"title": "Renamed"}
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )

    # The page installs the meeting, and tells the card, before it repaints the heading.
    expect(page.locator("#meeting-title")).to_have_text("Renamed", timeout=10_000)
    expect(page.locator("#notes-source-difference")).to_be_hidden()


def test_a_speaker_renamed_after_the_notes_is_said_on_the_card_which_keeps_the_saved_name(
    live, signed_in
):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, _ids = dated_notes(live, "2026-10-19")
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-speaker")).to_have_text("Morgan")
    expect(page.locator("#notes-source-difference")).to_be_hidden()

    update_current_generation_speaker_name(
        live.settings.data_dir,
        meeting_id,
        "SPEAKER_00",
        expected_generation_id=snapshot["generation_id"],
        expected_source_version=snapshot["source_version"],
        display_name="Dana",
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )

    difference = page.locator("#notes-source-difference")
    expect(difference).to_have_text(
        "Người nói của đoạn này đã đổi trong transcript hiện tại.", timeout=10_000
    )
    expect(page.locator("#notes-source-speaker")).to_have_text("Morgan")


# ---- AC 6: a broken source link in hand-edited Markdown is kept as typed, with a warning ----


def broken_link_markdown(meeting_id, snapshot, segment_ids):
    base = f"/meetings/{meeting_id}?notes_source={snapshot['id']}&segment="
    broken = base + "f" * 32
    typed = (
        "## Decisions\n\n"
        f"- Keep the scope [00:00:09]({broken}) as typed\n"
        f"- A valid link [00:00:01]({base}{segment_ids[0]}) still opens\n"
    )
    return typed, broken


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "narrow"])
def test_a_broken_source_link_is_saved_as_typed_with_a_warning_and_opens_no_source(
    live, signed_in, viewport
):
    context, page = signed_in(viewport=viewport)
    meeting_id, _revision, snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    typed, broken = broken_link_markdown(meeting_id, snapshot, segment_ids)
    requests = record_requests(page)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-preview li").first).to_be_visible()  # the saved notes are painted
    warning = page.locator("#notes-link-warning")
    expect(warning).to_be_hidden()  # a generated revision names no broken link

    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(typed)
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )

    expect(warning).to_be_visible()
    expect(warning).to_contain_text("1 liên kết nguồn không hợp lệ")
    expect(warning.locator("code")).to_have_text(broken)
    expect(page.locator("#notes-markdown")).to_have_value(typed)  # nothing stripped or repaired
    assert page.evaluate(OVERFLOW) == 0

    page.locator("#notes-mode-view").click()
    invalid = page.locator("#notes-preview .notes-link-invalid")
    expect(invalid).to_have_count(1)
    expect(invalid).to_have_text("00:00:09 (liên kết nguồn không hợp lệ)")
    links = page.locator("#notes-preview a")
    expect(links).to_have_count(1)  # only the valid link is still a link
    assert page.evaluate("document.querySelector('.notes-link-invalid').closest('a')") is None
    page.evaluate("window.__beforeClick = true")  # a full page load would drop it
    invalid.click()
    settle(page)
    expect(page.locator("#notes-source-view")).to_be_hidden()
    assert page.evaluate("window.__beforeClick") is True
    assert [path for _method, path in requests if path.startswith("/api/notes-sources/")] == []
    assert "notes_source" not in page.url

    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.locator("#notes-copy").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("Đã sao chép bản đã lưu")
    assert page.evaluate("navigator.clipboard.readText()") == typed
    with page.expect_download() as download:
        page.locator("#notes-export").click()
    with open(download.value.path(), encoding="utf-8") as exported:
        assert exported.read() == typed

    page.reload()  # the warning comes from the server's read, so a reload brings it back
    expect(page.locator("#notes-preview li").first).to_be_visible(timeout=10_000)
    expect(warning).to_contain_text("1 liên kết nguồn không hợp lệ")
    expect(page.locator("#notes-preview .notes-link-invalid")).to_have_count(1)
    links.click()  # the valid link still opens its saved source
    expect(page.locator("#notes-source-view")).to_be_visible()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_broken_link_and_its_warning_stay_readable_in_both_themes(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme, viewport=DESKTOP)
    meeting_id, revision, snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    typed, _broken = broken_link_markdown(meeting_id, snapshot, segment_ids)
    update_note(live.settings.data_dir, revision["id"], expected_edit_version=1, markdown=typed)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-link-warning")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-preview .notes-link-invalid")).to_have_count(1)

    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    assert min(page.evaluate(CONTRAST_JS, "#notes-link-warning, .notes-link-invalid")) >= 4.5
    # Colour is not the only cue: the inert text is underlined with a dashed line.
    assert (
        page.locator(".notes-link-invalid").evaluate(
            "element => getComputedStyle(element).textDecorationStyle"
        )
        == "dashed"
    )


def test_a_link_spelled_with_an_encoded_notes_source_key_is_not_opened_as_a_source(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, revision, snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    address = f"/meetings/{meeting_id}?notes%5Fsource={snapshot['id']}&segment={segment_ids[0]}"
    typed = f"- Typed [00:00:01]({address})\n"
    update_note(live.settings.data_dir, revision["id"], expected_edit_version=1, markdown=typed)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    item = page.locator("#notes-preview li").first
    expect(item).to_be_visible(timeout=10_000)
    expect(item).to_contain_text(f"[00:00:01]({address})")  # drawn as typed, not as a link
    expect(page.locator("#notes-preview a")).to_have_count(0)
    expect(page.locator("#notes-link-warning")).to_be_hidden()  # the server does not flag it


# ---- AC 3 and 8: the date controls and transcript edits stay locked until the run has stopped ----


def test_the_lock_holds_through_a_cancel_request_until_the_run_has_stopped(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, task_id, owner_id = running_notes(live)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    cancel = page.locator("#notes-cancel")
    lock = page.locator("#notes-lock-status")
    expect(cancel).to_be_visible()  # the running task was read, so the lock state is known
    expect(lock).to_be_visible()
    expect(lock).to_contain_text("tạm thời bị khóa")

    page.locator("#meeting-actions").click()

    page.get_by_role("button", name="Sửa thông tin").click()
    date, clear_date, title = (
        page.locator(f"#edit-{name}") for name in ("date", "clear-date", "title")
    )
    expect(date).to_be_disabled()
    expect(clear_date).to_be_disabled()
    expect(title).to_be_enabled()  # the title never touches the source

    cancel.click()
    # The server's answer says the run is stopping, not stopped: the worker still holds the source.
    expect(page.locator("#notes-status")).to_contain_text("Tác vụ vẫn giữ slot", timeout=10_000)
    settle(page, 1_500)  # and the next poll of the notes confirms it
    expect(lock).to_be_visible()
    expect(date).to_be_disabled()
    expect(clear_date).to_be_disabled()
    page.locator("#meeting-tab-transcript").click()
    edits = page.locator(".segment-edit-button")
    expect(edits).to_have_count(2)
    for edit in (edits.first, edits.last, page.locator(".speaker-chip").first):
        expect(edit).to_be_disabled()

    assert finish_canceled_task(
        live.settings.data_dir, task_id, owner_id=owner_id, elapsed_seconds=20
    )
    expect(lock).to_be_hidden(timeout=10_000)
    expect(date).to_be_enabled()
    expect(clear_date).to_be_enabled()
    for edit in (edits.first, edits.last, page.locator(".speaker-chip").first):
        expect(edit).to_be_enabled()
    expect(page.locator("#notes-status")).to_contain_text("Worker đã xác nhận hủy")
