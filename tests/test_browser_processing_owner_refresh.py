"""Two authenticated local clients and real stored Notes owner changes."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect
from test_browser_automatic_processing import _open_automatic_processing, _processing_request
from test_browser_history import live as live  # noqa: F401
from test_browser_history import signed_in as signed_in

from asr_studio.antigravity import AntigravityProvider
from asr_studio.processing import save_processing_preferences

pytestmark = pytest.mark.browser
pytest_plugins = ("test_browser_asr_model",)

OLD_MODEL = "gpt-6.1-sol"
NEW_MODEL = "gpt-5.6-luna"


def _owner_save(page, model, effort):
    return _processing_request(
        page,
        "/api/notes-preferences",
        "PUT",
        {"provider": "chatgpt", "model": model, "reasoning_effort": effort},
    )


def _enable_local_catalog(page, live, handler=None):
    provider_url = f"{live.url}/api/providers"
    catalog_url = f"{provider_url}/chatgpt/models"
    calls = []

    def status(route):
        response = route.fetch()
        data = response.json()
        openai = next(row for row in data["providers"] if row["id"] == "chatgpt")
        openai.update(status="connected", account={"label": "Local catalog fixture"})
        route.fulfill(response=response, json=data)

    def catalog(route):
        calls.append(route.request.url)
        if handler is None:
            _catalog_response(route, OLD_MODEL)
        else:
            handler(route)

    page.route(provider_url, status)
    page.route(catalog_url, catalog)
    return catalog_url, calls


def _catalog_response(route, selected_model):
    route.fulfill(
        status=200,
        json={
            "models": [
                {"id": model, "display_name": model, "reasoning_efforts": ["low", "high"]}
                for model in (OLD_MODEL, NEW_MODEL)
            ],
            "selected_model": selected_model,
        },
    )


def test_initial_catalog_cannot_invent_owner_effort_after_other_client_save(live, signed_in):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    assert "chatgpt" not in _processing_request(owner, "/api/notes-preferences")["models"]
    save_processing_preferences(
        live.settings.data_dir,
        asr_choice="auto",
        asr_language="vi",
        notes_provider="chatgpt",
        notes_model=NEW_MODEL,
        notes_reasoning_effort="high",
        notes_output_language="en",
    )
    assert "chatgpt" not in _processing_request(owner, "/api/notes-preferences")["models"]
    held = []
    catalog_url, _ = _enable_local_catalog(reader, live, held.append)
    with reader.expect_request(catalog_url):
        _open_automatic_processing(reader)
    assert len(held) == 1
    committed = _owner_save(owner, NEW_MODEL, "high")
    assert (committed["models"]["chatgpt"], committed["reasoning_efforts"]["chatgpt"]) == (
        NEW_MODEL,
        "high",
    )
    with reader.expect_response(catalog_url):
        _catalog_response(held[0], NEW_MODEL)

    summary = reader.locator("#automatic-preferences-notes-owner")
    expect(reader.locator(f'#connection-model option[value="{NEW_MODEL}"]')).to_have_count(1)
    expect(summary).not_to_have_attribute("data-state", "loading")
    expect(summary).not_to_contain_text("effort default")
    expect(reader.locator("#automatic-preferences-save")).to_be_disabled()


@pytest.mark.parametrize("local_draft", [False, True])
def test_processing_reentry_reconciles_a_second_clients_saved_owner(live, signed_in, local_draft):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    assert _owner_save(owner, OLD_MODEL, "low")["reasoning_efforts"]["chatgpt"] == "low"
    _, catalog_calls = _enable_local_catalog(reader, live)
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-notes-owner")
    save = reader.locator("#automatic-preferences-save")
    language = reader.locator("#automatic-preferences-output-language")
    expect(summary).to_contain_text(f"{OLD_MODEL} · effort low")
    expect(save).to_be_disabled()
    if local_draft:
        language.select_option("vi")
        expect(save).to_be_enabled()

    reader.get_by_role("tab", name="Thiết bị").click()
    assert _owner_save(owner, NEW_MODEL, "high")["reasoning_efforts"]["chatgpt"] == "high"
    current = _processing_request(owner, "/api/processing-preferences")
    assert current["notes"]["model"] == NEW_MODEL
    assert current["notes"]["reasoning_effort"] == "high"
    catalog_count = len(catalog_calls)
    reader.get_by_role("tab", name="Xử lý tự động").click()

    expect(summary).to_contain_text(f"{NEW_MODEL} · effort high", timeout=3500)
    expect(summary).not_to_contain_text("khác recipe workflow")
    assert len(catalog_calls) == catalog_count
    if local_draft:
        expect(language).to_have_value("vi")
        expect(save).to_be_enabled()
    else:
        expect(save).to_be_disabled()
    assert _processing_request(owner, "/api/processing-preferences") == current


def test_processing_reentry_shows_real_antigravity_drift_until_explicit_adoption(
    live, signed_in, monkeypatch
):
    older = "gemini-3.1-pro"
    newer = "gemini-3.1-flash"
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

    reader.get_by_role("tab", name="Thiết bị").click()
    monkeypatch.setattr(
        AntigravityProvider,
        "list_models",
        lambda self: {"models": [{"id": newer, "display_name": newer}]},
    )
    assert _processing_request(
        owner,
        "/api/providers/antigravity/preference",
        "PUT",
        {"model": newer, "reasoning_effort": "medium"},
    ) == {"provider": "antigravity", "model": newer, "reasoning_effort": "medium"}
    assert _processing_request(owner, "/api/processing-preferences")["notes"]["model"] == older
    reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(summary).to_contain_text(f"{newer} · effort medium", timeout=3500)
    expect(summary).to_contain_text("khác recipe workflow")
    expect(save).to_be_enabled()

    with reader.expect_response(
        lambda response: (
            response.url.endswith("/api/processing-preferences")
            and response.request.method == "PATCH"
        )
    ) as adopted:
        save.click()
    assert adopted.value.status == 200
    assert adopted.value.json()["notes"]["model"] == newer
    assert adopted.value.json()["notes"]["reasoning_effort"] == "medium"
    expect(save).to_be_disabled()


def test_processing_reentry_blocks_false_save_while_local_owner_read_is_pending(live, signed_in):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    _owner_save(owner, OLD_MODEL, "low")
    _, catalog_calls = _enable_local_catalog(reader, live)
    _open_automatic_processing(reader)
    save = reader.locator("#automatic-preferences-save")
    summary = reader.locator("#automatic-preferences-notes-owner")
    expect(summary).to_contain_text(f"{OLD_MODEL} · effort low")
    expect(save).to_be_disabled()
    reader.get_by_role("tab", name="Thiết bị").click()
    _owner_save(owner, NEW_MODEL, "high")
    catalog_count = len(catalog_calls)

    owners_url = f"{live.url}/api/notes-preferences"
    workflows_url = f"{live.url}/api/processing-preferences"
    held = []
    reader.route(owners_url, lambda route: held.append(route))
    with (
        reader.expect_request(owners_url, timeout=3000),
        reader.expect_response(workflows_url, timeout=3000) as workflow_response,
    ):
        reader.get_by_role("tab", name="Xử lý tự động").click()
    assert len(held) == 1
    assert workflow_response.value.json()["notes"]["model"] == NEW_MODEL
    expect(save).to_be_disabled()
    expect(summary).to_have_attribute("data-state", "loading")
    expect(summary).not_to_contain_text("Lưu để áp dụng cài đặt sở hữu")

    held[0].continue_()
    expect(summary).to_contain_text(f"{NEW_MODEL} · effort high")
    expect(save).to_be_disabled()
    assert len(catalog_calls) == catalog_count


def test_in_flight_automation_owner_read_is_overtaken_by_confirmed_owner_save(live, signed_in):
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    _owner_save(reader, OLD_MODEL, "low")
    _, catalog_calls = _enable_local_catalog(reader, live)
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-notes-owner")
    save = reader.locator("#automatic-preferences-save")
    expect(summary).to_contain_text(f"{OLD_MODEL} · effort low")
    expect(save).to_be_disabled()

    reader.get_by_role("tab", name="Thiết bị").click()
    url = f"{live.url}/api/notes-preferences"
    older = _processing_request(reader, "/api/notes-preferences")
    held = []

    def hold_one(route):
        if route.request.method == "GET" and not held:
            held.append(route)
        else:
            route.continue_()

    reader.route(url, hold_one)
    with reader.expect_request(url, timeout=3000):
        reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(summary).to_have_attribute("data-state", "loading")
    assert len(held) == 1
    reader.get_by_role("tab", name="OpenAI").click()
    reader.locator("#connection-model").select_option(NEW_MODEL)
    reader.locator("#connection-effort").select_option("high")
    with reader.expect_response(
        lambda response: response.url == url and response.request.method == "PUT"
    ) as owner_saved:
        reader.locator("#connection-model-save").click()
    assert owner_saved.value.status == 200
    assert owner_saved.value.json()["reasoning_efforts"]["chatgpt"] == "high"

    reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(save).to_be_disabled()
    summary.evaluate("""(element) => {
        window.ownerMessages = [];
        new MutationObserver(() => window.ownerMessages.push(element.textContent))
            .observe(element, {childList: true, subtree: true, characterData: true});
    }""")
    held[0].fulfill(status=200, json=older)
    expect(summary).to_contain_text(f"{NEW_MODEL} · effort high")
    expect(save).to_be_disabled()
    assert not any(
        "khác recipe workflow" in text for text in reader.evaluate("window.ownerMessages")
    )
    assert len(catalog_calls) == 1


@pytest.mark.parametrize("status", [200, 503])
def test_processing_owner_read_failure_is_explicit_and_reentry_recovers(live, signed_in, status):
    _, owner = signed_in()
    _, reader = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    _owner_save(owner, OLD_MODEL, "low")
    _, catalog_calls = _enable_local_catalog(reader, live)
    _open_automatic_processing(reader)
    summary = reader.locator("#automatic-preferences-notes-owner")
    save = reader.locator("#automatic-preferences-save")
    expect(summary).to_contain_text(f"{OLD_MODEL} · effort low")
    expect(save).to_be_disabled()

    reader.get_by_role("tab", name="Thiết bị").click()
    _owner_save(owner, NEW_MODEL, "high")
    url = f"{live.url}/api/notes-preferences"
    reader.route(url, lambda route: route.fulfill(status=status, json={}))
    reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(summary).to_have_attribute("data-state", "error")
    expect(summary).not_to_contain_text("Lưu để áp dụng cài đặt sở hữu")
    expect(save).to_be_disabled()

    reader.unroute(url)
    reader.get_by_role("tab", name="Thiết bị").click()
    reader.get_by_role("tab", name="Xử lý tự động").click()
    expect(summary).to_contain_text(f"{NEW_MODEL} · effort high")
    expect(summary).to_have_attribute("data-state", "ready")
    expect(save).to_be_disabled()
    assert len(catalog_calls) == 1
