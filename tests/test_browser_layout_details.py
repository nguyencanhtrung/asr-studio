from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_browser_history import _publish_browser_generation, seed_meeting
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

pytestmark = pytest.mark.browser


def _focus_identity(page):
    return page.evaluate(
        """() => {
          const element = document.activeElement;
          if (!element) return null;
          if (element.id) return `#${element.id}`;
          if (element.classList.contains("lang-toggle")) return ".lang-toggle";
          if (element.classList.contains("header-create")) return ".header-create";
          return element.className || element.tagName.toLowerCase();
        }"""
    )


def _layout_rectangles(page):
    return page.evaluate(
        """() => {
          const rect = (element) => {
            const box = element.getBoundingClientRect();
            return {
              top: Number(box.top.toFixed(3)),
              right: Number(box.right.toFixed(3)),
              bottom: Number(box.bottom.toFixed(3)),
              left: Number(box.left.toFixed(3)),
              width: Number(box.width.toFixed(3)),
              height: Number(box.height.toFixed(3)),
            };
          };
          const sidebar = document.querySelector(".sidebar");
          const footer = document.querySelector(".sidebar-footer");
          const version = document.querySelector(".version-link");
          const actions = document.querySelector(".header-actions");
          const launchers = document.querySelector(".connection-launchers");
          const header = document.querySelector(".workspace-header");
          const workspace = document.querySelector(".workspace");
          const root = document.documentElement;
          const grid = getComputedStyle(document.querySelector(".app-frame")).gridTemplateColumns;
          const workspaceStyle = getComputedStyle(workspace);
          const workspaceBox = workspace.getBoundingClientRect();
          return {
            viewport: { width: innerWidth, height: innerHeight },
            theme: root.dataset.theme,
            wordmark: document.querySelector(".brand-wordmark").innerText,
            brandName: document.querySelector(".brand").getAttribute("aria-label"),
            formErrorTabIndex: document.querySelector("#form-error").getAttribute("tabindex"),
            domOrder: Boolean(launchers.compareDocumentPosition(actions)
              & Node.DOCUMENT_POSITION_FOLLOWING),
            actions: rect(actions),
            header: rect(header),
            launchers: rect(launchers),
            sidebar: rect(sidebar),
            footer: rect(footer),
            version: rect(version),
            versionVisible: (() => {
              const footerBox = footer.getBoundingClientRect();
              const versionBox = version.getBoundingClientRect();
              return versionBox.top >= footerBox.top - 0.5
                && versionBox.bottom <= footerBox.bottom + 0.5;
            })(),
            sidebarOverflowY: getComputedStyle(sidebar).overflowY,
            sidebarScrollRange: sidebar.scrollHeight - sidebar.clientHeight,
            launchersInFooter: footer.contains(launchers),
            workspaceContentRight: workspaceBox.right - parseFloat(workspaceStyle.paddingRight),
            positiveTabIndexCount: [...document.querySelectorAll("[tabindex]")]
              .filter((element) => element.tabIndex > 0).length,
            gridColumns: grid,
          };
        }"""
    )


def _focus_rectangles(page):
    return page.evaluate(
        """() => {
          const focused = document.activeElement;
          const player = document.querySelector(".player-bar");
          const focusedBox = focused.getBoundingClientRect();
          const playerBox = player.getBoundingClientRect();
          const focusedStyle = getComputedStyle(focused);
          const outlineWidth = parseFloat(focusedStyle.outlineWidth) || 0;
          const outlineOffset = parseFloat(focusedStyle.outlineOffset) || 0;
          const rootStyle = getComputedStyle(document.documentElement);
          return {
            active: focused.className || focused.id || focused.tagName,
            focused: {
              top: Number(focusedBox.top.toFixed(3)),
              bottom: Number(focusedBox.bottom.toFixed(3)),
              left: Number(focusedBox.left.toFixed(3)),
              right: Number(focusedBox.right.toFixed(3)),
            },
            outline: {
              width: outlineWidth,
              offset: outlineOffset,
              top: Number((focusedBox.top - outlineWidth - outlineOffset).toFixed(3)),
              bottom: Number((focusedBox.bottom + outlineWidth + outlineOffset).toFixed(3)),
              style: focusedStyle.outlineStyle,
            },
            player: {
              top: Number(playerBox.top.toFixed(3)),
              bottom: Number(playerBox.bottom.toFixed(3)),
            },
            scrollPaddingTop: rootStyle.scrollPaddingTop,
            playerHeight: rootStyle.getPropertyValue("--player-height").trim(),
            playerClearance: rootStyle.getPropertyValue("--player-clearance").trim(),
          };
        }"""
    )


