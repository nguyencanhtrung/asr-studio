"""The meeting player stays in view and the transcript follows playback (real browser)."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from test_browser_history import FIXED_NOW, show_a_past_generation
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes_citations import CHIP, open_notes, seed_long_cited_notes

from asr_studio.media import normalize_media
from asr_studio.storage import database_path

pytestmark = pytest.mark.browser

SEGMENT_COUNT = 40
SEGMENT_SECONDS = 2
MAX_BAR_HEIGHT = 72
QUIET_MS = 2000
LIST_TOP = 150  # px below the viewport's top edge: the transcript list is on screen
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
NARROW = {"width": 360, "height": 740}
MINIMUM = {"width": 320, "height": 640}  # the narrowest width DESIGN.md supports
BOTH_VIEWPORTS = pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])

# The seeded recording has no media file, so the audio element never plays on its own. These
# tests drive the properties and events a playing element exposes.
STUB_AUDIO = """() => {
  const audio = document.querySelector("#meeting-audio");
  const player = (window.__player = { time: 0, paused: true, ended: false, plays: 0 });
  const define = (name, descriptor) =>
    Object.defineProperty(audio, name, { configurable: true, ...descriptor });
  define("currentTime", { get: () => player.time, set: (value) => { player.time = value; } });
  define("paused", { get: () => player.paused });
  define("ended", { get: () => player.ended });
  define("readyState", { get: () => HTMLMediaElement.HAVE_ENOUGH_DATA });
  audio.play = () => {
    player.plays += 1;
    player.paused = false;
    return Promise.resolve();
  };
}"""

RECT = """element => {
  const box = element.getBoundingClientRect();
  return { top: box.top, bottom: box.bottom, left: box.left, right: box.right, height: box.height };
}"""

# The real page ends right after the list. Room below lets the whole list scroll up under the bar.
ROOM_AFTER_LIST = """() => {
  const room = document.createElement("div");
  room.style.height = "3000px";
  document.querySelector("#meeting-view").append(room);
}"""


# Only the page's own spacing separates the source card from the list here. Real pages put the
# task details and the speaker list between them; this keeps the list off screen at the card.
NOTES_BELOW_SOURCE = """() => {
  const notes = document.createElement("div");
  notes.style.height = "800px";
  document.querySelector("#notes-source-view").after(notes);
}"""

# The real wheel reaches Chromium immediately, but its delivery to the page can be queued after
# the input call returns. Hold one real event at the browser boundary and deliver it after the
# original helper's 50 ms stability poll; the acknowledgement fix must wait for this receipt. The
# Web Animation clock remains real while the page's Playwright clock is paused.
DELAYED_WHEEL_DELIVERY = """() => {
  let delayed = true;
  window.addEventListener("wheel", event => {
    if (!delayed) return;
    delayed = false;
    const deltaX = event.deltaX;
    const deltaY = event.deltaY;
    event.stopImmediatePropagation();
    const delay = document.createElement("div");
    delay.style.cssText = "position:fixed;left:-10px;width:1px;height:1px;" +
      "opacity:0;pointer-events:none";
    document.body.append(delay);
    delay.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 100 }).finished.then(() => {
      delay.remove();
      window.dispatchEvent(new WheelEvent("wheel", {
        deltaX,
        deltaY,
        bubbles: true,
        cancelable: true,
      }));
    });
  }, { capture: true, passive: false });
}"""


def seed_long_transcript(data_dir: Path, count: int = SEGMENT_COUNT) -> str:
    """A completed transcript of back-to-back two-second segments, tall enough to scroll."""
    meeting_id, generation_id, _segments = seed_completed_transcript(data_dir, title="Long review")
    seconds = count * SEGMENT_SECONDS
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "DELETE FROM transcript_segments WHERE generation_id = ?", (generation_id,)
        )
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, ?, ?, ?, ?, 'SPEAKER_00', 'SPEAKER_00', "
            "-0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
            [
                (
                    uuid.uuid4().hex,
                    generation_id,
                    index,
                    index * SEGMENT_SECONDS,
                    (index + 1) * SEGMENT_SECONDS,
                    f"Segment {index} of the long review.",
                )
                for index in range(count)
            ],
        )
        connection.execute(
            "UPDATE transcript_generations SET segment_count = ? WHERE id = ?",
            (count, generation_id),
        )
        connection.execute(
            "UPDATE tasks SET segment_count = ?, transcript_end_seconds = ?, "
            "audio_duration_seconds = ? WHERE meeting_id = ?",
            (count, seconds, seconds, meeting_id),
        )
        connection.execute(
            "UPDATE recordings SET duration_seconds = ? WHERE meeting_id = ?", (seconds, meeting_id)
        )
    return meeting_id


def write_recording(data_dir: Path, meeting_id: str, tmp_path: Path, seconds: int) -> None:
    """Store a silent but decodable recording where the meeting's audio route reads it."""
    with sqlite3.connect(database_path(data_dir)) as connection:
        row = connection.execute(
            "SELECT storage_key FROM recordings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
    target = data_dir / row[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "player.wav"
    with wave.open(str(source), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 16_000 * seconds)
    normalize_media(source, target, max_duration_seconds=60)


def load_meeting(live, page, meeting_id, count):
    page.set_default_timeout(10_000)  # a missing element should fail the test, not stall it
    failed = []
    page.on("requestfailed", lambda request: failed.append(f"{request.url} {request.failure}"))
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="networkidle")
    try:
        expect(page.locator(".transcript-segment")).to_have_count(count)
    except AssertionError as error:
        # The browser's occasional ERR_NETWORK_CHANGED lands here; name it instead of "0 segments".
        raise AssertionError(f"transcript did not render; failed requests: {failed}") from error


def open_meeting(live, signed_in, *, count=SEGMENT_COUNT, **options):
    _context, page = signed_in(**options)
    load_meeting(live, page, seed_long_transcript(live.settings.data_dir, count), count)
    expect(page.locator(".segment-edit-button").first).to_be_enabled()
    page.evaluate(STUB_AUDIO)
    return page


def freeze_clock(page):
    """Own the page clock: `performance.now()` only moves when a test advances it."""
    start = datetime.fromisoformat(FIXED_NOW)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(minutes=1))


def settle(page):
    """Let any earlier scroll fall out of the two-second manual-scroll window."""
    page.clock.run_for(QUIET_MS + 100)


def wait_for(page, expression, arg=None, timeout=5.0):
    """Poll a page expression from Python, which keeps working while the page clock is paused."""
    deadline = time.monotonic() + timeout
    while not page.evaluate(expression, arg):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {expression}")
        page.wait_for_timeout(25)


