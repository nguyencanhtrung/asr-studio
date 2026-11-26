"""Capture baseline/candidate from identical disposable persisted Notes; no inference calls."""

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright
from test_browser_history import LiveServer
from test_browser_notes import _seed_saved_notes
from test_browser_notes_provider import mock_providers

from asr_studio import notes_routes, routes

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "docs/assets/frontend/issue-282"
OUT.mkdir(parents=True, exist_ok=True)
source = ROOT / "src/asr_studio/web"
measurements = []
with TemporaryDirectory(prefix="notes-282-evidence-") as tmp:
    temporary = Path(tmp)
    baseline = temporary / "baseline-web"
    shutil.copytree(source, baseline)
    for name in ("notes.css", "notes.js", "shell.html"):
        (baseline / name).write_bytes(
            subprocess.check_output(["git", "show", f"a112687:src/asr_studio/web/{name}"], cwd=ROOT)
        )
    live = LiveServer(temporary)
    live.start()
    try:
        meeting, *_ = _seed_saved_notes(live.settings.data_dir, title="Sample meeting")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path="/usr/bin/google-chrome")
            for phase, assets in (("before", baseline), ("after", source)):
                routes.WEB_DIR = assets
                notes_routes.WEB_DIR = assets
                for language in ("vi", "en"):
                    for scheme in ("light", "dark"):
                        for width in (1440, 390):
                            page = browser.new_page(
                                viewport={"width": width, "height": 900}, color_scheme=scheme
                            )
                            page.add_init_script(
                                f"localStorage.setItem('asr-studio:lang', '{language}')"
                            )
                            mock_providers(page, live)
                            page.goto(live.access_link)
                            page.wait_for_url(live.url + "/")
                            page.goto(f"{live.url}/meetings/{meeting}#notes")
                            page.locator("#notes-preview > *").first.wait_for()
                            page.evaluate("document.fonts.ready")
                            stem = f"{phase}-{width}-{scheme}-{language}"
                            box = page.locator("#notes-preview").bounding_box()
                            paragraph = page.locator(
                                "#notes-preview li, #notes-preview p"
                            ).first.bounding_box()
                            measurements.append(
                                dict(
                                    phase=phase,
                                    width=width,
                                    theme=scheme,
                                    language=language,
                                    preview_y=box["y"],
                                    first_content_y=paragraph["y"],
                                    first_content_bottom=paragraph["y"] + paragraph["height"],
                                )
                            )
                            page.screenshot(
                                path=str(OUT / f"{stem}.png"), full_page=True, animations="disabled"
                            )
                            if phase == "after":
                                page.locator("#notes-export").focus()
                                page.keyboard.press("Tab")
                                assert page.locator("#notes-mode-view").evaluate(
                                    "el => el === document.activeElement"
                                )
                            else:
                                page.locator("#notes-export").focus()
                                page.keyboard.press("Tab")
                            page.screenshot(
                                path=str(OUT / f"{stem}-focus.png"), animations="disabled"
                            )
                            page.close()
            browser.close()
    finally:
        routes.WEB_DIR = source
        notes_routes.WEB_DIR = source
        live.stop()
(OUT / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")
print(json.dumps(measurements, indent=2))
