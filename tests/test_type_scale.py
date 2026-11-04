"""One readable type scale: rem tokens, a 12px floor and 16px reading prose (#156)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_history import _publish_browser_generation
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes import _seed_saved_notes

WEB = Path(__file__).resolve().parents[1] / "src/asr_studio/web"

VISIBLE_TEXT_JS = """() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const sizes = {};
  while (walker.nextNode()) {
    const element = walker.currentNode.parentElement;
    if (!walker.currentNode.textContent.trim() || !element.getClientRects().length) continue;
    const style = getComputedStyle(element);
    if (style.visibility === "hidden") continue;
    const size = parseFloat(style.fontSize);
    (sizes[size] ||= new Set()).add(element.tagName + "." + element.className);
  }
  return Object.fromEntries(Object.entries(sizes).map(([size, set]) => [size, [...set]]));
}"""

SMALL_CONTROLS_JS = """(minimum) =>
  [...document.querySelectorAll(
    "button, select, textarea, [role=tab], input:not([type=radio], [type=checkbox], [type=file])"
  )]
    .filter((element) => element.getClientRects().length
      && parseFloat(getComputedStyle(element).fontSize) < minimum)
    .map((element) => element.tagName + "." + element.className)"""

# Text that no longer fits its box, in either axis, whether it is clipped or spills.
# A native select is exempt: it truncates a long value and opens the whole option.
CUT_TEXT_JS = """() => [...document.querySelectorAll("body *")]
  .filter((element) => {
    const text = element.value || [...element.childNodes].some(
      (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (!text || !element.getClientRects().length) return false;
    const style = getComputedStyle(element);
    if (style.display === "inline" || style.visibility === "hidden") return false;
    if (element.matches("select")) return false;
    if (style.textOverflow === "ellipsis" || style.clipPath !== "none") return false;
    return element.scrollWidth - element.clientWidth > 1
      || element.scrollHeight - element.clientHeight > 2;
  })
  .map((element) => element.tagName + "." + element.className + " " + element.textContent.trim())"""


# A large-calendar day whose meeting count sits on top of its number.
CROWDED_DAYS_JS = """() => {
  const days = [...document.querySelectorAll("#large-calendar .cal-day.has-meetings")];
  if (!days.length) return ["no seeded day on screen"];
  return days.filter((day) => {
    const number = day.querySelector(".cal-number").getBoundingClientRect();
    const count = day.querySelector(".cal-count").getBoundingClientRect();
    return number.left < count.right && count.left < number.right
      && number.top < count.bottom && count.top < number.bottom;
  }).map((day) => day.dataset.date);
}"""


def _screens(live, page, meeting_id):
    """Yield once per screen a person reads, with that screen open."""
    page.goto(f"{live.url}/")
    expect(page.locator(".header-create")).to_be_visible()
    yield "home"
    page.locator(".header-create").click()
    expect(page.locator("dialog[open]")).to_be_visible()
    yield "new meeting dialog"
    page.locator("#close-dialog").click()
    page.locator("#open-calendar:visible, #mini-calendar .cal-undated:visible").first.click()
    expect(page.locator("#large-calendar .cal-day").first).to_be_visible()
    yield "calendar"
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    yield "notes tab"
    page.locator("#meeting-tab-transcript").click()
    expect(page.locator(".segment-text").first).to_be_visible()
    yield "transcript tab"
    page.locator(".connection-launchers button").first.click()
    expect(page.locator("dialog[open]")).to_be_visible()
    yield "connections dialog"


def test_stylesheets_take_every_font_size_from_the_rem_scale():
    declared = {
        sheet.name: re.findall(r"font-size:\s*([^;]+);", sheet.read_text())
        for sheet in WEB.glob("*.css")
    }
    assert sum(map(len, declared.values())) > 100
    assert {name: [v for v in values if "px" in v] for name, values in declared.items()} == {
        name: [] for name in declared
    }

    tokens = dict(
        re.findall(r"--(text-[a-z-]+):\s*([\d.]+)rem;", (WEB / "design-system.css").read_text())
    )
    assert min(map(float, tokens.values())) == 0.75
    used = {
        name
        for values in declared.values()
        for v in values
        for name in re.findall(r"--(text-[a-z-]+)", v)
    }
    assert used == set(tokens)


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
def test_every_screen_keeps_the_type_floor(live, signed_in, width):
    context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    for screen in _screens(live, page, meeting_id):
        sizes = page.evaluate(VISIBLE_TEXT_JS)
        assert {s: e for s, e in sizes.items() if float(s) < 12} == {}, screen
        assert page.evaluate(SMALL_CONTROLS_JS, 14) == [], screen

    context.clear_cookies()
    page.goto(f"{live.url}/")
    expect(page.locator(".access-header")).to_be_visible()
    sizes = page.evaluate(VISIBLE_TEXT_JS)
    assert {s: e for s, e in sizes.items() if float(s) < 12} == {}, "access"
    assert page.evaluate(SMALL_CONTROLS_JS, 14) == [], "access"


@pytest.mark.browser
def test_reading_prose_is_16px_and_the_markdown_editor_15px(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    size = "element => parseFloat(getComputedStyle(element).fontSize)"
    prose = page.locator("#notes-preview").locator("p, li")
    expect(prose.first).to_be_visible(timeout=10_000)
    assert min(prose.evaluate_all(f"items => items.map({size})")) >= 16
    page.locator("#notes-mode-write").click()
    assert page.locator("#notes-markdown").evaluate(size) >= 15
    page.locator("#meeting-tab-transcript").click()
    assert page.locator(".segment-text").first.evaluate(size) >= 16


@pytest.mark.browser
def test_browser_font_preference_scales_the_interface(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    page.locator("#meeting-tab-transcript").click()
    text = page.locator(".segment-text").first
    expect(text).to_be_visible(timeout=10_000)
    size = "element => parseFloat(getComputedStyle(element).fontSize)"
    assert text.evaluate(size) == 16

    page.evaluate("document.documentElement.style.fontSize = '20px'")
    assert text.evaluate(size) == 20
    assert min(map(float, page.evaluate(VISIBLE_TEXT_JS))) >= 15
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0


@pytest.mark.browser
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("width", [1440, 1024, 768, 681, 390, 360, 320])
def test_larger_type_does_not_break_any_layout(live, signed_in, width, language):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.evaluate("language => localStorage.setItem('asr-studio:lang', language)", language)
    for screen in _screens(live, page, meeting_id):
        where = f"{screen} {width} {language}"
        assert page.evaluate("document.documentElement.lang") == language, where
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0, where
        assert page.evaluate(CUT_TEXT_JS) == [], where
        if screen == "calendar":
            assert page.evaluate(CROWDED_DAYS_JS) == [], where


@pytest.mark.browser
@pytest.mark.parametrize("size", [(1366, 768), (1024, 700)])
def test_the_rail_footer_covers_the_scrolling_calendar_to_the_viewport_edge(live, signed_in, size):
    for title in ("Alpha", "Beta", "Gamma"):
        seed_meeting(live.settings.data_dir, title, "2026-10-14")
    _context, page = signed_in(viewport={"width": size[0], "height": size[1]})
    page.goto(f"{live.url}/")
    expect(page.locator("#recent-list .recent-link")).to_have_count(3)
    rail = "document.querySelector('.sidebar')"
    assert page.evaluate(f"{rail}.scrollHeight > {rail}.clientHeight")
    bottom = page.locator(".sidebar-footer").evaluate("el => el.getBoundingClientRect().bottom")
    assert bottom >= size[1] - 0.5


@pytest.mark.browser
@pytest.mark.parametrize("width", [360, 320])
def test_long_speaker_names_fit_the_narrow_transcript_forms(live, signed_in, width):
    name = "Alexandria Montgomery-Featherstonehaugh"
    meeting_id = seed_meeting(live.settings.data_dir, "Long", "2026-10-14", duration_seconds=4000)
    _publish_browser_generation(
        live,
        meeting_id,
        segments=[
            {
                "id": "a" * 32,
                "start_seconds": 3662.5,
                "end_seconds": 3670,
                "text": "An hour into the meeting.",
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
            }
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": name,
                "first_seen_seconds": 3662.5,
                "last_seen_seconds": 3670,
            }
        ],
    )
    _context, page = signed_in(viewport={"width": width, "height": 800})
    page.evaluate("localStorage.setItem('asr-studio:lang', 'en')")
    page.goto(f"{live.url}/meetings/{meeting_id}")
    page.locator("#meeting-tab-transcript").click()
    overflow = "document.documentElement.scrollWidth - innerWidth"

    page.locator(".segment-edit-button").first.click()
    select = page.locator(".segment-speaker-select").first
    expect(select).to_be_visible()
    assert page.evaluate(overflow) <= 0
    row = select.locator("xpath=ancestor::*[contains(@class, 'segment-')][last()]")
    assert select.bounding_box()["x"] + select.bounding_box()["width"] <= (
        row.bounding_box()["x"] + row.bounding_box()["width"] + 0.5
    )
    page.keyboard.press("Escape")

    page.locator(".speaker-chip").first.click()
    form = page.locator(".speaker-name-form")
    expect(form).to_be_visible()
    field, save = form.locator("input").first, form.locator("button[type=submit]")
    assert field.bounding_box()["width"] >= 120
    field_box, save_box = field.bounding_box(), save.bounding_box()
    assert (
        field_box["y"] + field_box["height"] <= save_box["y"]
        or field_box["x"] + field_box["width"] <= save_box["x"]
    )
    assert page.evaluate(overflow) <= 0