def segment(page, index):
    return page.locator(".transcript-segment").nth(index)


def rect(locator):
    return locator.evaluate(RECT)


def scroll_y(page):
    return page.evaluate("window.scrollY")


def scroll_to(page, y):
    """Scroll, and return once the page has heard its own scroll event.

    Under the paused page clock that event is stamped when it is delivered. One that arrives after
    `settle()` advanced the clock would open a fresh two-second manual-scroll window.
    """
    page.evaluate(
        """y => new Promise((resolve) => {
          const before = window.scrollY;
          window.scrollTo(0, y);
          if (window.scrollY === before) resolve();
          else window.addEventListener("scroll", resolve, { once: true });
        })""",
        y,
    )


def place(locator, top, edge="top"):
    """Scroll so the element's `edge` ("top" or "bottom") sits `top` px below the viewport's top."""
    scroll_to(
        locator.page,
        locator.evaluate(
            "(element, [top, edge]) => scrollY + element.getBoundingClientRect()[edge] - top",
            [top, edge],
        ),
    )


def scroll_segment_to(page, index, top):
    place(segment(page, index), top)


def show_list(page):
    """Scroll the transcript list into view, as when comparing it with the audio."""
    scroll_segment_to(page, 0, LIST_TOP)


def put_list(page, where, *, inside):
    """Scroll the transcript list 5 px outside the area below the player bar, or 5 px inside it.

    "below": its top edge at the foot of the viewport. "above": its bottom edge under the bar.
    """
    items = page.locator("#transcript-segments")
    margin = 5 if inside else -5
    if where == "below":
        place(items, page.evaluate("window.innerHeight") - margin)
    else:
        place(items, rect(page.locator(".player-bar"))["height"] + margin, edge="bottom")


def play_at(page, index, *, into=0.5, paused=False, ended=False):
    """Move the stubbed audio into a segment and let the page hear it; returns scrollY."""
    return page.evaluate(
        """([seconds, paused, ended]) => {
          Object.assign(window.__player, { time: seconds, paused, ended });
          document.querySelector("#meeting-audio").dispatchEvent(new Event("timeupdate"));
          return window.scrollY;
        }""",
        [index * SEGMENT_SECONDS + into, paused, ended],
    )


def clearance(locator):
    """How far below the viewport's top edge the browser leaves a target it scrolls to."""
    return locator.evaluate(
        """element => parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)
          + parseFloat(getComputedStyle(element).scrollMarginTop)"""
    )


def assert_pinned(page, viewport):
    bar = page.locator(".player-bar")
    box = rect(bar)
    assert abs(box["top"]) <= 1, box
    assert box["height"] <= MAX_BAR_HEIGHT, box
    assert box["left"] >= 0 and box["right"] <= viewport["width"], box
    # The heading and the hint are not part of the sticky bar: they scrolled away.
    assert rect(page.locator("#audio-heading"))["bottom"] <= 0
    assert rect(page.locator(".audio-hint"))["bottom"] <= 0
    # The player is still usable: nothing covers it.
    topmost = page.evaluate(
        """() => {
          const box = document.querySelector("#meeting-audio").getBoundingClientRect();
          return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2).id;
        }"""
    )
    assert topmost == "meeting-audio"


# ---- AC: the player sticks to the top of the content and only the player sticks ----


@BOTH_VIEWPORTS
def test_player_bar_sticks_while_the_heading_and_hint_scroll_away(live, signed_in, viewport):
    page = open_meeting(live, signed_in, viewport=viewport)
    bar = page.locator(".player-bar")
    assert bar.evaluate("element => getComputedStyle(element).position") == "sticky"
    assert bar.evaluate("element => [...element.children].map(child => child.id)") == [
        "meeting-audio"
    ]
    assert rect(bar)["height"] <= MAX_BAR_HEIGHT

    list_top = page.evaluate(
        "document.querySelector('#transcript-segments').getBoundingClientRect().top + scrollY"
    )
    middle_top = segment(page, SEGMENT_COUNT // 2).evaluate(
        "element => element.getBoundingClientRect().top + window.scrollY - 300"
    )
    # Stuck where the transcript list starts, over its middle, and at the very end of the page.
    for y in (list_top, middle_top, page.evaluate("document.documentElement.scrollHeight")):
        scroll_to(page, y)
        assert_pinned(page, viewport)


def test_player_bar_stays_above_positioned_content_scrolling_under_it(live, signed_in):
    page = open_meeting(live, signed_in)
    # Positioned boxes later in the page would paint over a sticky bar that has no z-index.
    page.evaluate(
        """document.querySelectorAll(".transcript-segment, .transcript-segment *")
          .forEach((element) => { element.style.position = "relative"; })"""
    )
    scroll_to(page, 3000)
    assert_pinned(page, DESKTOP)


# ---- AC: scrolled-to targets are never hidden behind the sticky player ----


@BOTH_VIEWPORTS
def test_scroll_targets_stay_clear_of_the_player_bar(live, signed_in, viewport):
    page = open_meeting(live, signed_in, viewport=viewport)
    scroll_to(page, 1500)
    bar = rect(page.locator(".player-bar"))
    # The notes source heading is no target here: it is in the Notes tab, which pins no player.
    targets = [segment(page, 3), segment(page, 17), segment(page, 31)]
    for target in targets:
        assert clearance(target) > bar["height"]  # the bar's height plus a gap
        target.evaluate("element => element.scrollIntoView({ block: 'start' })")
        assert rect(target)["top"] >= bar["bottom"] - 0.5


@BOTH_VIEWPORTS
def test_keyboard_focus_is_not_left_under_the_player_bar(live, signed_in, viewport):
    page = open_meeting(live, signed_in, viewport=viewport)
    segment(page, 5).locator(".segment-edit-button").click()
    editor = segment(page, 5).locator(".segment-text-editor")
    editor.fill("Short.")  # a browser reveals only the caret's line: keep it the first line
    place(editor, 20)  # the editor sits under the bar, the next control just below it
    bar = rect(page.locator(".player-bar"))
    assert rect(editor)["top"] < bar["bottom"]
    segment(page, 5).locator("button[type=submit]").focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement.className") == "segment-text-editor"
    assert rect(editor)["top"] >= bar["bottom"]


CARET_LINE_TOP = """element => {
  const style = getComputedStyle(element);
  const lines = element.value.split("\\n").length;
  const lineHeight = parseFloat(style.lineHeight);
  return element.getBoundingClientRect().top + parseFloat(style.borderTopWidth)
    + parseFloat(style.paddingTop) + (lines - 1) * lineHeight - element.scrollTop;
}"""


