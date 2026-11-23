from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from notes_helpers import seed_cited_notes
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.notes import get_note, update_note

pytestmark = pytest.mark.browser

FORMATTED = (
    "# Weekly review\r\n\r\nKeep **Launch day** and *priorities* exact.\r\n\r\n"
    "3. Confirm the room\r\n4. Print the agenda\r\n\r\n"
    "| Owner | Location |\r\n| :--- | ---: |\r\n| Linh | Hà Nội |\r\n\r\n"
    "[Plan](https://example.invalid/agenda?key=one&format=2)\r\n"
)

EMAIL_FORMATTED = (
    "# Launch briefing\n\n## Introduction\n\nThanks for attending.\n\n"
    "## Confirmed Action Items\n\n- Bring the agenda.\n\n## Closing\n\nThank you.\n"
)


def _open_notes(live, page, make_markdown):
    meeting, revision, snapshot, segments = seed_cited_notes(live.settings.data_dir)
    markdown = make_markdown(revision["markdown"], meeting, snapshot["id"], segments)
    if markdown != revision["markdown"]:
        revision = update_note(
            live.settings.data_dir,
            revision["id"],
            expected_edit_version=revision["edit_version"],
            markdown=markdown,
        )
    page.goto(f"{live.url}/meetings/{meeting}#notes")
    expect(page.locator("#notes-saved-work")).to_be_visible()
    return meeting, revision, snapshot, markdown


@pytest.mark.parametrize(
    ("viewport_name", "viewport", "scheme"),
    [
        ("desktop", {"width": 1280, "height": 900}, "light"),
        ("desktop", {"width": 1280, "height": 900}, "dark"),
        ("mobile", {"width": 360, "height": 800}, "light"),
        ("mobile", {"width": 360, "height": 800}, "dark"),
    ],
)
def test_issue_256_visual_evidence(live, signed_in, viewport_name, viewport, scheme):
    output = os.environ.get("ASR_ISSUE_256_SCREENSHOT_DIR")
    if not output:
        pytest.skip("set ASR_ISSUE_256_SCREENSHOT_DIR to capture reviewed fixture screenshots")
    _context, page = signed_in(viewport=viewport, color_scheme=scheme)
    markdown = EMAIL_FORMATTED if viewport_name == "desktop" and scheme == "dark" else FORMATTED
    _open_notes(live, page, lambda *_: markdown)
    page.get_by_role("button", name="Viết", exact=True).click()
    expect(page.locator("#notes-rich-editor")).to_be_visible()
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(destination / f"notes-rich-{viewport_name}-{scheme}.png"),
        full_page=True,
        animations="disabled",
    )
    if viewport_name == "desktop" and scheme == "dark":
        page.get_by_role("button", name="Xem trước email", exact=True).click()
        dialog = page.get_by_role("dialog", name="Xem trước email")
        expect(dialog).to_be_visible()
        page.screenshot(
            path=str(destination / "notes-email-desktop-dark.png"),
            full_page=True,
            animations="disabled",
        )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_rendered_table_edit_keeps_other_markdown_and_focus(live, signed_in, scheme):
    _context, page = signed_in(viewport={"width": 360, "height": 800}, color_scheme=scheme)
    _, revision, _, _ = _open_notes(live, page, lambda *_: FORMATTED)
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    icon_tops = page.locator("#notes-copy, #notes-export").evaluate_all(
        "buttons => buttons.map(button => Math.round(button.getBoundingClientRect().top))"
    )
    assert icon_tops[0] == icon_tops[1]
    mode = page.get_by_role("group", name="Chế độ notes")
    write = mode.get_by_role("button", name="Viết", exact=True)
    raw_mode = mode.get_by_role("button", name="Markdown", exact=True)
    view = mode.get_by_role("button", name="Xem", exact=True)

    write.click()
    expect(write).to_have_attribute("aria-pressed", "true")
    expect(raw_mode).to_have_attribute("aria-pressed", "false")
    editor = page.get_by_role("group", name="Soạn notes có định dạng")
    expect(editor.get_by_role("textbox").first).to_be_focused()
    expect(editor.locator("ol")).to_have_attribute("start", "3")
    expect(editor.locator("strong")).to_have_text("Launch day")
    expect(editor.locator("em")).to_have_text("priorities")
    cell = editor.get_by_role("cell", name="Hà Nội").get_by_role("textbox")
    cell.fill("Hải Phòng")
    expect(page.locator("#notes-copy-rich")).to_be_disabled()
    expect(page.locator("#notes-copy-email")).to_be_disabled()
    expect(page.locator("#notes-markdown")).to_be_hidden()
    expect(editor.get_by_role("cell", name="Hải Phòng")).to_be_visible()
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == FORMATTED

    page.get_by_role("button", name="Hủy chỉnh sửa").click()
    expect(view).to_be_focused()
    expect(page.locator("#notes-preview").get_by_role("cell", name="Hà Nội")).to_be_visible()
    write.click()
    cell.fill("Hải Phòng")
    changed = editor.get_by_role("cell", name="Hải Phòng").get_by_role("textbox")
    with page.expect_response(lambda response: response.request.method == "PATCH") as saved:
        changed.press("Control+Enter")
    assert saved.value.ok
    expected = FORMATTED.replace("Hà Nội", "Hải Phòng")
    assert saved.value.request.post_data_json == {
        "expected_edit_version": revision["edit_version"],
        "markdown": expected,
    }
    expect(changed).to_be_focused()
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == expected

    view.click()
    expect(page.locator("#notes-preview").get_by_role("link", name="Plan")).to_have_attribute(
        "href", "https://example.invalid/agenda?key=one&format=2"
    )
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") == 0
    assert page_errors == []


