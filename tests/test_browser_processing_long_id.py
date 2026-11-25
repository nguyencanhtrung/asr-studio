"""Automation keeps long saved Antigravity model IDs readable at supported widths."""

import pytest
from playwright.sync_api import expect
from test_browser_automatic_processing import _open_automatic_processing, _processing_request
from test_browser_history import live as live  # noqa: F401
from test_browser_history import signed_in as signed_in  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 1280])
def test_long_saved_antigravity_id_wraps_in_automation_without_clipping(live, signed_in, width):
    _, page = signed_in(viewport={"width": width, "height": 860}, reduced_motion="reduce")
    model = "model/" + "x" * 234
    _processing_request(
        page,
        "/api/notes-preferences",
        "PUT",
        {"provider": "antigravity", "model": model, "reasoning_effort": "medium"},
    )
    before = _processing_request(page, "/api/processing-preferences")
    _open_automatic_processing(page)

    owner = page.locator("#automatic-preferences-notes-owner")
    expect(owner).to_contain_text(model)
    assert owner.evaluate("(element) => getComputedStyle(element).overflowWrap") == "anywhere"
    assert (
        page.locator("#antigravity-model-help").evaluate(
            "(element) => getComputedStyle(element).overflowWrap"
        )
        == "normal"
    )

    language = page.locator("#automatic-preferences-asr-language")
    expect(language).to_have_value("vi")
    language.select_option("en")
    save = page.locator("#automatic-preferences-save")
    expect(save).to_be_enabled()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/processing-preferences")
        )
    ) as reply:
        save.click()
    assert reply.value.status == 200
    expect(page.locator("#automatic-preferences-notice")).to_contain_text("Đã lưu")
    after = _processing_request(page, "/api/processing-preferences")
    assert after["notes"] == before["notes"]
    assert after["version"] == before["version"] + 1

    width_check = owner.evaluate(
        """(element) => {
            const panel = element.closest('[role=tabpanel]');
            const range = document.createRange();
            range.selectNodeContents(element);
            return {
              panelWidth: panel.clientWidth,
              panelContentWidth: panel.scrollWidth,
              textRight: Math.round(range.getBoundingClientRect().right),
              panelRight: Math.round(panel.getBoundingClientRect().right),
              text: element.textContent,
            };
        }"""
    )
    assert model in width_check["text"], width_check
    assert width_check["panelContentWidth"] <= width_check["panelWidth"] + 1, width_check
    assert width_check["textRight"] <= width_check["panelRight"] + 1, width_check
