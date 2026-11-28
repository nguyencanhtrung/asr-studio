from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes import _seed_saved_notes
from test_browser_notes import signed_in as signed_in

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    ("width", "scheme", "language"),
    [
        (1440, "light", "vi"),
        (1440, "dark", "en"),
        (390, "light", "en"),
        (390, "dark", "vi"),
    ],
)
def test_notes_shell_matches_prototype_composition(live, signed_in, width, scheme, language):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.emulate_media(color_scheme=scheme)
    page.evaluate("lang => localStorage.setItem('asr-studio:lang', lang)", language)
    meeting, *_ = _seed_saved_notes(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting}#notes", wait_until="networkidle")
    page.evaluate("document.fonts.ready")

    expect(page.locator(".workspace-breadcrumb")).to_be_visible()
    expect(page.locator(".workspace-breadcrumb a")).to_have_attribute("href", "/")
    expect(page.locator(".header-create")).to_be_visible()
    expect(page.locator("#notes-preview")).to_be_visible()
    expect(page.locator("#notes-mode-rich")).to_be_visible()
    expect(page.locator("#notes-save")).to_be_hidden()
    expect(page.locator("#notes-edit-cancel")).to_be_hidden()
    for selector in (
        "#notes-history-open",
        "#notes-copy-email",
        "#notes-copy-rich",
        "#notes-copy",
        "#notes-export",
    ):
        expect(page.locator(selector)).to_be_visible()

    assert page.locator("#notes-toolbar").evaluate("toolbar => !toolbar.closest('.notes-frame')")
    assert not errors, errors
    if width == 390:
        # The document, not a stack of action buttons, must be reached in the
        # initial viewport with navigation collapsed. This fails on the old UI.
        first_paragraph = page.locator("#notes-preview li").first.bounding_box()
        assert first_paragraph and first_paragraph["y"] < 650
        expect(page.locator("#settings-open")).to_be_visible()
        expect(page.locator("#sidebar-history")).not_to_have_attribute("open", "")

    evidence_dir = os.environ.get("ASR_VISUAL_EVIDENCE_DIR")
    if evidence_dir:
        destination = Path(evidence_dir)
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(destination / f"notes-{width}-{scheme}-{language}.png"),
            full_page=False,
            animations="disabled",
        )

    if width == 390:
        page.locator("#sidebar-history > summary").click()
    mini_calendar = page.locator("#mini-calendar")
    expect(mini_calendar.locator(".cal-step")).to_have_count(2)
    expect(mini_calendar.locator(".cal-undated")).to_be_visible()
    expect(mini_calendar.locator(".calendar-picker-disclosure")).to_be_visible()
    expect(mini_calendar.locator(".calendar-picker-disclosure .cal-month")).to_be_hidden()
    expect(mini_calendar.locator(".calendar-picker-disclosure .cal-year")).to_be_hidden()
    mini_calendar.locator(".calendar-picker-disclosure > summary").click()
    expect(mini_calendar.locator(".calendar-picker-disclosure .cal-month")).to_be_visible()
    expect(mini_calendar.locator(".calendar-picker-disclosure .cal-year")).to_be_visible()

    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 1440:
        shell = page.locator(".app-frame").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns"
        )
        assert shell.startswith("252px ")
        assert page.locator(".workspace-header").bounding_box()["height"] <= 66
        paper = page.locator(".notes-frame").bounding_box()
        content = page.locator("#notes-preview").bounding_box()
        toolbar = page.locator("#notes-toolbar").bounding_box()
        assert paper and content and toolbar
        assert 720 <= content["width"] <= 800
        assert content["y"] > toolbar["y"] + toolbar["height"]
    else:
        paper = page.locator(".notes-frame").bounding_box()
        assert paper and paper["x"] + paper["width"] <= width + 0.5