def _long_transcript():
    return [
        {
            "id": f"{index + 1:032x}",
            "start_seconds": index * 3,
            "end_seconds": index * 3 + 2.5,
            "text": f"Đoạn transcript đã lưu để kiểm tra điều khiển phát lại số {index + 1}.",
            "original_speaker_id": "SPEAKER_00" if index % 2 == 0 else "SPEAKER_01",
            "speaker_id": "SPEAKER_00" if index % 2 == 0 else "SPEAKER_01",
        }
        for index in range(24)
    ]


def _write_evidence(engine, evidence, evidence_dir):
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"layout-{engine}.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads(target.read_text(encoding="utf-8")) == evidence
    return target


def test_layout_matrix_and_keyboard_order(live, signed_in, tmp_path):
    evidence_dir = Path(os.environ.get("ASR_UI_EVIDENCE_DIR", str(tmp_path / "evidence")))
    focus_meeting = seed_meeting(
        live.settings.data_dir,
        "Layout focus meeting",
        "2026-10-14",
        duration_seconds=120,
    )
    seed_meeting(live.settings.data_dir, "Recent layout review", "2026-10-14", duration_seconds=90)
    seed_meeting(
        live.settings.data_dir, "Recent accessibility review", "2026-10-09", duration_seconds=60
    )
    _publish_browser_generation(
        live,
        focus_meeting,
        segments=_long_transcript(),
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 0,
                "last_seen_seconds": 70,
            },
            {
                "speaker_id": "SPEAKER_01",
                "display_name": "Speaker 2",
                "first_seen_seconds": 3,
                "last_seen_seconds": 71,
            },
        ],
    )

    engine = os.environ.get("ASR_BROWSER_ENGINE", "chromium").lower()
    evidence = {"engine": engine, "home": [], "focus": []}
    matrix = (
        ("mobile", 360, 844),
        ("desktop-1024", 1024, 768),
        ("desktop-1280", 1280, 800),
    )

    for theme in ("light", "dark"):
        for label, width, height in matrix:
            context, page = signed_in(
                color_scheme=theme,
                viewport={"width": width, "height": height},
                reduced_motion="reduce",
            )
            try:
                page.goto(f"{live.url}/", wait_until="networkidle")
                expect(page.locator("#recent-list a")).to_have_count(3)
                expect(page.locator(".brand-wordmark")).to_have_text("ASR Studio")
                expect(page.locator(".brand")).to_have_accessible_name("ASR Studio — về workspace")
                expect(page.locator("#form-error")).to_have_attribute("tabindex", "-1")

                metrics = _layout_rectangles(page)
                assert metrics["theme"] == theme
                assert metrics["domOrder"]
                assert metrics["wordmark"] == "ASR Studio"
                assert metrics["formErrorTabIndex"] == "-1"
                assert metrics["positiveTabIndexCount"] == 0
                assert metrics["launchersInFooter"]
                assert metrics["header"]["right"] <= metrics["workspaceContentRight"] + 0.5
                if width == 360:
                    assert metrics["launchers"]["bottom"] <= metrics["actions"]["top"] + 0.5
                    page.locator(".theme-toggle").focus()
                    keyboard_order = []
                    for _ in range(5):
                        page.keyboard.press("Tab")
                        keyboard_order.append(_focus_identity(page))
                    assert keyboard_order == [
                        ".lang-toggle",
                        "#launcher-chatgpt",
                        "#launcher-antigravity",
                        "#open-calendar",
                        ".header-create",
                    ]
                    metrics["keyboardOrder"] = keyboard_order
                else:
                    page.locator("#launcher-chatgpt").focus()
                    keyboard_order = []
                    for _ in range(3):
                        page.keyboard.press("Tab")
                        keyboard_order.append(_focus_identity(page))
                    assert keyboard_order == [
                        "#launcher-antigravity",
                        "github-link",
                        "version-link",
                    ]
                    metrics["keyboardOrder"] = keyboard_order

                if width >= 1024:
                    assert metrics["sidebar"]["width"] == 272
                    assert metrics["versionVisible"]
                    assert metrics["sidebarOverflowY"] == "auto"
                    assert metrics["sidebarScrollRange"] > 0
                    scrolled = page.locator(".sidebar").evaluate(
                        "element => { element.scrollTop = element.scrollHeight; "
                        "return element.scrollTop; }"
                    )
                    assert scrolled > 0
                    page.locator(".version-link").focus()
                    assert page.locator(".version-link").evaluate(
                        "element => { const box = element.getBoundingClientRect(); "
                        "return box.top >= 0 && box.bottom <= innerHeight; }"
                    )
                    page.locator(".sidebar").evaluate("element => { element.scrollTop = 0; }")
                screenshot = Path(tmp_path) / f"{engine}-{theme}-{label}.png"
                page.screenshot(path=screenshot, full_page=True, animations="disabled")
                assert screenshot.stat().st_size > 10_000
                evidence_dir.mkdir(parents=True, exist_ok=True)
                evidence["home"].append(
                    {
                        "theme": theme,
                        "label": label,
                        "rectangles": metrics,
                        "screenshot": str(evidence_dir / screenshot.name),
                    }
                )
                screenshot.replace(evidence_dir / screenshot.name)
            finally:
                context.close()

    # The narrower desktop rail stays 240px without adding another screenshot pass.
    context, page = signed_in(
        color_scheme="light",
        viewport={"width": 900, "height": 700},
        reduced_motion="reduce",
    )
    try:
        page.goto(f"{live.url}/", wait_until="networkidle")
        narrow_metrics = _layout_rectangles(page)
        assert narrow_metrics["sidebar"]["width"] == 240
        evidence["rail_width_900"] = narrow_metrics["sidebar"]["width"]
    finally:
        context.close()

    for theme in ("light", "dark"):
        for width, height in ((360, 844), (1024, 768)):
            context, page = signed_in(
                color_scheme=theme,
                viewport={"width": width, "height": height},
                reduced_motion="reduce",
            )
            try:
                page.goto(
                    f"{live.url}/meetings/{focus_meeting}#transcript", wait_until="networkidle"
                )
                expect(page.locator("#transcript-generation")).to_be_visible()
                expect(page.locator(".transcript-segment")).to_have_count(24)
                timestamp = page.locator(".segment-timestamp").nth(20)
                timestamp.focus()
                timestamp.evaluate(
                    "element => element.scrollIntoView({block: 'start', behavior: 'instant'})"
                )
                page.keyboard.press("Tab")
                expect(page.locator(".segment-edit-button").nth(20)).to_be_focused()
                focus_metrics = _focus_rectangles(page)
                assert focus_metrics["active"] == "secondary-button segment-edit-button"
                assert focus_metrics["outline"]["style"] == "solid"
                assert focus_metrics["outline"]["width"] >= 2
                assert focus_metrics["scrollPaddingTop"] == "72px"
                assert focus_metrics["playerHeight"] == "64px"
                assert focus_metrics["playerClearance"] == "8px"
                assert focus_metrics["outline"]["top"] >= focus_metrics["player"]["bottom"] - 0.5
                focus_screenshot = evidence_dir / f"{engine}-{theme}-focus-{width}x{height}.png"
                page.screenshot(path=focus_screenshot, full_page=False, animations="disabled")
                assert focus_screenshot.stat().st_size > 10_000
                evidence["focus"].append(
                    {
                        "theme": theme,
                        "viewport": {"width": width, "height": height},
                        "rectangles": focus_metrics,
                        "screenshot": str(focus_screenshot),
                    }
                )
            finally:
                context.close()

    evidence_path = _write_evidence(engine, evidence, evidence_dir)
    assert evidence_path.is_file()


