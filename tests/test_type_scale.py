"""One readable type scale: rem tokens, a 12px floor and 16px reading prose (#156)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_connections import (
    MockProviderApi,
    browser_runtime,  # noqa: F401  (fixture)
    open_connection_dialog,
    signed_in_page,
)
from test_browser_history import _publish_browser_generation, save_over_another_clients_edit
from test_browser_history import browser_context as browser_context
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes import _seed_saved_notes
from test_browser_notes_citations import (
    CHIP,
    _write_test_recording,
    open_notes,
    seed_cited_notes,
    seed_long_cited_notes,
)

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
    "button, summary, select, textarea, [role=tab], "
    + "input:not([type=radio], [type=checkbox], [type=file])"
  )]
    .filter((element) => element.getClientRects().length
      && parseFloat(getComputedStyle(element).fontSize) < minimum)
    .map((element) => element.tagName + "." + element.className)"""

FONT_SIZES_JS = "items => items.map(e => parseFloat(getComputedStyle(e).fontSize))"

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
def test_the_conflict_reload_button_is_a_control_size(live, browser_context):
    """State-only: it exists only after a save meets another client's edit (a real 409)."""
    context, page = browser_context
    save_over_another_clients_edit(context, live, page, "My draft, typed before the other save.")
    reload_button = page.locator("[data-reload-latest]")
    expect(reload_button).to_be_visible()
    assert reload_button.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") >= 14


@pytest.mark.browser
def test_the_conflict_panel_values_are_reading_size_and_chi_tiet_is_a_control_size(
    live, browser_context
):
    """State-only: the draft and the other client's value exist after a real 409."""
    context, page = browser_context
    save_over_another_clients_edit(context, live, page, "My draft, typed before the other save.")
    disclosure = page.locator("details.segment-details summary:visible")
    assert disclosure.count() > 0
    assert min(disclosure.evaluate_all(FONT_SIZES_JS)) >= 14

    # Reopened after the 409, the row reviews the draft against the latest saved text.
    first = page.locator(".transcript-segment").nth(0)
    first.locator(".segment-text-editor").press("Escape")
    first.locator(".segment-edit-button").click()
    values = first.locator(".transcript-draft-review p:visible")
    expect(values).to_have_count(2)
    assert min(values.evaluate_all(FONT_SIZES_JS)) >= 16


@pytest.mark.browser
@pytest.mark.parametrize("width", [1280, 390, 360])
def test_citation_chips_including_range_chips_are_a_control_size(live, signed_in, tmp_path, width):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, *_ = seed_cited_notes(live.settings.data_dir, segment_starts=[1.0, 2.0])
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    open_notes(live, page, meeting_id)
    expect(page.locator(CHIP).filter(has_text="2 đoạn")).to_be_visible()  # a real range chip
    assert page.locator(CHIP).count() >= 2
    assert min(page.locator(CHIP).evaluate_all(FONT_SIZES_JS)) >= 14
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0
    assert page.evaluate(CUT_TEXT_JS) == []


@pytest.mark.browser
@pytest.mark.parametrize("width", [1280, 390, 320])
def test_the_chatgpt_usage_link_is_a_control_size(browser_runtime, width):  # noqa: F811
    live, browser = browser_runtime
    api = MockProviderApi()
    api.status = "connected"
    context, page, _ = signed_in_page(
        browser, live, api=api, viewport={"width": width, "height": 900}
    )
    open_connection_dialog(page)
    link = page.locator(".connection-usage-link")
    link.scroll_into_view_if_needed()
    expect(link).to_be_visible()
    assert link.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") >= 14
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0
    context.close()