def test_rich_keystrokes_keep_clean_markdown_and_block_structure_enter(live, signed_in):
    _context, page = signed_in()
    markdown = "# Plan\n\nHello\n\n- one\n- two\n"
    _, revision, _, _ = _open_notes(live, page, lambda *_: markdown)
    page.get_by_role("button", name="Viết", exact=True).click()
    editor = page.locator("#notes-rich-editor")
    paragraph = editor.locator('[contenteditable="plaintext-only"]').filter(has_text="Hello")
    paragraph.click()
    page.keyboard.press("End")
    page.keyboard.type(" world (draft).")
    list_item = editor.get_by_role("listitem").first.locator('[contenteditable="plaintext-only"]')
    list_item.click()
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    expect(list_item).to_be_focused()
    with page.expect_response(lambda response: response.request.method == "PATCH") as saved:
        paragraph.press("Control+Enter")
    assert saved.value.ok
    stored = get_note(live.settings.data_dir, revision["id"])["markdown"]
    assert stored == markdown.replace("Hello", "Hello world (draft).")
    assert "&#32;" not in stored
    assert r"\." not in stored


def test_rich_invalid_code_input_reverts_and_ime_enter_is_allowed(live, signed_in):
    _context, page = signed_in()
    markdown = "# Notes\n\nUse `npm ci`.\n"
    _, revision, _, _ = _open_notes(live, page, lambda *_: markdown)
    page.get_by_role("button", name="Viết", exact=True).click()
    code = page.locator('#notes-rich-editor code[contenteditable="plaintext-only"]')
    code.click()
    page.keyboard.press("End")
    page.keyboard.type("`")
    expect(code).to_have_text("npm ci")
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == markdown
    composing = page.evaluate(
        """() => {
          const node = document.querySelector(
            "#notes-rich-editor [contenteditable='plaintext-only']"
          );
          const event = new KeyboardEvent('keydown', {
            key: 'Enter', isComposing: true, bubbles: true, cancelable: true,
          });
          node.dispatchEvent(event);
          return event.defaultPrevented;
        }"""
    )
    assert composing is False


