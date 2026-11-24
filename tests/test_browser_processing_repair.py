"""Real local Chromium response-order coverage for issue #258 repairs."""

from __future__ import annotations

import re
from contextlib import suppress

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect
from test_browser_asr_model import worker  # noqa: F401
from test_browser_automatic_processing import _open_automatic_processing, _processing_request
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.antigravity import AntigravityProvider
from asr_studio.processing import save_processing_preferences

pytestmark = pytest.mark.browser


def test_older_antigravity_catalog_cannot_undo_new_owner_choice(live, signed_in, monkeypatch):
    original_model, new_model = "gemini-3.1-pro", "gemini-3.1-flash"
    older_catalog = {
        "models": [{"id": model, "display_name": model} for model in (original_model, new_model)],
        "selected_model": original_model,
        "reasoning_effort": "high",
    }
    monkeypatch.setattr(AntigravityProvider, "list_models", lambda self: older_catalog)
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(8000)
    previous = _processing_request(
        page,
        "/api/notes-preferences",
        "PUT",
        {"provider": "antigravity", "model": original_model, "reasoning_effort": "high"},
    )
    assert previous["models"]["antigravity"] == original_model

    provider_url = f"{live.url}/api/providers"
    catalog_url = f"{provider_url}/antigravity/models"

    def fixture_connected(route):
        response = route.fetch()
        data = response.json()
        provider = next(row for row in data["providers"] if row["id"] == "antigravity")
        provider.update(status="connected", account={"label": "Local catalog fixture"})
        route.fulfill(response=response, json=data)

    pending_reads = []
    hold_catalog = False

    def catalog_read(route):
        if hold_catalog:
            pending_reads.append(route)
        else:
            route.fulfill(status=200, json=older_catalog)

    page.route(provider_url, fixture_connected)
    page.route(catalog_url, catalog_read)
    _open_automatic_processing(page)
    owner_tab = page.get_by_role("tab", name="Google Antigravity")
    automation_tab = page.get_by_role("tab", name="Xử lý tự động")
    summary = page.locator("#automatic-preferences-notes-owner")
    expect(summary).to_have_attribute("data-state", "ready")
    expect(summary).to_contain_text(original_model)

    page.keyboard.press("Escape")
    expect(page.locator("#connections-dialog")).to_be_hidden()
    hold_catalog = True
    with page.expect_request(catalog_url):
        page.locator("#settings-open").click()
    owner_tab.click()
    assert len(pending_reads) == 1
    page.locator("#antigravity-model").select_option(new_model)
    page.locator("#antigravity-effort").select_option("medium")
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and response.url.endswith("/api/providers/antigravity/preference")
        )
    ) as saved:
        page.locator("#antigravity-model-save").click()
    assert saved.value.status == 200, saved.value.text()
    assert saved.value.json() == {
        "provider": "antigravity",
        "model": new_model,
        "reasoning_effort": "medium",
    }

    automation_tab.click()
    expect(summary).to_contain_text(new_model)
    expect(page.locator("#automatic-preferences-save")).to_be_enabled()
    with page.expect_response(catalog_url):
        pending_reads[0].fulfill(status=200, json=older_catalog)
    expect(summary).to_have_attribute("data-state", "ready")
    expect(summary).to_contain_text(new_model, timeout=2500)
    expect(summary).to_contain_text("medium")
    expect(page.locator("#automatic-preferences-save")).to_be_enabled()

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/processing-preferences")
        )
    ) as adopted:
        page.locator("#automatic-preferences-save").click()
    assert adopted.value.status == 200
    assert adopted.value.json()["notes"]["model"] == new_model
    assert adopted.value.json()["notes"]["reasoning_effort"] == "medium"