def test_keyboard_focus_keeps_the_caret_line_of_a_wrapped_value_below_the_bar(live, signed_in):
    page = open_meeting(live, signed_in, viewport=PHONE)
    segment(page, 5).locator(".segment-edit-button").click()
    editor = segment(page, 5).locator(".segment-text-editor")
    editor.fill("\n".join(f"Line {number}." for number in range(1, 9)))  # the caret ends on line 8
    place(editor, 20)  # the editor's top sits under the bar, the next control just below it
    bar = rect(page.locator(".player-bar"))
    assert rect(editor)["top"] < bar["bottom"]
    segment(page, 5).locator("button[type=submit]").focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement.className") == "segment-text-editor"
    assert editor.evaluate(CARET_LINE_TOP) >= bar["bottom"]


# ---- AC: a changed active segment is scrolled into view while playing ----


@BOTH_VIEWPORTS
def test_active_segment_change_while_playing_scrolls_it_into_view_nearest(
    live, signed_in, viewport
):
    page = open_meeting(live, signed_in, viewport=viewport, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    play_at(page, 0, paused=True)
    settle(page)
    margin = clearance(segment(page, 0))

    # Below the viewport: its bottom edge lands on the bottom edge of the viewport.
    play_at(page, 10)
    expect(segment(page, 10)).to_have_attribute("aria-current", "true")
    assert abs(rect(segment(page, 10))["bottom"] - viewport["height"]) <= 1

    # Above the viewport: its top edge lands just under the player bar.
    scroll_segment_to(page, 30, 100)
    settle(page)
    play_at(page, 5)
    assert abs(rect(segment(page, 5))["top"] - margin) <= 1
    assert rect(segment(page, 5))["top"] >= rect(page.locator(".player-bar"))["bottom"]

    # Already fully in view: the page stays where it is.
    scroll_segment_to(page, 20, 250)
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 20) == before
    expect(segment(page, 20)).to_have_attribute("aria-current", "true")


def test_only_a_change_of_active_segment_scrolls(live, signed_in):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 10) > before  # a new active segment is followed ...
    show_list(page)
    settle(page)
    before = scroll_y(page)
    # ... but more playback inside that same segment, now far below the viewport, is not.
    assert play_at(page, 10, into=1.5) == before


def test_following_continues_across_consecutive_smooth_scrolls(live, signed_in):
    page = open_meeting(live, signed_in)
    page.clock.install(time=FIXED_NOW)
    show_list(page)
    play_at(page, 0, paused=True)
    settle(page)
    # The long hop animates for over a second. Its scroll events are the app's own, so the hop
    # back up still follows, however far the page travelled.
    for index, edge in ((10, "bottom"), (39, "bottom"), (3, "top")):
        play_at(page, index)
        wait_for(
            page,
            """([index, edge]) => {
              const item = document.querySelectorAll(".transcript-segment")[index];
              const box = item.getBoundingClientRect();
              const padding = getComputedStyle(document.documentElement).scrollPaddingTop;
              const clearance = parseFloat(padding)
                + parseFloat(getComputedStyle(item).scrollMarginTop);
              return edge === "bottom"
                ? Math.abs(box.bottom - window.innerHeight) <= 1
                : Math.abs(box.top - clearance) <= 1;
            }""",
            [index, edge],
            timeout=10.0,
        )


# ---- AC: no following while paused / just after a manual scroll / editing ----


@pytest.mark.parametrize(
    ("paused", "ended"), [(True, False), (False, True)], ids=["paused", "ended"]
)
def test_no_following_while_the_audio_is_not_playing(live, signed_in, paused, ended):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    play_at(page, 0, paused=True)
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 10, paused=paused, ended=ended) == before
    expect(segment(page, 10)).to_have_attribute("aria-current", "true")
    # Playing again follows the next change, so the page held still only because of the pause.
    assert play_at(page, 11) > before


def user_scrolls(page, gesture):
    """Scroll as a user would. Wheel and key presses happen at the end of the page, where they
    move nothing, so only their own listeners (not the scroll listener) can notice them."""
    if gesture == "wheel":
        page.mouse.move(640, 300)
        page.evaluate(
            """() => {
              window.__browserWheelReceipt = false;
              window.addEventListener("wheel", () => {
                queueMicrotask(() => { window.__browserWheelReceipt = true; });
              }, { once: true, passive: true });
            }"""
        )
        page.mouse.wheel(0, 200)
        wait_for(page, "() => window.__browserWheelReceipt === true", timeout=5.0)
    elif gesture == "keyboard":
        page.keyboard.press("End")
    elif gesture == "touch":
        page.evaluate("document.dispatchEvent(new Event('touchmove', { bubbles: true }))")
    else:
        # A scroll with no wheel, touch or key before it, like dragging the scrollbar.
        page.evaluate(
            """() => new Promise((resolve) => {
              window.addEventListener("scroll", resolve, { once: true });
              window.scrollBy(0, -200);
            })"""
        )
    last = None
    for _ in range(40):
        current = scroll_y(page)
        if current == last:
            return current
        last = current
        page.wait_for_timeout(50)
    raise AssertionError("scrolling never settled")


@pytest.mark.parametrize("gesture", ["wheel", "keyboard", "touch", "scrollbar"])
def test_no_following_for_two_seconds_after_a_manual_scroll(live, signed_in, gesture):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    play_at(page, 0, paused=True)
    scroll_to(page, page.evaluate("document.documentElement.scrollHeight"))
    settle(page)

    before = user_scrolls(page, gesture)
    assert play_at(page, 10) == before
    page.clock.run_for(QUIET_MS - 100)
    assert play_at(page, 11) == before
    page.clock.run_for(200)
    assert play_at(page, 12) != before


def test_wheel_delivery_is_received_before_manual_scroll_assertion(live, signed_in):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    play_at(page, 0, paused=True)
    scroll_to(page, page.evaluate("document.documentElement.scrollHeight"))
    settle(page)
    page.evaluate(DELAYED_WHEEL_DELIVERY)

    before = user_scrolls(page, "wheel")
    assert play_at(page, 10) == before
    page.clock.run_for(QUIET_MS - 100)
    assert play_at(page, 11) == before
    page.clock.run_for(200)
    assert play_at(page, 12) != before


