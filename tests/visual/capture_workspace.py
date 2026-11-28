"""Collect reviewable handoff screenshots from disposable real-app fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright
from seeding import seed_meeting
from test_browser_history import LiveServer

import asr_studio

PROFILES = ((1440, "light", "vi"), (1440, "dark", "en"), (390, "light", "en"), (390, "dark", "vi"))


def capture_calendar_and_import(out: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    with TemporaryDirectory(prefix="visual-workspace-") as temporary:
        live = LiveServer(Path(temporary))
        live.start()
        try:
            for title in ("Design review", "Release planning"):
                seed_meeting(live.settings.data_dir, title, today)
            with sync_playwright() as playwright:
                chrome = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")
                browser = playwright.chromium.launch(executable_path=chrome)
                for width, scheme, language in PROFILES:
                    context = browser.new_context(
                        viewport={"width": width, "height": 900},
                        color_scheme=scheme,
                        timezone_id="UTC",
                        reduced_motion="reduce",
                    )
                    context.add_init_script(
                        f"localStorage.setItem('asr-studio:lang', {json.dumps(language)})"
                    )
                    page = context.new_page()
                    page.set_default_timeout(10_000)
                    errors: list[str] = []
                    page.on(
                        "pageerror", lambda error, collected=errors: collected.append(str(error))
                    )
                    page.goto(live.access_link, wait_until="domcontentloaded")
                    page.wait_for_url(live.url + "/")
                    page.locator(".header-create").click()
                    expect(page.locator("#meeting-dialog")).to_be_visible()
                    expect(page.locator("#file-drop")).to_be_enabled()
                    expect(page.locator("#file-drop")).not_to_have_attribute(
                        "aria-disabled", "true"
                    )
                    for pending in ("Loading", "Đang tải"):
                        expect(page.locator("#file-limits")).not_to_contain_text(pending)
                    page.evaluate("document.fonts.ready")
                    page.screenshot(
                        path=str(out / f"new-meeting-{width}-{scheme}-{language}.png"),
                        animations="disabled",
                    )
                    page.keyboard.press("Escape")
                    expect(page.locator("#meeting-dialog")).to_be_hidden()
                    expect(page.locator(".header-create")).to_be_focused()
                    if width == 390:
                        page.locator("#open-calendar").click()
                    else:
                        page.locator(f'#mini-calendar .cal-day[data-date="{today}"]').click()
                    expect(page.locator("#calendar-view")).to_be_visible()
                    expect(page.locator("#large-calendar .cal-chip")).to_have_count(2)
                    page.evaluate("document.fonts.ready")
                    page.screenshot(
                        path=str(out / f"calendar-{width}-{scheme}-{language}.png"),
                        animations="disabled",
                    )
                    assert not errors, errors
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    context.close()
                browser.close()
        finally:
            live.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[2]
    out = args.output.expanduser().resolve()
    if out == checkout or checkout in out.parents:
        parser.error("Evidence output must be outside the checkout")
    assert Path(asr_studio.__file__).resolve().is_relative_to(checkout / "src")
    out.mkdir(parents=True, exist_ok=True)
    # These come from the full browser suite, using the actual Notes/Transcript
    # scenarios. Missing evidence must fail rather than produce a partial demo.
    assert len(list(out.glob("notes-*.png"))) == 4, "Run the Notes browser matrix first"
    assert len(list(out.glob("transcript-*.png"))) == 8, "Run the Transcript browser matrix first"
    capture_calendar_and_import(out)
    subprocess.run(
        [sys.executable, str(checkout / "tests/visual/capture_settings.py"), str(out / "settings")],
        check=True,
        timeout=180,
    )
    assert len(list((out / "settings").glob("*.png"))) == 32
    files = sorted(out.rglob("*.png"))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout))
    if source_commit := os.environ.get("GITHUB_SHA"):
        assert head == source_commit and not dirty, (
            "CI evidence must match the clean checked-out commit"
        )
    manifest = {
        "source_commit": head,
        "dirty": dirty,
        "scope": (
            "Disposable synthetic fixtures. Settings uses display stubs; "
            "no live auth/inference proof."
        ),
        "files": [
            {
                "path": str(file.relative_to(out)),
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "bytes": file.stat().st_size,
            }
            for file in files
        ],
        "web_assets": {
            str(file.relative_to(checkout)): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted((checkout / "src/asr_studio/web").iterdir())
            if file.is_file() and file.suffix in (".css", ".js", ".html")
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(files)} disposable handoff screenshots at {head}; dirty={dirty}")


if __name__ == "__main__":
    main()