def test_older_openai_preferences_read_cannot_undo_new_owner_choice(live, signed_in):
    original_model, new_model = "gpt-5", "gpt-5.5"
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(8000)
    _processing_request(
        page,
        "/api/notes-preferences",
        "PUT",
        {"provider": "chatgpt", "model": original_model, "reasoning_effort": "default"},
    )
    older_preferences = _processing_request(page, "/api/notes-preferences")
    provider_url = f"{live.url}/api/providers"
    prefs_url = f"{live.url}/api/notes-preferences"
    models_url = f"{provider_url}/chatgpt/models"
    paused_reads = []
    hold_read = False

    def fixture_connected(route):
        response = route.fetch()
        data = response.json()
        provider = next(row for row in data["providers"] if row["id"] == "chatgpt")
        provider.update(status="connected", account={"label": "Local catalog fixture"})
        route.fulfill(response=response, json=data)

    def preference_read(route):
        if hold_read and route.request.method == "GET":
            paused_reads.append(route)
        else:
            route.continue_()

    page.route(provider_url, fixture_connected)
    page.route(prefs_url, preference_read)
    page.route(
        models_url,
        lambda route: route.fulfill(
            status=200,
            json={
                "models": [
                    {"id": model, "display_name": model} for model in (original_model, new_model)
                ],
                "selected_model": original_model,
            },
        ),
    )
    _open_automatic_processing(page)
    summary = page.locator("#automatic-preferences-notes-owner")
    expect(summary).to_have_attribute("data-state", "ready")
    expect(summary).to_contain_text(original_model)
    page.keyboard.press("Escape")
    expect(page.locator("#connections-dialog")).to_be_hidden()
    hold_read = True
    with page.expect_request(lambda request: request.url == prefs_url and request.method == "GET"):
        page.locator("#settings-open").click()
    page.get_by_role("tab", name="OpenAI").click()
    assert len(paused_reads) == 1
    page.locator("#connection-model").select_option(new_model)
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url == prefs_url
    ) as saved:
        page.locator("#connection-model-save").click()
    assert saved.value.status == 200
    assert saved.value.json()["models"]["chatgpt"] == new_model

    page.get_by_role("tab", name="Xử lý tự động").click()
    expect(summary).to_contain_text(new_model)
    paused_reads[0].fulfill(status=200, json=older_preferences)
    expect(summary).to_have_attribute("data-state", "ready")
    expect(summary).to_contain_text(new_model, timeout=2500)
    expect(page.locator("#automatic-preferences-save")).to_be_disabled()


@pytest.mark.parametrize("provider", ["chatgpt", "antigravity"])
def test_pending_first_provider_status_never_claims_disconnected(live, signed_in, provider):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(5000)
    save_processing_preferences(
        live.settings.data_dir,
        notes_provider=provider,
        notes_model="workflow-fixture-model",
        notes_reasoning_effort="default",
    )
    url = f"{live.url}/api/providers"
    held = []
    page.route(url, lambda route: held.append(route))
    with page.expect_request(url):
        page.reload(wait_until="domcontentloaded")
    with page.expect_request(url):
        _open_automatic_processing(page)
    help_text = page.locator("#automatic-preferences-provider-help")
    owner = page.locator("#automatic-preferences-notes-owner")
    try:
        expect(help_text).to_contain_text("Đang kiểm tra")
        expect(owner).to_have_attribute("data-state", "loading", timeout=2000)
        expect(owner).not_to_contain_text(re.compile("kết nối lại|chưa chọn model", re.I))
    finally:
        for route in reversed(held):
            with suppress(PlaywrightError):
                route.continue_()
        page.unroute(url)
    expect(owner).to_have_attribute("data-state", "disconnected")