def test_last_calendar_day_full_focus_outline_clears_sticky_footer(live, signed_in):
    for title in ("August one", "August two", "August three"):
        seed_meeting(live.settings.data_dir, title, "2026-08-31")

    for theme in ("light", "dark"):
        context, page = signed_in(
            color_scheme=theme,
            viewport={"width": 1024, "height": 768},
            reduced_motion="reduce",
        )
        try:
            page.goto(f"{live.url}/", wait_until="networkidle")
            expect(page.locator("#recent-list a")).to_have_count(3)
            month = page.locator("#mini-calendar .cal-month")
            month.select_option("7")
            year = page.locator("#mini-calendar .cal-year")
            year.fill("2026")
            year.press("Enter")

            first_day = page.locator('#mini-calendar .cal-day[data-date="2026-08-01"]')
            first_day.focus()
            for _ in range(30):
                page.keyboard.press("ArrowRight")
            day = page.locator('#mini-calendar .cal-day[data-date="2026-08-31"]')
            expect(day).to_be_focused()
            metrics = day.evaluate(
                """element => {
                  const box = element.getBoundingClientRect();
                  const footer = document.querySelector('.sidebar-footer').getBoundingClientRect();
                  const sidebar = document.querySelector('.sidebar');
                  const style = getComputedStyle(element);
                  const outlineWidth = parseFloat(style.outlineWidth) || 0;
                  const outlineOffset = parseFloat(style.outlineOffset) || 0;
                  return {
                    active: element === document.activeElement,
                    focusVisible: style.outlineStyle === 'solid',
                    bottom: box.bottom,
                    outlineBottom: box.bottom + outlineWidth + outlineOffset,
                    footerTop: footer.top,
                    footerHeight: footer.height,
                    sidebarPaddingBottom: getComputedStyle(sidebar).paddingBottom,
                    sidebarScrollPaddingBottom: getComputedStyle(sidebar).scrollPaddingBottom,
                  };
                }"""
            )
            assert metrics["active"]
            assert metrics["focusVisible"]
            assert metrics["outlineBottom"] <= metrics["footerTop"] + 0.5, metrics
        finally:
            context.close()