@pytest.mark.parametrize(
    ("selector", "stops_following", "editing"),
    [
        (".segment-text-editor", True, True),
        (".segment-speaker-select", True, True),
        ("#speaker-add-name", True, False),
        (".segment-timestamp", False, False),
    ],
    ids=["textarea", "select", "input", "button"],
)
def test_no_following_while_focus_is_in_a_transcript_edit_field(
    live, signed_in, selector, stops_following, editing
):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    if editing:  # the textarea and the select exist only while a segment is being edited
        segment(page, 0).locator(".segment-edit-button").click()
    play_at(page, 0, paused=True)
    page.locator(selector).first.evaluate("element => element.focus({ preventScroll: true })")
    settle(page)
    before = scroll_y(page)
    followed = play_at(page, 10) != before
    assert followed is not stops_following
    if stops_following:
        # Leaving the field lets the next change follow, so the focus was the only reason.
        page.evaluate("document.activeElement.blur()")
        assert play_at(page, 11) != before


@pytest.mark.parametrize(
    ("selector", "opener"),
    [("#edit-title", "#edit-meeting"), ("#history-search", None)],
    ids=["meeting-edit", "sidebar"],
)
def test_following_continues_while_focus_is_in_a_field_outside_the_transcript(
    live, signed_in, selector, opener
):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    if opener:  # the title field is on the page only while the meeting editor is open
        page.locator("#meeting-actions").click()
        page.locator(opener).click()
    show_list(page)
    play_at(page, 0, paused=True)
    field = page.locator(selector)
    field.evaluate("element => element.focus({ preventScroll: true })")
    expect(field).to_be_focused()  # a field that took no focus would prove nothing
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 10) > before


# ---- no following while the transcript list is off screen (the reader is on the notes) ----


@pytest.mark.parametrize(
    ("viewport", "where"),
    [(DESKTOP, "below"), (PHONE, "below"), (DESKTOP, "above")],
    ids=["below-desktop", "below-phone", "above-desktop"],
)
def test_no_following_while_the_transcript_list_is_off_screen(live, signed_in, viewport, where):
    page = open_meeting(live, signed_in, viewport=viewport, reduced_motion="reduce")
    freeze_clock(page)
    if where == "above":
        page.evaluate(ROOM_AFTER_LIST)
    play_at(page, 0, paused=True)
    put_list(page, where, inside=False)
    settle(page)
    before = scroll_y(page)
    # Playing on moves the highlight, not the page: the list is not what the reader is looking at.
    assert play_at(page, 10) == before
    expect(segment(page, 10)).to_have_attribute("aria-current", "true")

    # Five pixels of the list back in the visible area, nothing else changed: the next change
    # follows.
    put_list(page, where, inside=True)
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 11) != before


# ---- AC: reduced motion ----


def test_following_is_instant_when_motion_is_reduced(live, signed_in):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    play_at(page, 0, paused=True)
    settle(page)
    before = scroll_y(page)
    # Segment 20 sits well over 500px below the first: read-mode rows are short.
    assert play_at(page, 20) > before + 500


def test_following_is_smooth_otherwise(live, signed_in):
    page = open_meeting(live, signed_in)
    freeze_clock(page)
    show_list(page)
    play_at(page, 0, paused=True)
    settle(page)
    before = scroll_y(page)
    assert play_at(page, 20) == before
    wait_for(page, "before => window.scrollY > before + 500", before)


# ---- AC: timestamp click and notes-source seek keep their behaviour ----


@BOTH_VIEWPORTS
def test_timestamp_click_still_seeks_and_plays_under_the_player_bar(live, signed_in, viewport):
    page = open_meeting(live, signed_in, viewport=viewport)
    scroll_segment_to(page, 12, clearance(segment(page, 12)))
    target = segment(page, 12)
    target.locator(".segment-timestamp").click()
    player = page.evaluate("window.__player")
    assert player["time"] == 12 * SEGMENT_SECONDS
    assert player["plays"] == 1
    expect(target).to_have_attribute("aria-current", "true")


def seek_notes_source(page, seconds):
    """Open a notes source at `seconds` the way the notes do; whether the seek went through."""
    return page.evaluate(
        """async ([meetingId, seconds]) => {
          const { meetingHistory } = await import("/assets/history.js");
          return meetingHistory.seekNotesSource({
            meetingId, snapshotId: "snapshot", segmentId: "segment", startSeconds: seconds,
          });
        }""",
        [page.locator("#meeting-audio").get_attribute("data-meeting-id"), seconds],
    )


def test_opening_a_notes_source_while_playing_leaves_the_page_on_the_source(live, signed_in):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    play_at(page, 0)
    # The reader opened a source: its card is on screen under the player, and the transcript list
    # lies below the fold.
    page.evaluate("document.querySelector('#notes-source-view').hidden = false")
    page.evaluate(NOTES_BELOW_SOURCE)
    heading = page.locator("#notes-source-heading")
    place(heading, clearance(heading))
    settle(page)
    before = scroll_y(page)
    heading_top = rect(heading)["top"]
    assert seek_notes_source(page, 60.5) is True
    expect(segment(page, 30)).to_have_attribute("aria-current", "true")
    assert scroll_y(page) == before  # the page is not pulled away from the source
    assert rect(heading)["top"] == heading_top


def test_opening_a_notes_source_while_playing_follows_when_the_list_is_in_view(live, signed_in):
    page = open_meeting(live, signed_in, reduced_motion="reduce")
    freeze_clock(page)
    show_list(page)
    play_at(page, 0)
    settle(page)
    assert seek_notes_source(page, 60.5) is True
    expect(segment(page, 30)).to_have_attribute("aria-current", "true")
    box = rect(segment(page, 30))
    assert rect(page.locator(".player-bar"))["bottom"] <= box["top"]
    assert box["bottom"] <= page.evaluate("window.innerHeight") + 1


def real_recording_page(live, signed_in, tmp_path, **options):
    meeting_id = seed_long_transcript(live.settings.data_dir, 8)
    write_recording(live.settings.data_dir, meeting_id, tmp_path, 16)
    _context, page = signed_in(**options)
    load_meeting(live, page, meeting_id, 8)
    page.wait_for_function("document.querySelector('#meeting-audio').readyState >= 1")
    return page, meeting_id


def test_notes_source_seek_moves_the_real_recording_without_playing_it(live, signed_in, tmp_path):
    page, meeting_id = real_recording_page(live, signed_in, tmp_path)
    audio = page.locator("#meeting-audio")
    sought = page.evaluate(
        """async meetingId => {
          const { meetingHistory } = await import("/assets/history.js");
          return meetingHistory.seekNotesSource({
            meetingId, snapshotId: "snapshot", segmentId: "segment", startSeconds: 6.5,
          });
        }""",
        meeting_id,
    )
    assert sought is True
    assert audio.evaluate("element => Math.abs(element.currentTime - 6.5) < 0.1")
    assert audio.evaluate("element => element.paused")
    expect(segment(page, 3)).to_have_attribute("aria-current", "true")


