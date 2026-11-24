"""Check that a successful Automation save reconciles an independently changed owner."""

import pytest
from playwright.sync_api import expect
from test_browser_automatic_processing import _open_automatic_processing, _processing_request
from test_browser_history import live as live  # noqa: F401
from test_browser_history import signed_in as signed_in

from asr_studio.antigravity import AntigravityProvider

pytestmark = pytest.mark.browser
pytest_plugins = ("test_browser_asr_model",)


@pytest.mark.parametrize("hold_owner_read", [False, True])
def test_confirmed_automation_save_reconciles_an_owner_changed_in_another_client(
    live, signed_in, monkeypatch, hold_owner_read
):
    older, newer = "gemini-3.1-pro", "gemini-3.1-flash"
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    _processing_request(
        owner,
        "/api/notes-preferences",
        "PUT",
        {"provider": "antigravity", "model": older, "reasoning_effort": "high"},
    )
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-notes-owner")
    save = reader.locator("#automatic-preferences-save")
    expect(summary).to_contain_text(f"{older} · effort high")
    expect(save).to_be_disabled()
    before = _processing_request(owner, "/api/processing-preferences")

    monkeypatch.setattr(
        AntigravityProvider,
        "list_models",
        lambda self: {"models": [{"id": newer, "display_name": newer}]},
    )
    _processing_request(
        owner,
        "/api/providers/antigravity/preference",
        "PUT",
        {"model": newer, "reasoning_effort": "medium"},
    )
    assert _processing_request(owner, "/api/processing-preferences") == before
    owners_url = f"{live.url}/api/notes-preferences"
    writes = []
    held = []

    def note_write(request):
        if request.method == "PATCH" and request.url.endswith("/api/processing-preferences"):
            writes.append(request)

    if hold_owner_read:
        reader.route(owners_url, lambda route: held.append(route))
    reader.on("request", note_write)
    language = reader.locator("#automatic-preferences-output-language")
    new_language = "vi" if language.input_value() == "en" else "en"
    language.select_option(new_language)
    with reader.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/processing-preferences")
        )
    ) as saved:
        save.click()
    outcome = saved.value.json()
    assert (outcome["notes"]["model"], outcome["notes"]["reasoning_effort"]) == (newer, "medium")
    assert outcome["version"] == before["version"] + 1
    expect(reader.locator("#automatic-preferences-notice")).to_contain_text("Đã lưu")
    expect(reader.locator("#automatic-preferences-title")).to_be_focused()
    if hold_owner_read:
        expect(summary).to_have_attribute("data-state", "loading")
        assert len(held) == 1
        expect(save).to_be_disabled()
        expect(summary).not_to_contain_text("Lưu để áp dụng")
        held[0].continue_()
    expect(summary).to_contain_text(f"{newer} · effort medium", timeout=3500)
    expect(summary).not_to_contain_text("Lưu để áp dụng")
    expect(save).to_be_disabled()
    expect(language).to_have_value(new_language)
    reader.locator("#automatic-preferences-form").evaluate("(form) => form.requestSubmit()")
    assert _processing_request(owner, "/api/processing-preferences") == outcome
    assert len(writes) == 1