def test_mixed_markdown_falls_back_without_hiding_view_citations_or_source_links(live, signed_in):
    _context, page = signed_in()

    def markdown_with_sources(markdown, meeting, snapshot, segments):
        href = f"/meetings/{meeting}?notes_source={snapshot}&segment={segments[0]}#transcript"
        return (
            markdown.replace(
                "- The team reviewed its launch plan.", "  - The team reviewed its launch plan.", 1
            )
            + f"\n\n[Before]({href}) plus [literal fragment] plus [After]({href}).\n\n"
            + f"```md\n[In code]({href})\n```\n\n<script>throw Error('unsafe')</script>\n"
        )

    _, revision, snapshot, markdown = _open_notes(live, page, markdown_with_sources)
    preview = page.locator("#notes-preview")
    bullet = preview.get_by_role("listitem").filter(has_text="The team reviewed its launch plan.")
    expect(bullet.locator(".notes-citation-chip").first).to_have_attribute(
        "href", re.compile(rf"notes_source={snapshot['id']}&")
    )
    expect(preview).to_contain_text("[literal fragment]")
    expect(preview.get_by_role("link", name="Mở nguồn transcript tại Before")).to_be_visible()
    after = preview.get_by_role("link", name="Mở nguồn transcript tại After")
    expect(after).to_be_visible()
    expect(preview.get_by_role("link", name=re.compile("In code"))).to_have_count(0)
    expect(preview.locator("script")).to_have_count(0)
    expect(preview).to_contain_text("<script>throw Error('unsafe')</script>")

    page.get_by_role("button", name="Viết", exact=True).click()
    expect(page.locator("#notes-rich-fallback")).to_be_visible()
    expect(page.locator("#notes-markdown")).to_be_focused()
    expect(page.locator("#notes-markdown")).to_have_value(markdown)
    expect(page.locator("#notes-rich-editor [contenteditable]")).to_have_count(0)
    expect(page.locator("#notes-save")).to_be_disabled()
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == markdown
    page.get_by_role("button", name="Xem", exact=True).click()
    after.click()
    expect(page.locator("#notes-source-text")).to_contain_text("We need a launch plan.")
    expect(page.locator("#notes-source-heading")).to_be_focused()


def test_raw_boundaries_keep_view_source_links_and_reject_indented_tables(live, signed_in):
    _context, page = signed_in()

    def hard_break(_original, meeting, snapshot, segments):
        href = f"/meetings/{meeting}?notes_source={snapshot}&segment={segments[0]}#transcript"
        return f"# Notes\n\n[Before break]({href})  \nNext line.\n"

    _open_notes(live, page, hard_break)
    expect(
        page.locator("#notes-preview").get_by_role(
            "link", name="Mở nguồn transcript tại Before break"
        )
    ).to_be_visible()

    def indented_table(*_):
        return "# Notes\n\n    | A | B |\n    | --- | --- |\n    | x | y |\n"

    _open_notes(live, page, indented_table)
    page.get_by_role("button", name="Viết", exact=True).click()
    expect(page.locator("#notes-rich-fallback")).to_be_visible()
    expect(page.locator("#notes-rich-editor table")).to_have_count(0)


def test_unsupported_lists_keep_every_line_and_exact_view_chips(live, signed_in):
    _context, page = signed_in()

    def unsupported_lists(markdown, *_):
        return (
            markdown.replace(
                "- The team reviewed its launch plan.",
                "- The team reviewed its launch plan. \n  continuation text",
                1,
            )
            + "\n1. Ordered first\n  ordered continuation\n2. Ordered second\n"
        )

    _open_notes(live, page, unsupported_lists)
    preview = page.locator("#notes-preview")
    expect(preview).to_contain_text("continuation text")
    expect(preview).to_contain_text("1. Ordered first")
    expect(preview).to_contain_text("ordered continuation")
    expect(preview).to_contain_text("2. Ordered second")
    assert preview.locator(".notes-citation-chip").count() >= 2
    page.get_by_role("button", name="Viết", exact=True).click()
    expect(page.locator("#notes-rich-fallback")).to_be_visible()


def _stub_clipboard(page):
    page.evaluate(
        """() => {
          window.testWrites = [];
          window.testCopyMode = 'rich';
          Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
            async write([item]) {
              if (window.testCopyMode !== 'rich') throw Error('HTML denied');
              const entries = await Promise.all(item.types.map(async type =>
                [type, await (await item.getType(type)).text()]));
              window.testWrites.push(Object.fromEntries(entries));
            },
            async writeText(text) {
              if (window.testCopyMode === 'denied') throw Error('Text denied');
              window.testWrites.push({'text/plain': text});
            },
          }});
        }"""
    )