def test_real_playback_scrolls_the_next_segment_into_view(live, signed_in, tmp_path):
    page, _meeting_id = real_recording_page(
        live, signed_in, tmp_path, viewport={"width": 1280, "height": 500}, reduced_motion="reduce"
    )
    page.clock.install(time=FIXED_NOW)
    # Segment 2 sits at the foot of the viewport, so segment 3 is below the fold.
    place(segment(page, 2), 500, edge="bottom")
    assert rect(segment(page, 3))["top"] >= 500
    settle(page)

    segment(page, 2).locator(".segment-timestamp").click()
    wait_for(page, "!document.querySelector('#meeting-audio').paused")
    assert page.evaluate("document.querySelector('#meeting-audio').currentTime") >= 3.9
    page.evaluate("document.querySelector('#meeting-audio').currentTime = 5.8")
    expect(segment(page, 3)).to_have_attribute("aria-current", "true", timeout=5_000)
    box = rect(segment(page, 3))
    assert rect(page.locator(".player-bar"))["bottom"] <= box["top"]
    assert box["bottom"] <= 500 + 1


def test_real_playback_leaves_a_reader_of_the_notes_alone(live, signed_in, tmp_path):
    page, _meeting_id = real_recording_page(
        live, signed_in, tmp_path, viewport={"width": 1280, "height": 500}, reduced_motion="reduce"
    )
    page.clock.install(time=FIXED_NOW)
    place(segment(page, 2), 500, edge="bottom")
    settle(page)
    segment(page, 2).locator(".segment-timestamp").click()
    wait_for(page, "!document.querySelector('#meeting-audio').paused")

    # The reader goes back to the notes while the recording plays on, and the quiet window passes.
    scroll_to(page, 0)
    assert rect(page.locator("#transcript-segments"))["top"] >= 500
    settle(page)
    # Two segment changes later, the second well after that window, the page has not moved.
    expect(segment(page, 4)).to_have_attribute("aria-current", "true", timeout=8_000)
    assert scroll_y(page) == 0


# ---- AC: 360 px, both themes, keyboard focus ----