@pytest.mark.browser
def test_the_saved_source_text_opened_from_a_citation_is_reading_size(live, signed_in, tmp_path):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    meeting_id, *_ = seed_long_cited_notes(live, tmp_path)
    open_notes(live, page, meeting_id)
    page.locator(CHIP).first.click()
    source = page.locator("#notes-source-text")
    expect(source).to_be_visible()
    assert source.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") >= 16


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


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 1280, 1024, 760, 390, 360, 320])
def test_transcript_controls_share_rows_before_stacking(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir, title="Transcript controls")
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript")
    page.locator("#meeting-tab-transcript").click()
    expect(page.locator("#transcription-panel")).to_be_visible(timeout=10_000)
    expect(page.get_by_role("button", name="Chạy lại bản ghi")).to_be_visible()

    # The model selector is conditional in the product, so expose it here to exercise both
    # columns and make one help text wrap independently of the other.
    page.evaluate(
        """() => {
          document.querySelector("#transcription-model-control").hidden = false;
          document.querySelector("#transcription-model-help").textContent =
            "Một mô tả dài để kiểm tra rằng hàng trợ giúp dùng cùng một điểm bắt đầu " +
            "dù chỉ một cột xuống dòng.";
        }"""
    )
    rects = page.evaluate(
        """() => {
          const language = ".transcription-actions > " +
            ".transcription-language-control:not(#transcription-model-control)";
          const box = selector => {
            const value = document.querySelector(selector).getBoundingClientRect();
            return {top: value.top, bottom: value.bottom, left: value.left, right: value.right};
          };
          return {
            languageLabel: box(`${language} label`),
            modelLabel: box("#transcription-model-control label"),
            languageSelect: box(`${language} select`),
            modelSelect: box("#transcription-model-control select"),
            languageHelp: box("#transcription-language-help"),
            modelHelp: box("#transcription-model-help"),
            rerun: box("#start-transcription"),
            overflow: document.documentElement.scrollWidth - innerWidth,
          };
        }"""
    )
    assert rects["overflow"] <= 0
    if width > 760:
        for row in ("Label", "Select", "Help"):
            first, second = rects[f"language{row}"], rects[f"model{row}"]
            assert abs(first["top"] - second["top"]) <= 0.5, (width, row, rects)
        assert abs(rects["modelSelect"]["top"] - rects["rerun"]["top"]) <= 0.5, rects
    else:
        assert rects["modelLabel"]["top"] >= rects["languageHelp"]["bottom"] - 0.5, rects
        assert rects["rerun"]["top"] >= rects["modelHelp"]["bottom"] - 0.5, rects


@pytest.mark.browser
@pytest.mark.parametrize(
    ("language", "labels"),
    [("vi", ("Ngôn ngữ gợi ý", "Model ASR")), ("en", ("Language hint", "ASR model"))],
)
@pytest.mark.parametrize("width", [1280, 390])
def test_transcript_row_labels_are_short_and_clear_of_their_selects(
    live, signed_in, width, language, labels
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    page.evaluate("language => localStorage.setItem('asr-studio:lang', language)", language)
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir, title="Transcript labels")
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript")
    page.locator("#meeting-tab-transcript").click()
    expect(page.locator("#transcription-panel")).to_be_visible(timeout=10_000)
    page.evaluate(
        '() => { document.querySelector("#transcription-model-control").hidden = false; }'
    )
    for select, text in zip(
        ("#transcription-language", "#transcription-model"), labels, strict=True
    ):
        name = page.locator(f"label[for='{select[1:]}']")
        expect(name).to_have_text(text)
        gap = page.locator(select).bounding_box()["y"] - (
            name.bounding_box()["y"] + name.bounding_box()["height"]
        )
        assert 6 <= gap <= 9, (select, gap)


@pytest.mark.browser
@pytest.mark.parametrize("width", [1920, 1440])
def test_header_right_edge_matches_the_content_column_on_every_view(live, signed_in, width):
    meeting_id = seed_meeting(live.settings.data_dir, "Edge", "2026-10-14")
    _context, page = signed_in(viewport={"width": width, "height": 900})
    edges = {}

    def measure(view):
        edges[view] = page.evaluate(
            """() => {
              const column = document.querySelector(
                "#home-view:not([hidden]), #calendar-view:not([hidden]), "
                + "#meeting-view:not([hidden])");
              return [
                document.querySelector(".workspace-header").getBoundingClientRect().right,
                column.getBoundingClientRect().right,
              ];
            }"""
        )

    page.goto(f"{live.url}/")
    expect(page.locator("#home-view")).to_be_visible()
    measure("home")
    page.locator("#open-calendar:visible, #mini-calendar .cal-undated:visible").first.click()
    expect(page.locator("#large-calendar .cal-day").first).to_be_visible()
    measure("calendar")
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-view")).to_be_visible()
    measure("meeting")
    for view, (header, column) in edges.items():
        assert abs(header - column) <= 1, (view, header, column)


