"""Settings visual grouping and compact provider forms remain usable (#284)."""

import pytest
from playwright.sync_api import expect
from test_browser_connections import MockProviderApi, signed_in_page
from test_browser_connections import browser_runtime as browser_runtime  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("width", [1440, 681, 680, 390])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_grouped_settings_keep_provider_fields_and_all_sections_reachable(
    browser_runtime, width, scheme, language
):
    live, browser = browser_runtime
    api = MockProviderApi()
    api.status = "connected"
    api.antigravity_status = "connected"
    context, page, _ = signed_in_page(
        browser, live, api=api, color_scheme=scheme, viewport={"width": width, "height": 900}
    )
    try:
        page.evaluate("lang => localStorage.setItem('asr-studio:lang',lang)", language)
        page.reload()
        gear = page.locator("#settings-open")
        gear.click()
        dialog = page.locator("#connections-dialog")
        if width > 680:
            text_left = """button => {
                const range = document.createRange();
                range.selectNodeContents(button);
                return range.getBoundingClientRect().left;
            }"""
            processing_left = page.locator("#settings-tab-processing").evaluate(text_left)
            asr_left = page.locator("#settings-tab-asr").evaluate(text_left)
            assert abs(processing_left - asr_left) <= 1
        labels = (
            ("Nhà cung cấp", "Provider"),
            ("Model và xử lý", "Models and processing"),
            ("Ghi âm và kết nối", "Recording and connections"),
            ("Ứng dụng", "Application"),
        )
        for vietnamese, english in labels:
            label = page.locator("#settings-tabs").get_by_text(
                vietnamese if language == "vi" else english, exact=True
            )
            if width > 680:
                expect(label).to_be_visible()
            else:
                expect(label).to_be_hidden()
        page.locator("#provider-overview-list button[data-settings-tab=openai]").click()
        model = page.locator("#connection-model")
        effort = page.locator("#connection-effort")
        expect(model).to_be_enabled()
        expect(effort).to_be_visible()
        model_box, effort_box = model.bounding_box(), effort.bounding_box()
        if width >= 900:
            assert abs(model_box["y"] - effort_box["y"]) <= 1
            assert model_box["x"] + model_box["width"] <= effort_box["x"]
        else:
            assert effort_box["y"] > model_box["y"]
        model.select_option("gpt-6.1-sol")
        effort.select_option("high")
        expect(effort.locator("option:checked")).to_have_text(
            "Cao (high)" if language == "vi" else "High (high)"
        )
        page.locator("#connection-model-save").click()
        expect(page.locator("#connection-model-status")).to_contain_text(
            "Đã lưu" if language == "vi" else "Saved"
        )
        page.locator("#settings-tab-ai").click()
        page.locator("#provider-overview-list button[data-settings-tab=antigravity]").click()
        google_model = page.locator("#antigravity-model")
        google_effort = page.locator("#antigravity-effort")
        expect(google_model).to_be_enabled()
        expect(google_effort).to_be_visible()
        google_model_box = google_model.bounding_box()
        google_effort_box = google_effort.bounding_box()
        if width >= 900:
            assert abs(google_model_box["y"] - google_effort_box["y"]) <= 1
            assert google_model_box["x"] + google_model_box["width"] <= google_effort_box["x"]
        else:
            assert google_effort_box["y"] > google_model_box["y"]
        page.locator("#settings-tab-ai").click()
        page.locator("#provider-overview-list button[data-settings-tab=processing]").click()
        expect(page.locator("#settings-panel-processing")).to_be_visible()
        for section in ("asr", "devices", "theme", "about"):
            tab = page.locator(f"#settings-tab-{section}")
            tab.click()
            expect(page.locator(f"#settings-panel-{section}")).to_be_visible()
            expect(tab).to_have_attribute("aria-selected", "true")
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
        tab.focus()
        tab.press("Home")
        expect(page.locator("#settings-tab-ai")).to_be_focused()
        page.keyboard.press("End")
        expect(page.locator("#settings-tab-about")).to_be_focused()
        page.keyboard.press("ArrowUp" if width > 680 else "ArrowLeft")
        expect(page.locator("#settings-tab-theme")).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(gear).to_be_focused()
    finally:
        context.close()