@pytest.mark.parametrize("viewport", [NARROW, MINIMUM], ids=["360", "320"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_player_bar_fits_narrow_screens_and_is_an_opaque_themed_surface(
    live, signed_in, scheme, viewport
):
    page = open_meeting(live, signed_in, viewport=viewport, color_scheme=scheme)
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    overflow = "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    assert page.evaluate(overflow) <= 0
    scroll_to(page, 3000)
    assert page.evaluate(overflow) <= 0
    assert_pinned(page, viewport)

    colors = page.evaluate(
        """() => {
          const resolve = (token) => {
            const probe = document.createElement("span");
            probe.style.color = `var(${token})`;
            document.body.append(probe);
            const color = getComputedStyle(probe).color;
            probe.remove();
            return color;
          };
          const bar = getComputedStyle(document.querySelector(".player-bar"));
          return {
            background: bar.backgroundColor,
            border: bar.borderTopColor,
            borderWidth: bar.borderTopWidth,
            surface: resolve("--surface"),
            hairline: resolve("--hairline"),
          };
        }"""
    )
    assert colors["background"] == colors["surface"]
    assert colors["border"] == colors["hairline"]
    assert colors["borderWidth"] == "1px"
    audio = rect(page.locator("#meeting-audio"))
    bar = rect(page.locator(".player-bar"))
    assert bar["left"] <= audio["left"] and audio["right"] <= bar["right"]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_player_shows_a_keyboard_focus_ring(live, signed_in, scheme):
    page = open_meeting(live, signed_in, color_scheme=scheme)
    page.locator("#meeting-actions").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "meeting-tab-transcript"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "meeting-audio"
    scroll_to(page, 1500)  # the focus stays on the player while it is stuck at the top edge
    bar = rect(page.locator(".player-bar"))
    clip = {
        "x": 0,
        "y": 0,
        "width": page.evaluate("window.innerWidth"),
        "height": bar["bottom"] + 8,
    }

    def strip():
        return page.screenshot(clip=clip, animations="disabled", caret="hide")

    focused = strip()
    ring = page.locator("#meeting-audio").evaluate(
        """element => {
          const style = getComputedStyle(element);
          const probe = document.createElement("span");
          probe.style.color = "var(--accent)";
          document.body.append(probe);
          const accent = getComputedStyle(probe).color;
          probe.remove();
          const box = element.getBoundingClientRect();
          const reach = parseFloat(style.outlineOffset) + parseFloat(style.outlineWidth);
          return {
            style: style.outlineStyle,
            width: parseFloat(style.outlineWidth),
            color: style.outlineColor,
            accent,
            top: box.top - reach,
            bottom: box.bottom + reach,
            left: box.left - reach,
            right: box.right + reach,
          };
        }"""
    )
    assert ring["style"] == "solid" and ring["width"] >= 2, ring
    assert ring["color"] == ring["accent"], ring
    # The ring is not clipped by the bar or by the top edge of the viewport.
    assert ring["top"] >= max(bar["top"], 0) and ring["bottom"] <= bar["bottom"], (ring, bar)
    assert ring["left"] >= bar["left"] and ring["right"] <= bar["right"], (ring, bar)
    page.evaluate("document.activeElement.blur()")
    assert strip() != focused  # and it is really painted


@pytest.mark.parametrize("target_kind", ["source-id", "source-time", "seek", "native-seek"])
@pytest.mark.parametrize("generation_kind", ["current", "past"])
def test_filtered_source_and_saved_seek_reveal_real_row(
    live, signed_in, target_kind, generation_kind, tmp_path
):
    options = {"viewport": MINIMUM, "color_scheme": "dark"} if target_kind == "source-id" else {}
    page = open_meeting(live, signed_in, **options)
    if generation_kind == "past":
        show_a_past_generation(page)
        page.reload(wait_until="networkidle")
        page.locator("#generation-selector").select_option("browser-past-generation")
        page.evaluate(STUB_AUDIO)
    target = page.locator(".transcript-segment").nth(30)
    target_id = target.get_attribute("data-segment-id")
    page.locator("#transcript-search").fill("no matching transcript")
    page.locator("#transcript-review-only").check()
    expect(target).to_be_hidden()
    if target_kind == "seek":
        assert seek_notes_source(page, 60.5) is True
        assert page.evaluate("window.__player.time") == 60.5
    elif target_kind == "native-seek":
        page.evaluate("""() => {
          const audio = document.querySelector('#meeting-audio');
          audio.currentTime = 60.5;
          audio.dispatchEvent(new Event('seeked'));
        }""")
    else:
        found = page.evaluate(
            """async ([id, seconds]) => {
              const { meetingHistory } = await import('/assets/history.js');
              return meetingHistory.markNotesSource(id, seconds);
            }""",
            [target_id if target_kind == "source-id" else "older-source-segment", 60.5],
        )
        assert found is True
    control = page.locator("#transcript-reveal-target")
    expect(control).to_be_visible()
    expect(control).to_have_attribute("data-segment-id", target_id)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshots = Path(os.environ.get("ASR_ISSUE_257_SCREENSHOT_DIR", tmp_path))
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=screenshots / f"hidden-{generation_kind}-{target_kind}.png", full_page=True
    )
    control.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#transcript-search")).to_have_value("")
    expect(target).to_be_visible()
    expect(page.locator("#transcript-review-only")).not_to_be_checked()
    expect(target.locator(".segment-timestamp")).to_be_focused()
    expect(control).to_be_hidden()
    if target_kind in {"seek", "native-seek"}:
        expect(target).to_have_attribute("aria-current", "true")
    else:
        expect(target).to_have_class(re.compile(r"(?:^|\s)is-notes-source(?:\s|$)"))
        expect(target.locator(".segment-source-mark")).to_have_text("Nguồn của mục notes")


def test_stable_playback_samples_do_not_repaint_transcript_filters(live, signed_in):
    page = open_meeting(live, signed_in)
    page.locator("#transcript-search").fill("Segment 30")
    counts = page.evaluate(
        """() => {
          const list = document.querySelector('#transcript-segments');
          const observer = new MutationObserver(() => {});
          observer.observe(list, { attributes: true, subtree: true,
            attributeFilter: ['hidden', 'data-filter-key'] });
          for (let index = 0; index < 20; index++) {
            window.__player.time = 60.5;
            document.querySelector('#meeting-audio').dispatchEvent(new Event('timeupdate'));
          }
          const records = observer.takeRecords();
          observer.disconnect();
          return records.length;
        }"""
    )
    assert counts == 0
    expect(page.locator('.transcript-segment[aria-current="true"]')).to_have_count(1)
    expect(page.locator(".transcript-segment:visible")).to_have_count(1)


def test_filtered_seek_keeps_exact_source_id_when_segments_overlap(live, signed_in):
    page = open_meeting(live, signed_in)
    meeting_id = page.locator("#meeting-audio").get_attribute("data-meeting-id")

    def overlap(route):
        response = route.fetch()
        payload = response.json()
        # Two speakers overlap at the saved source timestamp. The source names the later row.
        payload["transcription"]["generation"]["segments"][29]["start_seconds"] = 60
        payload["transcription"]["generation"]["segments"][29]["end_seconds"] = 62
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", overlap)
    page.reload(wait_until="networkidle")
    page.evaluate(STUB_AUDIO)
    target = page.locator(".transcript-segment").nth(30)
    target_id = target.get_attribute("data-segment-id")
    page.locator("#transcript-search").fill("no matching source")
    assert page.evaluate(
        """async ([meetingId, segmentId]) => {
          const { meetingHistory } = await import('/assets/history.js');
          return meetingHistory.seekNotesSource({
            meetingId, snapshotId: 'saved', segmentId, startSeconds: 60.5,
          });
        }""",
        [meeting_id, target_id],
    )
    expect(page.locator("#transcript-reveal-target")).to_have_attribute(
        "data-segment-id", target_id
    )
    # The native seeked event that follows currentTime must not replace the chosen overlapping ID.
    page.locator("#meeting-audio").dispatch_event("seeking")
    page.locator("#meeting-audio").dispatch_event("seeked")
    expect(page.locator("#transcript-reveal-target")).to_have_attribute(
        "data-segment-id", target_id
    )
    page.locator("#transcript-reveal-target").click()
    expect(target.locator(".segment-timestamp")).to_be_focused()


@pytest.mark.parametrize("older_mark", [False, True], ids=["no-older-mark", "older-notes-mark"])
@pytest.mark.parametrize(
    "scheme,width",
    [("light", 1280), ("dark", 1280), ("light", 320), ("dark", 320)],
    ids=["desktop-light", "desktop-dark", "phone-light", "phone-dark"],
)
def test_filtered_timestamp_click_keeps_exact_overlapping_target(
    live, signed_in, older_mark, scheme, width, tmp_path
):
    page = open_meeting(
        live, signed_in, viewport={"width": width, "height": 844}, color_scheme=scheme
    )
    meeting_id = page.locator("#meeting-audio").get_attribute("data-meeting-id")

    def overlap(route):
        response = route.fetch()
        payload = response.json()
        payload["transcription"]["generation"]["segments"][29]["start_seconds"] = 60
        payload["transcription"]["generation"]["segments"][29]["end_seconds"] = 62
        route.fulfill(response=response, json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", overlap)
    page.reload(wait_until="networkidle")
    page.evaluate(STUB_AUDIO)
    target = page.locator(".transcript-segment").nth(30)
    target_id = target.get_attribute("data-segment-id")
    older = page.locator(".transcript-segment").nth(29)
    older_id = older.get_attribute("data-segment-id")
    if older_mark:
        page.evaluate(
            """async ([meetingId, segmentId]) => {
              const { meetingHistory } = await import('/assets/history.js');
              return meetingHistory.markNotesSource(segmentId, 60.5);
            }""",
            [meeting_id, older_id],
        )
        expect(older.locator(".segment-source-mark")).to_be_visible()

    target.locator(".segment-timestamp").click()
    page.locator("#meeting-audio").dispatch_event("seeking")
    page.locator("#meeting-audio").dispatch_event("seeked")
    page.locator("#transcript-search").fill("no matching source")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", target_id)
    screenshots = Path(os.environ.get("ASR_ISSUE_257_SCREENSHOT_DIR", tmp_path))
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=screenshots / f"hidden-target-{width}-{scheme}.png", full_page=True)
    reveal.focus()
    reveal.press("Enter")
    expect(target).to_be_visible()
    expect(target.locator(".segment-timestamp")).to_be_focused()
    if older_mark:
        expect(older.locator(".segment-source-mark")).to_be_visible()

    # Once the matching native acknowledgement is consumed, a later time-only seek resolves by time.
    later = page.locator(".transcript-segment").nth(4)
    later_id = later.get_attribute("data-segment-id")
    page.evaluate("window.__player.time = 8.5")
    page.locator("#meeting-audio").dispatch_event("seeked")
    expect(page.locator("#transcript-reveal-target")).to_have_attribute("data-segment-id", later_id)


@pytest.mark.parametrize("older_mark", [False, True], ids=["no-older-mark", "older-notes-mark"])
def test_playing_timestamp_click_keeps_target_through_delayed_trusted_seeked(
    live, signed_in, tmp_path, older_mark
):
    meeting_id = seed_long_transcript(live.settings.data_dir, count=8)
    write_recording(live.settings.data_dir, meeting_id, tmp_path, 16)
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_segments SET start_seconds = 8, end_seconds = 10 "
            "WHERE sequence = 3 AND generation_id = "
            "(SELECT current_generation_id FROM meetings WHERE id = ?)",
            (meeting_id,),
        )
    _, page = signed_in(viewport={"width": 360, "height": 800}, color_scheme="dark")
    load_meeting(live, page, meeting_id, 8)
    page.wait_for_function("() => document.querySelector('#meeting-audio').readyState >= 1")
    earlier = page.locator(".transcript-segment").nth(3)
    target = page.locator(".transcript-segment").nth(4)
    earlier_id = earlier.get_attribute("data-segment-id")
    target_id = target.get_attribute("data-segment-id")
    assert (
        target.get_attribute("data-start-seconds")
        == earlier.get_attribute("data-start-seconds")
        == "8"
    )
    if older_mark:
        assert page.evaluate(
            """async segmentId => {
              const { meetingHistory } = await import('/assets/history.js');
              return meetingHistory.markNotesSource(segmentId, 8);
            }""",
            earlier_id,
        )
    page.locator("#meeting-audio").evaluate("async node => { await node.play(); }")
    page.locator(".transcript-segments").evaluate(
        """node => node.addEventListener('click', () => {
          const end = performance.now() + 250;
          while (performance.now() < end) {}
        }, {once: true})"""
    )
    target.locator(".segment-timestamp").click()
    page.wait_for_function("() => document.querySelector('#meeting-audio').currentTime >= 8")
    page.locator("#meeting-audio").evaluate("node => node.pause()")
    page.locator("#transcript-search").fill("no matching delayed target")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", target_id)
    if older_mark:
        expect(earlier).to_have_class(re.compile(r"(?:^|\s)is-notes-source(?:\s|$)"))


