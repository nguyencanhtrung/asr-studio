"""Capture reproducible Transcript workspace scenarios to an external directory."""

import argparse
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from notes_helpers import seed_completed_transcript
from playwright.sync_api import sync_playwright
from test_browser_history import LiveServer

from asr_studio.storage import database_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("phase", choices=("before", "after"))
parser.add_argument("output", type=Path, help="Evidence directory outside the checkout")
args = parser.parse_args()
out = args.output.expanduser().resolve()
checkout = Path(__file__).resolve().parents[2]
if out == checkout or checkout in out.parents:
    parser.error("Evidence output must be outside the checkout")
out.mkdir(parents=True, exist_ok=True)
phase = args.phase
with tempfile.TemporaryDirectory(prefix="transcript-283-") as tmp:
    server = LiveServer(Path(tmp))
    server.start()
    try:
        meeting, generation, segments = seed_completed_transcript(
            server.settings.data_dir, title="Product planning · Kế hoạch ra mắt"
        )
        with sqlite3.connect(database_path(server.settings.data_dir)) as db:
            key = db.execute(
                "SELECT storage_key FROM recordings WHERE meeting_id=?", (meeting,)
            ).fetchone()[0]
        audio = server.settings.data_dir / key
        audio.parent.mkdir(exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=16000",
                "-t",
                "30",
                "-c:a",
                "aac",
                str(audio),
            ],
            check=True,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")
            measurements = []
            for width, height, device in [(1440, 900, "desktop"), (390, 844, "mobile")]:
                for theme in ["light", "dark"]:
                    for lang in ["vi", "en"]:
                        context = browser.new_context(
                            viewport={"width": width, "height": height}, color_scheme=theme
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        page.goto(server.access_link)
                        page.wait_for_url(server.url + "/")
                        page.evaluate('(lang)=>localStorage.setItem("asr-studio:lang",lang)', lang)
                        page.goto(server.url + f"/meetings/{meeting}#transcript")
                        page.locator(".transcript-segment").first.wait_for()
                        page.locator("#meeting-audio").evaluate("(a)=>a.pause()")
                        page.evaluate("document.fonts.ready")
                        page.screenshot(
                            path=str(out / f"{phase}-{device}-{theme}-{lang}.png"), full_page=False
                        )
                        measurements.append(
                            {
                                "device": device,
                                "theme": theme,
                                "language": lang,
                                "search": page.locator("#transcript-search").bounding_box(),
                                "first": page.locator(".transcript-segment").first.bounding_box(),
                                "overflow": page.evaluate(
                                    "document.documentElement.scrollWidth > innerWidth"
                                ),
                                "page_errors": errors,
                            }
                        )
                        if phase == "after":
                            transcript_context = page.locator("#transcript-context")
                            summary = transcript_context.locator(":scope > summary")
                            summary.scroll_into_view_if_needed()
                            if not transcript_context.evaluate("el => el.open"):
                                summary.click()
                            catalog = page.locator("#speaker-catalog")
                            catalog.scroll_into_view_if_needed()
                            catalog.locator(".speaker-chip").first.scroll_into_view_if_needed()
                            page.screenshot(
                                path=str(out / f"{phase}-context-{device}-{theme}-{lang}.png"),
                                full_page=False,
                            )
                            assert not page.evaluate(
                                "document.documentElement.scrollWidth > innerWidth"
                            )
                        context.close()
            browser.close()
            (out / f"{phase}.json").write_text(json.dumps(measurements, indent=2))
    finally:
        server.stop()