def test_failed_chatgpt_catalog_auth_recovery_settles_without_losing_owner(live, signed_in):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(5000)
    _processing_request(
        page,
        "/api/notes-preferences",
        "PUT",
        {"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "medium"},
    )
    provider_url = f"{live.url}/api/providers"
    catalog_url = f"{provider_url}/chatgpt/models"
    catalog_calls = []

    def providers(route):
        if catalog_calls:
            route.fulfill(status=503, json={"detail": {"code": "unavailable"}})
            return
        response = route.fetch()
        data = response.json()
        provider = next(row for row in data["providers"] if row["id"] == "chatgpt")
        provider.update(status="connected", account={"label": "Local catalog fixture"})
        route.fulfill(response=response, json=data)

    def catalog(route):
        catalog_calls.append(route.request.url)
        route.fulfill(status=409, json={"detail": {"code": "reconnect_required"}})

    page.route(provider_url, providers)
    page.route(catalog_url, catalog)
    with page.expect_response(
        lambda response: response.url == provider_url and response.status == 503
    ):
        _open_automatic_processing(page)
    summary = page.locator("#automatic-preferences-notes-owner")
    expect(summary).to_have_attribute("data-state", "error", timeout=2000)
    expect(summary).to_contain_text("gpt-6.1-sol")
    expect(summary).to_contain_text("medium")
    expect(summary).not_to_contain_text("Đang tải catalog")
    assert len(catalog_calls) == 1


def test_invalid_initial_preferences_response_runs_only_one_queued_retry(live, signed_in):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(5000)
    url = f"{live.url}/api/processing-preferences"
    reads = []

    def read(route):
        reads.append(route)
        if len(reads) == 2:
            route.fulfill(status=200, json={"unexpected": "second invalid preferences response"})
        elif len(reads) > 2:
            route.continue_()

    page.route(url, read)
    with page.expect_request(url):
        page.locator("#settings-open").click()
    automation = page.get_by_role("tab", name="Xử lý tự động")
    automation.click()
    assert len(reads) == 1
    reads[0].fulfill(status=200, json={"unexpected": "first invalid preferences response"})
    expect(page.locator("#automatic-preferences-form")).to_have_attribute("aria-busy", "false")
    assert len(reads) == 2
    expect(page.locator("#automatic-preferences-error")).to_be_visible()
    page.get_by_role("tab", name="ASR Model").click()
    automation.click()
    expect(page.locator("#automatic-preferences-version")).to_have_text("v1")
    assert len(reads) == 3


@pytest.mark.parametrize("malformed_followup", [False, True])
def test_invalid_patch_acknowledgement_refreshes_without_replaying_the_write(
    live,
    signed_in,
    worker,  # noqa: F811
    malformed_followup,  # noqa: F811
):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(5000)
    _open_automatic_processing(page)
    url = f"{live.url}/api/processing-preferences"
    patches = []
    next_read_invalid = False

    def hold_patch(route):
        nonlocal next_read_invalid
        if route.request.method == "PATCH":
            patches.append(route)
        elif next_read_invalid:
            next_read_invalid = False
            route.fulfill(status=200, json={"unexpected": "invalid reconciliation response"})
        else:
            route.continue_()

    page.route(url, hold_patch)
    language = page.locator("#automatic-preferences-asr-language")
    language.select_option("en")
    with page.expect_request(lambda request: request.url == url and request.method == "PATCH"):
        page.locator("#automatic-preferences-save").click()
    assert len(patches) == 1
    committed = page.request.patch(
        url,
        headers={"Origin": live.settings.public_url},
        data=patches[0].request.post_data_json,
    )
    assert committed.status == 200 and committed.json()["version"] == 2

    page.get_by_role("tab", name="ASR Model").click()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/asr-models/default") and response.request.method == "PUT"
        )
    ) as owner:
        page.locator("#asr-default-select").select_option("whisper-large-v3")
    assert owner.value.status == 200
    page.get_by_role("tab", name="Xử lý tự động").click()
    next_read_invalid = malformed_followup
    error = page.locator("#automatic-preferences-error")
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "GET", timeout=2000
    ):
        patches[0].fulfill(
            status=200,
            json={"unexpected": "write committed but acknowledgement cannot be read"},
        )
    if malformed_followup:
        expect(page.locator("#automatic-preferences-form")).to_have_attribute("aria-busy", "false")
        expect(error).to_contain_text("chưa xác nhận")
        page.get_by_role("tab", name="ASR Model").click()
        page.get_by_role("tab", name="Xử lý tự động").click()
    expect(page.locator("#automatic-preferences-version")).to_have_text("v3", timeout=2000)
    expect(language).to_have_value("en")
    expect(error).to_contain_text("chưa xác nhận")
    expect(page.locator("#automatic-preferences-notice")).not_to_contain_text("Đã lưu")
    assert len(patches) == 1
    assert _processing_request(page, "/api/processing-preferences")["version"] == 3