@pytest.mark.browser
def test_meeting_title_stays_at_the_top_when_tabs_change(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir, title="A meeting with saved notes")
    page.goto(f"{live.url}/meetings/{meeting_id}")
    tops = []
    for tab in ("notes", "transcript", "notes"):
        page.locator(f"#meeting-tab-{tab}").click()
        expect(page.locator(f"#meeting-panel-{tab}")).to_be_visible(timeout=10_000)
        tops.append(page.locator("#meeting-title").bounding_box()["y"])
    assert max(tops) - min(tops) <= 0.5, tops


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 1024, 900], ids=["1440", "1024", "900"])
@pytest.mark.parametrize("signed_in", [True], indirect=True)
def test_provider_launchers_stay_in_the_sticky_footer(live, signed_in, width):
    for index in range(8):
        seed_meeting(live.settings.data_dir, f"Footer overflow {index}", "2026-10-14")
    meeting_id = seed_meeting(live.settings.data_dir, "Footer focus meeting", "2026-10-14")
    _context, page = signed_in(viewport={"width": width, "height": 760})
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_be_visible(timeout=10_000)

    launchers = page.locator(".sidebar-footer .connection-launcher")
    expect(launchers).to_have_count(2)
    expect(launchers.nth(0)).to_have_attribute("aria-label", re.compile("^Kết nối OpenAI"))
    expect(launchers.nth(1)).to_have_attribute("aria-label", re.compile("^Kết nối Google"))
    expect(page.locator(".workspace-header .connection-launchers")).to_have_count(0)
    expect(page.locator(".header-create")).to_be_visible()
    expect(page.locator("#open-calendar")).to_be_hidden()

    metrics = page.evaluate(
        """() => {
          const rect = selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {top: box.top, right: box.right, bottom: box.bottom, width: box.width};
          };
          const elementRect = element => {
            const box = element.getBoundingClientRect();
            return {top: box.top, right: box.right, bottom: box.bottom, width: box.width};
          };
          const workspace = document.querySelector(".workspace");
          const style = getComputedStyle(workspace);
          const workspaceBox = workspace.getBoundingClientRect();
          const footer = document.querySelector(".sidebar-footer");
          const sidebar = document.querySelector(".sidebar");
          return {
            buttons: [...document.querySelectorAll(".sidebar-footer .connection-launcher")]
              .map(elementRect),
            footer: rect(".sidebar-footer"),
            sidebar: rect(".sidebar"),
            header: rect(".workspace-header"),
            meeting: rect(".meeting-view"),
            footerPosition: getComputedStyle(footer).position,
            railScrollRange: sidebar.scrollHeight - sidebar.clientHeight,
            overflow: document.documentElement.scrollWidth - innerWidth,
            workspaceContentRight: workspaceBox.right - parseFloat(style.paddingRight),
            footerHeightToken: getComputedStyle(document.documentElement)
              .getPropertyValue("--sidebar-footer-height").trim(),
          };
        }"""
    )
    assert metrics["buttons"][0]["top"] == metrics["buttons"][1]["top"]
    assert metrics["buttons"][0]["width"] == metrics["buttons"][1]["width"]
    assert metrics["buttons"][1]["right"] <= metrics["sidebar"]["right"] + 0.5
    assert metrics["footer"]["bottom"] >= 759.5
    assert metrics["footerPosition"] == "sticky"
    assert metrics["railScrollRange"] > 0
    assert metrics["overflow"] <= 0
    assert abs(metrics["header"]["right"] - metrics["meeting"]["right"]) <= 0.5
    assert metrics["header"]["right"] <= metrics["workspaceContentRight"] + 0.5
    assert metrics["footerHeightToken"].endswith("rem")

    launchers.nth(0).focus()
    page.keyboard.press("Tab")
    expect(launchers.nth(1)).to_be_focused()
    launchers.nth(0).click()
    expect(page.locator("#connections-dialog")).to_be_visible()
    page.locator("#connections-close").click()
    launchers.nth(1).click()
    expect(page.locator("#connections-dialog")).to_be_visible()


@pytest.mark.browser
@pytest.mark.parametrize("width", [390, 360, 320], ids=["390", "360", "320"])
def test_phone_launchers_are_logo_buttons_on_the_wordmark_row(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 844})
    page.goto(f"{live.url}/")
    launchers = page.locator(".sidebar-footer .connection-launcher")
    expect(launchers).to_have_count(2)
    expect(launchers.nth(0)).to_have_attribute("aria-label", re.compile("^Kết nối OpenAI"))
    expect(launchers.nth(1)).to_have_attribute("aria-label", re.compile("^Kết nối Google"))
    for index in range(2):
        button = launchers.nth(index)
        expect(button).to_be_visible()
        expect(button.locator(".connection-launcher-logo")).to_be_visible()
        expect(button.locator(".connection-launcher-name")).to_be_hidden()
        expect(button.locator(".connection-launcher-dot")).to_be_visible()
        box, dot = button.bounding_box(), button.locator(".connection-launcher-dot").bounding_box()
        assert 34 <= box["width"] <= 40 and abs(box["width"] - box["height"]) <= 0.5
        # The LED sits on the top-right corner, inside the button.
        assert dot["x"] + dot["width"] <= box["x"] + box["width"] + 0.5
        assert dot["x"] > box["x"] + box["width"] / 2 and dot["y"] < box["y"] + box["height"] / 2
    expect(page.locator(".sidebar-footer-meta")).to_be_hidden()
    assert page.locator(".version-link").is_hidden()

    # One row: brand, wordmark, theme, EN, OpenAI, Google share a centre line, left to right.
    items = [
        ".sidebar-masthead .brand-mark",
        ".sidebar-masthead .brand-wordmark",
        ".sidebar-masthead .theme-toggle",
        ".sidebar-masthead .lang-toggle",
        "#launcher-chatgpt",
        "#launcher-antigravity",
    ]
    boxes = [page.locator(selector).bounding_box() for selector in items]
    centres = [box["y"] + box["height"] / 2 for box in boxes]
    assert max(centres) - min(centres) <= 1, (width, boxes)
    for left, right in zip(boxes, boxes[1:], strict=False):
        assert left["x"] + left["width"] <= right["x"] + 0.5, (width, boxes)
    sidebar = page.locator(".sidebar").bounding_box()
    assert boxes[-1]["x"] + boxes[-1]["width"] <= sidebar["x"] + sidebar["width"] + 0.5
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0

    # Tab order equals the visual order, and each logo button still opens its dialog.
    page.locator(".sidebar-masthead .lang-toggle").focus()
    page.keyboard.press("Tab")
    expect(launchers.nth(0)).to_be_focused()
    page.keyboard.press("Tab")
    expect(launchers.nth(1)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#connections-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#connections-dialog")).to_be_hidden()


@pytest.mark.browser
def test_desktop_launchers_keep_the_dot_and_name_without_the_logo(live, signed_in):
    _context, page = signed_in(viewport={"width": 1024, "height": 760})
    page.goto(f"{live.url}/")
    for index in range(2):
        button = page.locator(".sidebar-footer .connection-launcher").nth(index)
        expect(button.locator(".connection-launcher-logo")).to_be_hidden()
        expect(button.locator(".connection-launcher-dot")).to_be_visible()
        expect(button.locator(".connection-launcher-name")).to_be_visible()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390, 320], ids=["1440", "390", "320"])