def test_new_player_seek_supersedes_outstanding_row_seek(live, signed_in, tmp_path):
    page, _ = real_recording_page(live, signed_in, tmp_path, reduced_motion="reduce")
    row = page.locator(".transcript-segment").nth(4)
    new_target = page.locator(".transcript-segment").nth(5)
    target_id = new_target.get_attribute("data-segment-id")
    audio = page.locator("#meeting-audio")
    audio.evaluate("""node => {
      window.actualSeekTrace = [];
      for (const type of ['seeking', 'seeked']) {
        node.addEventListener(type, event => window.actualSeekTrace.push([
          type, event.isTrusted, node.currentTime,
        ]));
      }
      node.addEventListener('seeking', () => { node.currentTime = 10.5; }, {once: true});
    }""")
    row.locator(".segment-timestamp").click()
    page.wait_for_function(
        "() => window.actualSeekTrace.some("
        "([type, trusted, time]) => type === 'seeked' && trusted && time >= 10.5)"
    )
    audio.evaluate("node => node.pause()")
    page.locator("#transcript-search").fill("no matching sources")
    expect(page.locator("#transcript-reveal-target")).to_have_attribute(
        "data-segment-id", target_id
    )


def test_two_timestamp_writes_before_metadata_keep_first_later_time_only_seek(
    live, signed_in, tmp_path
):
    meeting_id = seed_long_transcript(live.settings.data_dir, count=8)
    write_recording(live.settings.data_dir, meeting_id, tmp_path, 16)
    _, page = signed_in(reduced_motion="reduce")
    held = []
    audio_url = f"{live.url}/api/meetings/{meeting_id}/audio"
    page.route(audio_url, lambda route: held.append(route))
    page.set_default_timeout(10_000)
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript", wait_until="domcontentloaded")
    audio = page.locator("#meeting-audio")
    rows = page.locator(".transcript-segment")
    expect(rows).to_have_count(8)
    page.locator("#meeting-tab-transcript").click()
    events = audio.evaluate(
        """node => {
          window.loadingSeekEvents = [];
          for (const type of ['seeking', 'seeked']) {
            node.addEventListener(type, event => window.loadingSeekEvents.push([
              event.type, event.isTrusted, node.currentTime,
            ]));
          }
        }"""
    )
    assert events is None
    assert audio.evaluate("node => node.readyState") == 0
    first = rows.nth(1)
    target = rows.nth(4)
    later = rows.nth(5)
    first.locator(".segment-timestamp").click()
    target.locator(".segment-timestamp").click()
    assert audio.evaluate("node => node.readyState") == 0
    assert held
    for route in held[:]:
        route.continue_()
    page.unroute(audio_url)
    page.wait_for_function(
        "() => window.loadingSeekEvents.some(([type, trusted, time]) => "
        "type === 'seeked' && trusted && time >= 8 && time < 8.7)"
    )
    audio.evaluate("node => node.pause()")
    page.locator("#transcript-search").fill("No matching local rows")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", target.get_attribute("data-segment-id"))
    audio.evaluate("node => { node.currentTime = 10.5; }")
    page.wait_for_function(
        "() => window.loadingSeekEvents.some(([type, trusted, time]) => "
        "type === 'seeked' && trusted && time >= 10.5 && time < 11.2)"
    )
    expect(reveal).to_have_attribute("data-segment-id", later.get_attribute("data-segment-id"))


