"""Timestamp citations in saved meeting notes, shown as chips in Xem only (real browser).

Each test names the acceptance criterion of issue #48 it proves. Notes are seeded through the
production path: the renderer and the citation builder run over the frozen source snapshot, and
`publish_notes_revision` stores both, so the page reads them from the real endpoints.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from notes_helpers import default_cited_document, rerun_transcript, seed_cited_notes
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes import _write_test_recording

from asr_studio.tasks import update_current_generation_segment_text

pytestmark = pytest.mark.browser

DESKTOP = {"width": 1280, "height": 900}
NARROW = {"width": 360, "height": 800}
OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"
CHIP = "#notes-preview a.notes-citation-chip"
CARD_TITLE = "Nguồn đã lưu lúc tạo notes"
COUNT_PLAYS = """
  window.__plays = 0;
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    window.__plays += 1;
    return play.apply(this, arguments);
  };
"""


def open_notes(live, page, meeting_id, suffix=""):
    page.goto(f"{live.url}/meetings/{meeting_id}{suffix}")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-preview li").first).to_be_visible()


def chip_pairs(page):
    """[bullet text, its chips] for each bullet of the rendered notes, in order."""
    return page.evaluate(
        """() => {
          const words = (item) => [...item.childNodes]
            .filter((node) => node.nodeType === 3)
            .map((node) => node.textContent)
            .join("")
            .trim();
          return [...document.querySelectorAll("#notes-preview li")].map((item) => [
            words(item),
            [...item.querySelectorAll("a.notes-citation-chip")].map((chip) => chip.textContent),
          ]);
        }"""
    )


def chip_labels(page):
    """The chips of each bullet of the rendered notes, by bullet text."""
    return {words: chips for words, chips in chip_pairs(page)}


def edit_markdown(page, replace):
    """Change the draft in Viết (not saved), then return to Xem."""
    page.locator("#notes-mode-write").click()
    textarea = page.locator("#notes-markdown")
    textarea.fill(replace(textarea.input_value()))
    page.locator("#notes-mode-view").click()


def expect_tab(page, name):
    other = "transcript" if name == "notes" else "notes"
    expect(page.locator(f"#meeting-tab-{name}")).to_have_attribute("aria-selected", "true")
    expect(page.locator(f"#meeting-tab-{other}")).to_have_attribute("aria-selected", "false")
    expect(page.locator(f"#meeting-panel-{name}")).to_be_visible()
    expect(page.locator(f"#meeting-panel-{other}")).to_be_hidden()


def rect(locator):
    return locator.evaluate(
        """element => {
          const box = element.getBoundingClientRect();
          return { top: box.top, bottom: box.bottom, left: box.left, right: box.right };
        }"""
    )


def long_document(segment_ids):
    return {
        "schema_version": 1,
        "overview": [{"text": "Opening remarks.", "source_ids": [segment_ids[2]]}],
        "key_topics": [],
        "decisions": [{"text": "Ship on Friday.", "source_ids": [segment_ids[25]]}],
        "open_questions": [],
        "action_items": [],
        "confirmation_items": [],
    }


def seed_long_cited_notes(live, tmp_path):
    """Forty one-second segments, a recording of 30 s, and a decision citing the one at 25 s."""
    seeded = seed_cited_notes(
        live.settings.data_dir,
        long_document,
        segment_starts=[float(second) for second in range(40)],
    )
    _write_test_recording(live.settings.data_dir, seeded[0], tmp_path)
    return seeded


# ---- AC 3: Xem shows a chip after each cited bullet, as a link with a spoken name ----


def test_xem_shows_a_chip_after_each_cited_bullet_linking_to_the_saved_source(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, revision, snapshot, segment_ids = seed_cited_notes(
        live.settings.data_dir, segment_starts=[1.0, 3725.0]
    )
    open_notes(live, page, meeting_id)

    assert chip_labels(page) == {
        "The team reviewed its launch plan.": ["00:01"],
        "A second point cites both parts.": ["00:01", "1:02:05"],  # mm:ss, h:mm:ss from an hour on
        "Prepare the launch plan.": ["00:01"],
        "Who will review the draft?": ["1:02:05"],
        "Send the draft. — Owner: Morgan; Deadline: tomorrow (2026-10-20).": ["1:02:05"],
        "Confirm the review date.": ["1:02:05"],
    }
    chips = page.locator(CHIP)
    expect(chips).to_have_count(7)
    base = f"/meetings/{meeting_id}?notes_source={snapshot['id']}&segment="
    assert chips.evaluate_all("items => items.map(item => item.getAttribute('href'))") == [
        f"{base}{segment_ids[0]}#transcript",
        f"{base}{segment_ids[0]}#transcript",
        f"{base}{segment_ids[1]}#transcript",
        f"{base}{segment_ids[0]}#transcript",
        f"{base}{segment_ids[1]}#transcript",
        f"{base}{segment_ids[1]}#transcript",
        f"{base}{segment_ids[1]}#transcript",
    ]
    assert chips.evaluate_all("items => items.map(item => item.tagName)") == ["A"] * 7
    names = chips.evaluate_all("items => items.map(item => item.getAttribute('aria-label'))")
    assert names[:3] == [
        "Nghe lại đoạn nguồn lúc 00:01",
        "Nghe lại đoạn nguồn lúc 00:01",
        "Nghe lại đoạn nguồn lúc 1:02:05",
    ]
    # The chips follow the words of their bullet and nothing follows the chips.
    for item in page.locator("#notes-preview li").all():
        assert item.evaluate(
            """element => {
              const kinds = [...element.childNodes].map(node => node.nodeType === 3 ? "text"
                : node.classList.contains("notes-citation-chip") ? "chip" : "other");
              return kinds[0] === "text" && /^(text )?(chip )*chip$/.test(kinds.join(" "));
            }"""
        )
    expect(page.locator("#notes-preview h4 a, #notes-preview p a")).to_have_count(0)
    assert revision["citations"]  # the chips come from stored data, not from the Markdown


# ---- AC 2: the Markdown, Viết, copy and download carry no link ----


def test_markdown_write_copy_and_download_stay_free_of_timestamp_links(live, signed_in, tmp_path):
    context, page = signed_in(viewport=DESKTOP)
    meeting_id, revision, snapshot, segment_ids = seed_cited_notes(live.settings.data_dir)
    markdown = revision["markdown"]
    open_notes(live, page, meeting_id)
    expect(page.locator(CHIP)).to_have_count(7)

    page.locator("#notes-mode-write").click()
    expect(page.locator("#notes-markdown")).to_have_value(markdown)
    expect(page.locator(CHIP)).to_have_count(0)  # no chip is even built outside Xem
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.locator("#notes-copy").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("edit 1", timeout=10_000)
    assert page.evaluate("navigator.clipboard.readText()") == markdown
    with page.expect_download() as download:
        page.locator("#notes-export").click()
    assert Path(download.value.path()).read_text(encoding="utf-8") == markdown

    api = page.context.request.get(f"{live.url}/api/notes/{revision['id']}")
    assert api.json()["markdown"] == markdown
    for text in (markdown, api.json()["markdown"]):
        assert "notes_source" not in text and "/meetings/" not in text
        assert snapshot["id"] not in text and not any(
            identifier in text for identifier in segment_ids
        )
        assert not re.search(r"\d{1,2}:\d{2}", text) and "](" not in text

    page.locator("#notes-mode-view").click()
    expect(page.locator(CHIP)).to_have_count(7)


# ---- AC 4: a chip only on a bullet that still says what it said; one edit loses no other chip ----


def test_an_edited_bullet_loses_its_chips_and_no_other_bullet_loses_or_gains_any(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, revision, _snapshot, _ids = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    before = chip_labels(page)
    assert all(before.values())

    edit_markdown(
        page,
        lambda text: text.replace("Prepare the launch plan.", "Prepare the launch plan today."),
    )
    after = chip_labels(page)
    assert after.pop("Prepare the launch plan today.") == []
    before.pop("Prepare the launch plan.")
    assert after == before  # every other bullet keeps exactly its own chips
    expect(page.locator("#notes-view-draft")).to_be_visible()

    # The same bullet moved under another heading is not the bullet that was cited there.
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(
        page.locator("#notes-markdown")
        .input_value()
        .replace("- Who will review the draft?\n", "")
        .replace("## Decisions\n\n", "## Decisions\n\n- Who will review the draft?\n"),
    )
    page.locator("#notes-mode-view").click()
    moved = chip_labels(page)
    assert moved["Who will review the draft?"] == []
    assert moved["Confirm the review date."] == ["00:04"]

    # Saved, the edit survives a reload and the stored citations are untouched.
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )
    page.reload()
    expect(page.locator("#notes-preview li").first).to_be_visible(timeout=10_000)
    assert chip_labels(page)["Who will review the draft?"] == []
    assert chip_labels(page)["Confirm the review date."] == ["00:04"]
    api = page.context.request.get(f"{live.url}/api/notes/{revision['id']}").json()
    assert api["edit_version"] == 2 and api["citations"] == revision["citations"]


def test_identical_bullets_keep_their_own_chips_and_none_once_one_is_edited(live, signed_in):
    def document(segment_ids):
        return {
            **default_cited_document(segment_ids),
            "overview": [
                {"text": "Same words.", "source_ids": [segment_ids[0]]},
                {"text": "Same words.", "source_ids": [segment_ids[1]]},
            ],
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "confirmation_items": [],
        }

    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir, document)
    open_notes(live, page, meeting_id)
    twins = page.locator("#notes-preview li")
    expect(twins).to_have_count(2)
    # Used in order: the first twin gets the first entry, the second twin the second.
    assert [twin.locator("a").all_text_contents() for twin in twins.all()] == [["00:01"], ["00:04"]]

    # Words that two bullets share cannot tell which of them was edited, so neither shows a chip:
    # the second must not inherit the first one's.
    edit_markdown(page, lambda text: text.replace("- Same words.", "- Other words.", 1))
    assert [twin.locator("a").all_text_contents() for twin in twins.all()] == [[], []]


@pytest.mark.parametrize(
    ("edited", "copied"),
    [("Alpha", "Bravo"), ("Bravo", "Alpha")],
    ids=["first-becomes-a-copy-of-the-second", "second-becomes-a-copy-of-the-first"],
)
def test_a_bullet_edited_into_a_copy_of_another_leaves_both_copies_without_a_chip(
    live, signed_in, edited, copied
):
    def document(segment_ids):
        return {
            **default_cited_document(segment_ids),
            "overview": [
                {"text": "Alpha point.", "source_ids": [segment_ids[0]]},
                {"text": "Bravo point.", "source_ids": [segment_ids[1]]},
                {"text": "Charlie point.", "source_ids": [segment_ids[0]]},
            ],
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "confirmation_items": [],
        }

    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir, document)
    open_notes(live, page, meeting_id)
    assert chip_pairs(page) == [
        ["Alpha point.", ["00:01"]],
        ["Bravo point.", ["00:04"]],
        ["Charlie point.", ["00:01"]],
    ]

    # The words cannot say which copy is the genuine one, so neither shows a chip: the edited bullet
    # has none, and the genuine one must not wear a chip that may belong to the other.
    edit_markdown(page, lambda text: text.replace(f"{edited} point.", f"{copied} point."))
    assert chip_pairs(page) == [
        [f"{copied} point.", []],
        [f"{copied} point.", []],
        ["Charlie point.", ["00:01"]],  # an unrelated bullet keeps its own
    ]


def test_a_note_published_before_citations_were_kept_shows_no_chip_and_no_error(
    live, signed_in, tmp_path
):
    _context, page = signed_in(viewport=DESKTOP)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    legacy, revision, *_ = seed_cited_notes(
        live.settings.data_dir, title="Legacy notes", with_citations=False
    )
    cited, *_ = seed_cited_notes(live.settings.data_dir, title="Cited notes")
    for meeting_id in (legacy, cited):
        _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    assert revision["citations"] == []

    open_notes(live, page, legacy)
    expect(page.locator("#notes-preview")).to_contain_text("Prepare the launch plan.")
    expect(page.locator(CHIP)).to_have_count(0)
    # Even a payload with no citations field at all, as an older server would send, renders.

    def without_citations(route):
        payload = route.fetch().json()
        for item in payload["revisions"]:
            del item["citations"]
        route.fulfill(json=payload)

    page.route(f"**/api/meetings/{legacy}/notes", without_citations)
    page.reload()
    expect(page.locator("#notes-preview li").first).to_be_visible(timeout=10_000)
    expect(page.locator(CHIP)).to_have_count(0)

    # Control: a note that does carry citations shows its chips, so the empty page above is real.
    open_notes(live, page, cited)
    expect(page.locator(CHIP)).to_have_count(7)
    assert errors == []


def test_vietnamese_notes_get_their_chips_under_the_vietnamese_headings(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir, output_language="vi")
    open_notes(live, page, meeting_id)
    assert page.locator("#notes-preview h4").all_text_contents()[2] == "Quyết định"
    expect(page.locator(CHIP)).to_have_count(7)
    assert chip_labels(page)["Prepare the launch plan."] == ["00:01"]


def test_each_revision_shows_chips_that_open_its_own_saved_source(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, first, first_snapshot, segment_ids = seed_cited_notes(live.settings.data_dir)
    update_current_generation_segment_text(
        live.settings.data_dir,
        meeting_id,
        segment_ids[0],
        expected_generation_id=first_snapshot["generation_id"],
        expected_source_version=first_snapshot["source_version"],
        text="Rewritten before the second run.",
    )
    _meeting, _second, second_snapshot, _ids = seed_cited_notes(
        live.settings.data_dir,
        existing=(meeting_id, first_snapshot["generation_id"], segment_ids),
        source_version=2,
    )
    assert second_snapshot["segments"][0]["text"] == "Rewritten before the second run."

    open_notes(live, page, meeting_id)  # the newest revision shows first
    hrefs = page.locator(CHIP).evaluate_all("items => items.map(item => item.getAttribute('href'))")
    assert hrefs and all(f"notes_source={second_snapshot['id']}&" in href for href in hrefs)
    page.locator("#notes-revision-selector").select_option(first["id"])
    hrefs = page.locator(CHIP).evaluate_all("items => items.map(item => item.getAttribute('href'))")
    assert hrefs and all(f"notes_source={first_snapshot['id']}&" in href for href in hrefs)

    page.locator(CHIP).first.click()
    expect(page.locator("#notes-source-text")).to_have_text(first_snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-difference")).to_be_visible()
    expect(page.locator("#notes-source-difference")).to_contain_text("đã được sửa")


# ---- AC 5: a click opens the Transcript tab and the card under the player, plays, marks; Back ----


def test_a_click_opens_the_source_under_the_player_plays_it_and_marks_its_row(
    live, signed_in, tmp_path
):
    _context, page = signed_in(viewport=DESKTOP, reduced_motion="reduce")
    meeting_id, revision, snapshot, segment_ids = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    expect_tab(page, "notes")
    page.evaluate("window.__sameDocument = 1")  # a full page load would drop it, and drafts with it

    page.locator(CHIP).filter(has_text="00:25").click()

    expect_tab(page, "transcript")
    card = page.locator("#notes-source-view")
    expect(card).to_be_visible()
    expect(card.get_by_role("heading", name=CARD_TITLE)).to_be_visible()
    expect(page.locator("#notes-source-heading")).to_be_focused()
    assert page.evaluate("window.__sameDocument") == 1  # the click navigated inside the page
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][25]["text"])
    expect(page.locator("#notes-source-time")).to_have_text("00:00:25")
    expect(page.locator("#notes-source-status")).to_contain_text("đang phát", timeout=10_000)
    audio = page.locator("#meeting-audio")
    assert audio.evaluate("element => element.currentTime >= 24.7")  # sought, and playing on
    assert audio.evaluate("element => !element.paused || element.ended")

    # The segment it names is marked, labelled, and brought into view below the sticky player.
    row = page.locator(".transcript-segment.is-notes-source")
    expect(row).to_have_count(1)
    assert row.get_attribute("data-segment-id") == segment_ids[25]
    expect(row.locator(".segment-source-mark")).to_have_text("Nguồn của mục notes")
    bar, box = rect(page.locator(".player-bar")), rect(row)
    assert box["top"] >= bar["bottom"] - 1 and box["bottom"] <= page.viewport_size["height"] + 1
    assert page.evaluate("window.scrollY") > 0  # the list was far below the card

    url = urlsplit(page.url)
    assert (url.path, url.fragment) == (f"/meetings/{meeting_id}", "transcript")
    assert parse_qs(url.query) == {
        "notes_source": [revision["source_snapshot_id"]],
        "segment": [segment_ids[25]],
    }

    # Back returns to Meeting notes, with the source closed and the chips still there.
    page.go_back()
    expect_tab(page, "notes")
    expect(card).to_be_hidden()
    expect(page.locator(".transcript-segment.is-notes-source")).to_have_count(0)
    assert urlsplit(page.url).query == ""
    expect(page.locator(CHIP)).to_have_count(2)
    page.go_forward()  # and Forward reopens the same source
    expect_tab(page, "transcript")
    expect(card).to_be_visible()
    expect(page.locator(".transcript-segment.is-notes-source")).to_have_count(1)
    expect(page.locator("#notes-source-heading")).to_be_focused()  # as after the click
    assert page.evaluate("window.__sameDocument") == 1  # the chip, Back and Forward never reloaded


def test_a_browser_that_refuses_to_play_still_opens_and_seeks_the_source(live, signed_in, tmp_path):
    _context, page = signed_in(viewport=DESKTOP, reduced_motion="reduce")
    meeting_id, *_ = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    page.evaluate(
        """() => {
          HTMLMediaElement.prototype.play = () =>
            Promise.reject(new DOMException("Autoplay is blocked.", "NotAllowedError"));
        }"""
    )

    page.locator(CHIP).filter(has_text="00:25").click()

    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-view")).to_be_visible()
    status = page.locator("#notes-source-status")
    expect(status).to_contain_text("Đã tua bản ghi cuộc họp tới 00:00:25", timeout=10_000)
    expect(status).to_contain_text("Bấm phát trên trình phát để nghe")  # said, not silently dropped
    expect(status).not_to_contain_text("đang phát")
    audio = page.locator("#meeting-audio")
    assert audio.evaluate("element => element.paused")
    assert audio.evaluate("element => Math.abs(element.currentTime - 25) < 0.3")
    expect(page.locator(".transcript-segment.is-notes-source")).to_have_count(1)


def test_the_card_sits_directly_under_the_player_in_the_transcript_tab(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    assert page.locator("#meeting-panel-notes #notes-source-view").count() == 0  # not in the notes

    page.locator(CHIP).first.click()
    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-view")).to_be_visible()
    assert page.evaluate(
        """() => {
          const card = document.querySelector("#notes-source-view");
          return card.parentElement.id === "meeting-panel-transcript"
            && card.previousElementSibling.classList.contains("player-bar")
            && card.nextElementSibling.id === "transcription-panel";
        }"""
    )
    page.evaluate(
        "window.scrollTo({ top: 0, behavior: 'instant' })"
    )  # the row may have scrolled it
    bar, card = rect(page.locator(".player-bar")), rect(page.locator("#notes-source-view"))
    assert 0 <= card["top"] - bar["bottom"] <= 32  # nothing but the page's own gap between them


def test_a_second_click_on_the_same_chip_plays_the_source_again(live, signed_in, tmp_path):
    _context, page = signed_in(viewport=DESKTOP, reduced_motion="reduce")
    meeting_id, *_ = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).filter(has_text="00:02").click()
    expect(page.locator("#notes-source-status")).to_contain_text("đang phát", timeout=10_000)
    page.evaluate("document.querySelector('#meeting-audio').currentTime = 20")

    page.locator("#meeting-tab-notes").click()
    page.locator(CHIP).filter(has_text="00:02").click()  # the address is the same: still a click
    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-status")).to_contain_text("Đã tua bản ghi")
    # The page's CSP forbids wait_for_function, so poll until the player is back near 2 s.
    for _ in range(100):
        if page.evaluate("document.querySelector('#meeting-audio').currentTime") < 10:
            break
        page.wait_for_timeout(50)
    else:
        raise AssertionError("the source did not play again from its mark")


# ---- AC 6: the transcript has since changed; and a pasted link works as a click ----


def test_the_card_keeps_the_saved_text_and_says_how_the_transcript_now_differs(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, segment_ids = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-difference")).to_be_hidden()  # nothing differs yet

    update_current_generation_segment_text(
        live.settings.data_dir,
        meeting_id,
        segment_ids[0],
        expected_generation_id=snapshot["generation_id"],
        expected_source_version=snapshot["source_version"],
        text="The launch plan was rewritten afterwards.",
    )
    page.reload()  # the pasted link, after the live transcript was edited
    expect(page.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-context")).to_contain_text("bản chụp transcript bất biến")
    expect(page.locator("#notes-source-difference")).to_be_visible()
    expect(page.locator("#notes-source-difference")).to_contain_text("đã được sửa")
    row = page.locator(".transcript-segment.is-notes-source")
    expect(row.locator(".segment-text")).to_have_text("The launch plan was rewritten afterwards.")


def test_editing_the_marked_row_keeps_its_mark_and_updates_the_difference_note(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, _ids = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    original = snapshot["segments"][0]["text"]
    expect(page.locator("#notes-source-text")).to_have_text(original)
    expect(page.locator("#notes-source-difference")).to_be_hidden()

    row = page.locator(
        ".transcript-segment.is-notes-source"
    )  # re-resolved: editing rebuilds the row
    row.locator(".segment-edit-button").click()
    expect(row.locator(".segment-source-mark")).to_be_visible()  # the open editor keeps the label
    row.locator(".segment-text-editor").fill("Edited live while the source card is open.")
    row.locator('button[type="submit"]').click()

    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.", timeout=10_000)
    expect(row).to_have_count(1)
    expect(row.locator(".segment-text")).to_have_text("Edited live while the source card is open.")
    expect(row.locator(".segment-source-mark")).to_have_text("Nguồn của mục notes")
    expect(page.locator("#notes-source-text")).to_have_text(original)  # the card stays frozen
    expect(page.locator("#notes-source-difference")).to_be_visible()
    expect(page.locator("#notes-source-difference")).to_contain_text("đã được sửa")


def test_a_newer_transcript_with_other_segment_ids_still_marks_the_row_at_that_time(
    live, signed_in
):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, _revision, snapshot, segment_ids = seed_cited_notes(live.settings.data_dir)
    rerun_transcript(live.settings.data_dir, meeting_id)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).last.click()  # the 00:04 source of the confirmation bullet

    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][1]["text"])
    expect(page.locator("#notes-source-difference")).to_be_visible()
    expect(page.locator("#notes-source-difference")).to_contain_text("lần chép lời khác")
    row = page.locator(".transcript-segment.is-notes-source")
    expect(row).to_have_count(1)
    assert row.get_attribute("data-segment-id") not in segment_ids
    expect(row.locator(".segment-text")).to_have_text("A fresh pass heard the follow-up.")


def test_a_pasted_link_opens_the_source_as_a_click_does(live, signed_in, tmp_path):
    context, page = signed_in(viewport=DESKTOP, reduced_motion="reduce")
    meeting_id, revision, snapshot, segment_ids = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    href = page.locator(CHIP).filter(has_text="00:25").get_attribute("href")
    assert href == (
        f"/meetings/{meeting_id}?notes_source={revision['source_snapshot_id']}"
        f"&segment={segment_ids[25]}#transcript"
    )

    pasted = context.new_page()
    pasted.add_init_script(COUNT_PLAYS)
    pasted.goto(f"{live.url}{href}")
    expect_tab(pasted, "transcript")
    expect(
        pasted.locator("#notes-source-view").get_by_role("heading", name=CARD_TITLE)
    ).to_be_visible(timeout=10_000)
    expect(pasted.locator("#notes-source-heading")).to_be_focused()
    expect(pasted.locator("#notes-source-text")).to_have_text(snapshot["segments"][25]["text"])
    expect(pasted.locator("#notes-source-time")).to_have_text("00:00:25")
    # Nobody clicked, so autoplay may be refused: either wording is right, both follow an attempt.
    expect(pasted.locator("#notes-source-status")).to_have_text(
        re.compile(
            r"Đã tua bản ghi cuộc họp tới 00:00:25"
            r"( và đang phát\.|\. Bấm phát trên trình phát để nghe\.)"
        ),
        timeout=10_000,
    )
    assert pasted.evaluate("window.__plays") >= 1  # it tried to play, as a click does
    assert pasted.locator("#meeting-audio").evaluate("element => element.currentTime >= 24.7")
    row = pasted.locator(".transcript-segment.is-notes-source")
    assert row.get_attribute("data-segment-id") == segment_ids[25]
    bar, box = rect(pasted.locator(".player-bar")), rect(row)
    assert box["top"] >= bar["bottom"] - 1 and box["bottom"] <= pasted.viewport_size["height"] + 1

    # Without the hash the link still lands where the card is; with #notes the notes tab shows
    # and the source stays loaded for the tab switch.
    bare = context.new_page()
    bare.goto(f"{live.url}{href.split('#')[0]}")
    expect_tab(bare, "transcript")
    expect(bare.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    expect(bare.locator("#notes-source-heading")).to_be_focused()
    notes_first = context.new_page()
    notes_first.goto(f"{live.url}{href.split('#')[0]}#notes")
    expect_tab(notes_first, "notes")
    notes_first.locator("#meeting-tab-transcript").click()
    expect(notes_first.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    expect(notes_first.locator("#notes-source-text")).to_have_text(snapshot["segments"][25]["text"])


# ---- AC 9: 360 px, both themes, keyboard focus, reduced motion ----


def luminance(color):
    channels = [int(value) / 255 for value in re.findall(r"\d+", color)[:3]]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground, background):
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_chips_and_the_source_card_fit_360px_and_read_in_both_themes(
    live, signed_in, tmp_path, scheme
):
    def crowded(segment_ids):
        unbroken = "x" * 300
        return {
            **long_document(segment_ids),
            "overview": [
                {"text": unbroken, "source_ids": segment_ids[:12]},
                {"text": "Opening remarks.", "source_ids": [segment_ids[2]]},
            ],
        }

    _context, page = signed_in(viewport=NARROW, color_scheme=scheme, reduced_motion="reduce")
    meeting_id, *_ = seed_cited_notes(
        live.settings.data_dir, crowded, segment_starts=[float(s) for s in range(40)]
    )
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    open_notes(live, page, meeting_id)
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    expect(page.locator(CHIP)).to_have_count(3)
    assert page.evaluate(OVERFLOW) == 0
    assert (
        page.evaluate(
            "() => { const frame = document.querySelector('.notes-frame');"
            " return frame.scrollWidth - frame.clientWidth; }"
        )
        == 0
    )

    pairs = page.evaluate(
        """() => {
          const chip = document.querySelector("a.notes-citation-chip");
          const style = getComputedStyle(chip);
          return { chip: [style.color, style.backgroundColor] };
        }"""
    )
    page.locator(CHIP).filter(has_text="00:25").click()
    expect(page.locator("#notes-source-view")).to_be_visible()
    assert page.evaluate(OVERFLOW) == 0
    opened = page.evaluate(
        """() => {
          const pair = selector => {
            const style = getComputedStyle(document.querySelector(selector));
            return [style.color, style.backgroundColor];
          };
          return {
            mark: pair(".transcript-segment.is-notes-source .segment-source-mark"),
            heading: [getComputedStyle(document.querySelector("#notes-source-heading")).color,
                      getComputedStyle(document.querySelector("#notes-source-view")).backgroundColor],
          };
        }"""
    )
    for name, (foreground, background) in {**pairs, **opened}.items():
        assert contrast(foreground, background) >= 4.5, (name, foreground, background)
    card = page.locator("#notes-source-view").bounding_box()
    assert card is not None and card["x"] >= 0 and card["x"] + card["width"] <= 360


def test_a_chip_takes_keyboard_focus_with_a_visible_ring_and_opens_on_enter(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)

    # The view/write switch sits above the frame, so its Viết is the Tab stop before the chips.
    page.locator("#notes-mode-write").focus()
    page.keyboard.press("Tab")
    focused = page.locator(CHIP).first
    expect(focused).to_be_focused()
    ring = focused.evaluate(
        "element => [getComputedStyle(element).outlineStyle,"
        " parseFloat(getComputedStyle(element).outlineWidth),"
        " parseFloat(getComputedStyle(element).outlineOffset)]"
    )
    assert ring[0] == "solid" and ring[1] >= 2 and ring[2] >= 0, ring
    assert (
        page.locator(CHIP).first.evaluate("element => element.getBoundingClientRect().height") >= 24
    )
    page.keyboard.press("Tab")
    expect(page.locator(CHIP).nth(1)).to_be_focused()  # each chip is its own stop, in reading order

    page.locator(CHIP).first.focus()
    page.keyboard.press("Enter")
    expect_tab(page, "transcript")
    expect(page.locator("#notes-source-view")).to_be_visible()
    expect(page.locator("#notes-source-heading")).to_be_focused()


@pytest.mark.parametrize(
    ("preference", "behavior"), [("reduce", "auto"), ("no-preference", "smooth")]
)
def test_the_row_is_scrolled_into_view_only_with_motion_the_reader_allows(
    live, signed_in, tmp_path, preference, behavior
):
    _context, page = signed_in(viewport=DESKTOP, reduced_motion=preference)
    meeting_id, *_ = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    page.evaluate(
        """() => {
          const original = Element.prototype.scrollIntoView;
          window.__scrolls = [];
          Element.prototype.scrollIntoView = function (options) {
            window.__scrolls.push(options);
            return original.call(this, options);
          };
        }"""
    )
    page.locator(CHIP).filter(has_text="00:25").click()
    expect(page.locator(".transcript-segment.is-notes-source")).to_have_count(1)
    scrolls = page.evaluate("window.__scrolls")
    assert scrolls[0]["block"] == "nearest"
    # Playback may follow along too, but never with motion the reader switched off.
    assert {call["behavior"] for call in scrolls} == {behavior}


def early_document(segment_ids):
    return {
        **long_document(segment_ids),
        "overview": [{"text": "Early point.", "source_ids": [segment_ids[1]]}],
    }


def test_a_row_near_the_first_screen_lands_between_the_player_bar_and_the_viewport_bottom(
    live, signed_in, tmp_path
):
    _context, page = signed_in(viewport=DESKTOP, reduced_motion="reduce")
    meeting_id, _revision, _snapshot, segment_ids = seed_cited_notes(
        live.settings.data_dir,
        early_document,
        segment_starts=[float(second) for second in range(40)],
    )
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    open_notes(live, page, meeting_id)

    # A real click would scroll the chip into view first and hide the order of mark, seek and card.
    assert page.evaluate("window.scrollY") == 0
    page.locator(CHIP).filter(has_text="00:01").dispatch_event("click")

    row = page.locator(".transcript-segment.is-notes-source")
    expect(row).to_have_count(1)
    assert row.get_attribute("data-segment-id") == segment_ids[1]
    expect(page.locator("#notes-source-status")).to_contain_text("tua", timeout=10_000)
    bar, box = rect(page.locator(".player-bar")), rect(row)
    assert box["top"] >= bar["bottom"] - 1 and box["bottom"] <= page.viewport_size["height"] + 1


def test_a_tab_switch_keeps_keyboard_focus_on_a_citation_chip(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir)
    open_notes(live, page, meeting_id)
    chip = page.locator(CHIP).first
    chip.focus()
    expect(chip).to_be_focused()

    page.evaluate(
        """() => {
          for (const hidden of [true, false]) {
            Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
            document.dispatchEvent(new Event("visibilitychange"));
          }
        }"""
    )

    expect(chip).to_be_focused()


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW])
def test_adjacent_group_opens_all_members_and_keyboard_seeks(live, signed_in, tmp_path, viewport):
    _context, page = signed_in(viewport=viewport)
    meeting_id, revision, snapshot, ids = seed_cited_notes(
        live.settings.data_dir, segment_starts=[1.0, 2.0]
    )
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    open_notes(live, page, meeting_id)
    group = page.locator(CHIP).filter(has_text="2 đoạn")
    expect(group).to_have_text("00:01–00:03 · 2 đoạn")
    group.focus()
    group.press("Enter")
    members = page.locator("#notes-source-members a")
    expect(members).to_have_count(2)
    expect(page.locator("#notes-source-text")).to_have_text("Segment 0.")
    members.nth(1).focus()
    members.nth(1).press("Enter")
    expect(page.locator("#notes-source-text")).to_have_text("Segment 1.")
    expect(page.locator("#notes-source-heading")).to_be_focused()
    expect(page.locator("#notes-source-status")).not_to_be_empty()
    assert page.evaluate(OVERFLOW) <= 1
    page.go_back()
    expect(page.locator("#notes-source-text")).to_have_text("Segment 0.")
    page.go_forward()
    expect(page.locator("#notes-source-text")).to_have_text("Segment 1.")


def test_group_keeps_clicked_item_members_and_historical_revision(live, signed_in):
    def document(ids):
        result = default_cited_document(ids)
        result["overview"] = [
            {"text": "Two sources.", "source_ids": ids[:2]},
            {"text": "Three sources.", "source_ids": ids[:3]},
        ]
        return result

    _context, page = signed_in(viewport=NARROW)
    meeting_id, first, snapshot, ids = seed_cited_notes(
        live.settings.data_dir, document, segment_starts=[1.0, 2.0, 3.0]
    )
    rerun_transcript(live.settings.data_dir, meeting_id)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).filter(has_text="3 đoạn").click()
    expect(page.locator("#notes-source-members a")).to_have_count(3)
    expect(page.locator("#notes-source-difference")).to_contain_text("lần chép lời khác")
    page.locator("#notes-source-members a").nth(2).click()
    expect(page.locator("#notes-source-text")).to_have_text("Segment 2.")
    page.go_back()
    expect(page.locator("#notes-source-members a")).to_have_count(3)
    page.locator("#meeting-tab-notes").click()
    page.locator(CHIP).filter(has_text="2 đoạn").click()
    expect(page.locator("#notes-source-members a")).to_have_count(2)


def test_group_projection_switches_with_notes_revision(live, signed_in):
    _context, page = signed_in(viewport=DESKTOP)
    meeting_id, first, snapshot, ids = seed_cited_notes(
        live.settings.data_dir, segment_starts=[1.0, 2.0]
    )
    update_current_generation_segment_text(
        live.settings.data_dir,
        meeting_id,
        ids[0],
        expected_generation_id=snapshot["generation_id"],
        expected_source_version=1,
        text="Revised transcript.",
    )
    _, second, _, _ = seed_cited_notes(
        live.settings.data_dir,
        existing=(meeting_id, snapshot["generation_id"], ids),
        source_version=2,
        segment_starts=[10.0, 20.0],
    )
    open_notes(live, page, meeting_id)
    expect(page.locator(CHIP).filter(has_text="2 đoạn")).to_have_count(0)
    page.locator("#notes-revision-selector").select_option(first["id"])
    group = page.locator(CHIP).filter(has_text="2 đoạn")
    expect(group).to_have_text("00:01–00:03 · 2 đoạn")
    group.click()
    expect(page.locator("#notes-source-text")).to_have_text("Segment 0.")
    page.locator("#meeting-tab-notes").click()
    page.locator("#notes-revision-selector").select_option(second["id"])
    expect(page.locator(CHIP).filter(has_text="2 đoạn")).to_have_count(0)