def test_email_preview_pins_saved_revision_and_reports_clipboard_outcomes(live, signed_in):
    _context, page = signed_in(viewport={"width": 360, "height": 800}, color_scheme="dark")

    def email(_original, meeting, snapshot, segments):
        href = f"/meetings/{meeting}?notes_source={snapshot}&segment={segments[0]}#transcript"
        return (
            "# Launch briefing\n\n## Introduction\n\nThanks for attending.\n\n"
            "## Confirmed Action Items\n\n- Bring the agenda.\n\n"
            f"- Follow [evidence]({href})\n\n"
            "[Unsafe](javascript:alert%281%29) ![pixel](https://example.invalid/beacon)\n\n"
            "<img src=https://example.invalid/beacon>\n"
        )

    meeting, revision, snapshot, markdown = _open_notes(live, page, email)
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    _stub_clipboard(page)

    opener = page.locator("#notes-copy-email")
    opener.click()
    dialog = page.get_by_role("dialog", name="Xem trước email")
    expect(dialog).to_be_visible()
    colors = dialog.locator("#notes-copy-document").evaluate(
        "element => [getComputedStyle(element).backgroundColor, getComputedStyle(element).color]"
    )
    assert colors == ["rgb(255, 255, 255)", "rgb(34, 34, 34)"]
    expect(dialog.get_by_role("heading", name="Xem trước email")).to_be_focused()
    expect(dialog.locator("#notes-copy-subject")).to_have_text("Chủ đề: Launch briefing")
    expect(dialog.get_by_role("region", name="Introduction")).to_be_visible()
    expect(dialog.get_by_role("region", name="Confirmed Action Items")).to_be_visible()
    expect(dialog.locator("[data-email-section]")).to_have_count(2)
    expect(dialog.locator("img, script, .notes-citation-chip")).to_have_count(0)
    assert page.evaluate("window.testWrites.length") == 0

    newer = update_note(
        live.settings.data_dir,
        revision["id"],
        expected_edit_version=revision["edit_version"],
        markdown="# Replaced\n",
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting,
    )
    expect(page.locator("#notes-revision-meta")).to_contain_text(f"Edit {newer['edit_version']}")
    expect(dialog.locator("#notes-copy-pin")).to_contain_text(f"edit {revision['edit_version']}")

    confirm = dialog.get_by_role("button", name="Sao chép nội dung email")
    status = dialog.locator("#notes-copy-result")
    confirm.click()
    expect(status).to_have_text("Đã sao chép nội dung có định dạng.")
    assert page.evaluate("window.testWrites[0]['text/plain']") == markdown
    html = page.evaluate("window.testWrites[0]['text/html']")
    assert '<section aria-label="Confirmed Action Items">' in html
    assert f"notes_source={snapshot['id']}&amp;segment=" in html
    assert "background:#fff" in html
    assert "&lt;img" in html
    assert all(value not in html for value in ("<img", 'href="javascript:', "Replaced", "Outro"))

    page.evaluate("window.testCopyMode = 'plain'")
    confirm.click()
    expect(status).to_contain_text("Đã sao chép văn bản Markdown")
    assert page.evaluate("window.testWrites[1]") == {"text/plain": markdown}
    page.evaluate("window.testCopyMode = 'denied'")
    confirm.click()
    expect(status).to_contain_text("chưa sao chép nội dung")
    expect(confirm).to_be_focused()
    assert page.evaluate("window.testWrites.length") == 2
    assert all(method == "GET" and url.startswith(live.url) for method, url in requests)
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") == 0

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def test_late_email_read_does_not_take_focus_from_transcript_or_reopen_on_return(live, signed_in):
    _context, page = signed_in()
    _, revision, _, _ = _open_notes(live, page, lambda *_: FORMATTED)
    endpoint = f"**/api/notes/{revision['id']}"
    pending = []
    page.route(endpoint, lambda route: pending.append(route))

    with page.expect_request(endpoint):
        page.locator("#notes-copy-email").click()
    transcript = page.get_by_role("tab", name=re.compile("Transcript"))
    transcript.click()
    expect(transcript).to_have_attribute("aria-selected", "true")
    assert len(pending) == 1
    pending[0].fulfill(
        json={
            **revision,
            "markdown": "# Obsolete preview",
            "edit_version": revision["edit_version"] + 1,
        }
    )
    expect(page.locator("#notes-copy-dialog")).to_be_hidden()
    expect(transcript).to_be_focused()

    page.locator("#meeting-tab-notes").click()
    expect(page.locator("#notes-copy-dialog")).to_be_hidden()
    expect(page.locator("#notes-preview")).to_contain_text("Weekly review")
    expect(page.locator("#notes-copy-export-status")).to_be_empty()
    page.unroute(endpoint)
    page.locator("#notes-copy-email").click()
    expect(page.get_by_role("dialog", name="Xem trước email")).to_be_visible()