def test_delayed_saved_source_card_does_not_override_newer_timestamp_seek(
    live, signed_in, tmp_path
):
    meeting_id, _, snapshot, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(reduced_motion="reduce")
    open_notes(live, page, meeting_id)
    audio = page.locator("#meeting-audio")
    audio.evaluate(
        """node => {
          window.loadingSeekEvents = [];
          for (const type of ['seeking', 'seeked']) {
            node.addEventListener(type, event => window.loadingSeekEvents.push([
              event.type, event.isTrusted, node.currentTime,
            ]));
          }
        }"""
    )
    snapshot_url = f"{live.url}/api/notes-sources/{snapshot['id']}"
    held = []
    page.route(snapshot_url, lambda route: held.append(route))
    with page.expect_request(snapshot_url):
        page.locator(CHIP).filter(has_text="00:25").click()
    target = page.locator(f'.transcript-segment[data-segment-id="{segment_ids[20]}"]')
    target.locator(".segment-timestamp").click()
    page.wait_for_function(
        "() => window.loadingSeekEvents.some(([type, trusted, time]) => "
        "type === 'seeked' && trusted && time >= 20 && time < 20.7)"
    )
    audio.evaluate("node => node.pause()")
    assert held
    for route in held[:]:
        route.continue_()
    page.unroute(snapshot_url)
    card = page.locator("#notes-source-view")
    expect(card).to_be_visible()
    expect(page.locator("#notes-source-time")).to_have_text("00:00:25")
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][25]["text"])
    page.locator("#meeting-tab-transcript").click()
    page.locator("#transcript-search").fill("No matching local rows")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[20])
    page.locator("#notes-source-close").click()
    expect(card).to_be_hidden()
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[20])
    assert audio.evaluate(
        "node => node.paused && node.currentTime >= 19.9 && node.currentTime < 20.7"
    )


def test_new_notes_source_after_native_seek_start_keeps_its_reveal(live, signed_in, tmp_path):
    meeting_id, _, snapshot, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(reduced_motion="reduce")
    open_notes(live, page, meeting_id)
    page.wait_for_function("() => document.querySelector('#meeting-audio').readyState >= 1")
    chip = page.locator(CHIP).filter(has_text="00:25")
    expect(chip).to_be_visible()
    page.locator("#meeting-tab-transcript").click()
    held = []
    snapshot_url = f"{live.url}/api/notes-sources/{snapshot['id']}"
    page.route(snapshot_url, lambda route: held.append(route))
    with page.expect_request(snapshot_url):
        chip.evaluate(
            """chip => {
              const audio = document.querySelector('#meeting-audio');
              window.reverseSourceTrace = [];
              audio.addEventListener('seeking', () => {
                window.reverseSourceTrace.push(['seeking', audio.currentTime]);
                document.querySelector('#meeting-tab-notes').click();
                Array.from(document.querySelectorAll('#notes-preview a'))
                  .find(link => link.textContent.includes('00:25')).click();
              }, {once: true});
              audio.addEventListener('seeked', () => {
                window.reverseSourceTrace.push(['seeked', audio.currentTime]);
              }, {once: true});
              audio.currentTime = 20;
            }"""
        )
    page.wait_for_function("() => window.reverseSourceTrace.some(([type]) => type === 'seeked')")
    assert held
    held[0].continue_()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][25]["text"])
    page.locator("#meeting-tab-transcript").click()
    page.locator("#transcript-search").fill("No such local row")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[25])


def test_closing_older_notes_source_keeps_newer_hidden_seek_recovery(live, signed_in, tmp_path):
    meeting_id, _, _, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(viewport={"width": 1280, "height": 844})
    open_notes(live, page, meeting_id)
    page.locator(CHIP).filter(has_text="00:25").click()
    page.wait_for_function(
        "() => document.querySelector('#meeting-audio').currentTime >= 24.7 "
        "&& !document.querySelector('#meeting-audio').seeking"
    )
    page.locator("#meeting-audio").evaluate("node => node.pause()")
    target = page.locator(f'.transcript-segment[data-segment-id="{segment_ids[20]}"]')
    target.locator(".segment-timestamp").click()
    page.wait_for_function(
        "() => !document.querySelector('#meeting-audio').seeking "
        "&& document.querySelector('#meeting-audio').currentTime < 22"
    )
    page.locator("#transcript-search").fill("no result random")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[20])
    page.locator("#notes-source-close").click()
    expect(reveal).to_be_visible()
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[20])


def test_closing_notes_source_clears_source_owned_hidden_reveal(live, signed_in, tmp_path):
    meeting_id, _, _, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(viewport={"width": 1280, "height": 844})
    open_notes(live, page, meeting_id)
    page.locator(CHIP).filter(has_text="00:25").click()
    page.wait_for_function(
        "() => document.querySelector('#meeting-audio').currentTime >= 24.7 "
        "&& !document.querySelector('#meeting-audio').seeking"
    )
    page.locator("#meeting-audio").evaluate("node => node.pause()")
    page.locator("#transcript-search").fill("no source row")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[25])
    page.locator("#notes-source-close").click()
    expect(reveal).to_be_hidden()
    expect(reveal).to_have_attribute("data-segment-id", "")


def test_closing_notes_during_trusted_source_seeking_dismisses_reveal(live, signed_in, tmp_path):
    meeting_id, _, _, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(viewport={"width": 1280, "height": 844})
    open_notes(live, page, meeting_id)
    page.locator("#meeting-audio").evaluate("""node => {
      window.sourceSeekTrace = [];
      node.addEventListener('seeking', () => {
        window.sourceSeekTrace.push('seeking');
        document.querySelector('#notes-source-close')?.click();
      }, {once: true});
      node.addEventListener('seeked', () => window.sourceSeekTrace.push('seeked'));
    }""")
    page.locator(CHIP).filter(has_text="00:25").click()
    page.wait_for_function(
        "() => window.sourceSeekTrace.includes('seeking') "
        "&& window.sourceSeekTrace.includes('seeked')"
    )
    page.locator("#meeting-audio").evaluate("node => node.pause()")
    page.locator("#transcript-search").fill("no source row")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_be_hidden()
    expect(reveal).to_have_attribute("data-segment-id", "")
    assert segment_ids[25]


def test_newer_timestamp_wins_when_notes_seek_is_waiting_for_metadata(live, signed_in, tmp_path):
    meeting_id, _, _, segment_ids = seed_long_cited_notes(live, tmp_path)
    _, page = signed_in(viewport={"width": 1280, "height": 844})
    open_notes(live, page, meeting_id)
    page.evaluate(
        """() => Object.defineProperty(document.querySelector('#meeting-audio'), 'readyState', {
          configurable: true, get: () => 0,
        })"""
    )
    page.locator(CHIP).filter(has_text="00:25").click()
    page.locator(
        f'.transcript-segment[data-segment-id="{segment_ids[20]}"] .segment-timestamp'
    ).click()
    page.locator("#notes-source-close").click()
    page.evaluate(
        """() => {
          const audio = document.querySelector('#meeting-audio');
          delete audio.readyState;
          audio.dispatchEvent(new Event('loadedmetadata'));
        }"""
    )
    page.locator("#transcript-search").fill("nothing matches this")
    reveal = page.locator("#transcript-reveal-target")
    expect(reveal).to_have_attribute("data-segment-id", segment_ids[20])