def test_workspace_language_toggle_stays_beside_theme_and_access_is_unchanged(
    live, signed_in, width
):
    context, page = signed_in(viewport={"width": width, "height": 844})
    page.goto(f"{live.url}/")
    expect(page.locator(".sidebar-masthead .lang-toggle")).to_be_visible()
    expect(page.locator(".workspace-header .lang-toggle")).to_have_count(0)
    controls = page.evaluate(
        """() => {
          const rect = selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {top: box.top, right: box.right, width: box.width};
          };
          const theme = rect(".sidebar-masthead .theme-toggle");
          const language = rect(".sidebar-masthead .lang-toggle");
          const brand = rect(".brand-wordmark");
          const sidebar = rect(".sidebar");
          return {
            theme, language, brand, sidebar,
            overflow: document.documentElement.scrollWidth - innerWidth,
          };
        }"""
    )
    assert abs(controls["theme"]["top"] - controls["language"]["top"]) <= 0.5
    assert controls["brand"]["right"] <= controls["sidebar"]["right"] + 0.5
    assert controls["overflow"] <= 0
    page.locator(".sidebar-masthead .lang-toggle").click()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator(".sidebar-masthead .lang-toggle")).to_have_text("VI")

    context.clear_cookies()
    page.goto(f"{live.url}/access")
    expect(page.locator(".access-header .lang-toggle")).to_be_visible()
    expect(page.locator(".sidebar-masthead")).to_have_count(0)


@pytest.mark.browser
def test_provider_launchers_keep_control_typography(live, signed_in):
    _context, page = signed_in(viewport={"width": 1024, "height": 760})
    page.goto(f"{live.url}/")
    launcher = page.locator("#launcher-chatgpt")
    expect(launcher).to_be_visible()
    typography = launcher.evaluate(
        """element => {
          const style = getComputedStyle(element);
          return {family: style.fontFamily, weight: style.fontWeight};
        }"""
    )
    assert "Inter Variable" in typography["family"], typography
    assert typography["weight"] == "680", typography


@pytest.mark.browser
def test_transcript_controls_drop_below_title_at_medium_width(live, signed_in):
    _context, page = signed_in(viewport={"width": 1024, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir, title="Medium transcript layout")
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript")
    page.locator("#meeting-tab-transcript").click()
    expect(page.locator("#transcription-panel")).to_be_visible(timeout=10_000)
    page.evaluate(
        """() => {
          const control = document.querySelector("#transcription-model-control");
          const select = document.querySelector("#transcription-model");
          control.hidden = false;
          select.replaceChildren(new Option("Tự động · chưa sẵn sàng", "auto"));
          select.value = "auto";
        }"""
    )
    geometry = page.evaluate(
        """() => {
          const title = document.querySelector("#transcription-heading").getBoundingClientRect();
          const controls = document.querySelector(".transcription-actions").getBoundingClientRect();
          const select = document.querySelector("#transcription-model");
          return {
            titleBottom: title.bottom,
            controlsTop: controls.top,
            selectClientWidth: select.clientWidth,
            selectScrollWidth: select.scrollWidth,
          };
        }"""
    )
    assert geometry["controlsTop"] >= geometry["titleBottom"] - 0.5, geometry
