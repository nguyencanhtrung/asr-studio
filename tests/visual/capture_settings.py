"""Capture the Settings surface with synthetic fixtures to an external directory."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright
from test_browser_connections import MockProviderApi, signed_in_page
from test_browser_history import LiveServer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path, help="Evidence directory outside the checkout")
args = parser.parse_args()
out = args.output.expanduser().resolve()
checkout = Path(__file__).resolve().parents[2]
if out == checkout or checkout in out.parents:
    parser.error("Evidence output must be outside the checkout")
out.mkdir(parents=True, exist_ok=True)
with TemporaryDirectory(prefix="visual-settings-") as tmp:
    live = LiveServer(Path(tmp))
    live.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path="/usr/bin/google-chrome")
            for width, scheme, lang in (
                (1440, "light", "vi"),
                (1440, "dark", "en"),
                (390, "dark", "vi"),
                (390, "light", "en"),
            ):
                api = MockProviderApi()
                api.status = "connected"
                api.antigravity_status = "connected"
                api.preferences = {
                    "provider": "chatgpt",
                    "models": {"chatgpt": "gpt-6.1-sol"},
                    "reasoning_effort": "high",
                }
                context, page, _ = signed_in_page(
                    browser,
                    live,
                    api=api,
                    color_scheme=scheme,
                    viewport={"width": width, "height": 900},
                )
                page.evaluate('(lang) => localStorage.setItem("asr-studio:lang",lang)', lang)
                page.reload()
                page.locator("#settings-open").click()
                for section in (
                    "ai",
                    "openai",
                    "antigravity",
                    "processing",
                    "asr",
                    "devices",
                    "theme",
                    "about",
                ):
                    if section in ("openai", "antigravity", "processing"):
                        page.locator("#settings-tab-ai").click()
                        page.locator(
                            f"#provider-overview-list button[data-settings-tab={section}]"
                        ).click()
                    else:
                        page.locator(f"#settings-tab-{section}").click()
                    panel = "ai" if section in ("ai", "openai", "antigravity") else section
                    expect(page.locator(f"#settings-panel-{panel}")).to_be_visible()
                    if section in ("openai", "antigravity"):
                        selector = (
                            "#connection-model" if section == "openai" else "#antigravity-model"
                        )
                        expect(page.locator(selector)).to_be_enabled()
                    page.evaluate("document.fonts.ready")
                    page.wait_for_load_state("networkidle", timeout=10_000)
                    page.screenshot(path=str(out / f"{section}-{scheme}-{width}-{lang}.png"))
                context.close()
            browser.close()
    finally:
        live.stop()
