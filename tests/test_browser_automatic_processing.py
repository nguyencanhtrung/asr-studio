"""Real-browser coverage for automatic-processing preferences and workflow actions."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from itertools import product

import pytest
from notes_helpers import seed_cited_notes
from playwright.sync_api import expect
from test_browser_history import live as live  # noqa: F401
from test_browser_history import open_page  # noqa: F401
from test_browser_history import signed_in as signed_in

from asr_studio.extension_ingest import publish_ready_derivative
from asr_studio.processing import cancel_workflow
from asr_studio.storage import database_path, initialize

pytestmark = pytest.mark.browser
pytest_plugins = ("test_browser_asr_model",)


def _processing_request(page, path, method="GET", payload=None):
    outcome = page.evaluate(
        """async ({path, method, payload}) => {
            const options = {method};
            if (payload !== null) {
                options.headers = {'Content-Type': 'application/json'};
                options.body = JSON.stringify(payload);
            }
            const response = await fetch(path, options);
            return [response.status, await response.json()];
        }""",
        {"path": path, "method": method, "payload": payload},
    )
    assert outcome[0] == 200, outcome
    return outcome[1]


def _open_automatic_processing(page, language="vi"):
    page.locator("#settings-open").click()
    label = "Xử lý tự động" if language == "vi" else "Automatic processing"
    tab = page.get_by_role("tab", name=label)
    if tab.is_visible():
        tab.click()
    else:
        page.locator("#settings-tab-ai").click()
        page.locator("[data-settings-overview='processing'] .provider-overview-row").click()
    expect(page.locator("#automatic-preferences-form")).to_have_attribute("aria-busy", "false")


def _seed_ready_processing_meeting(
    live, title: str = "Automatic processing browser fixture"
) -> str:
    """Create a real ready extension recording; the app creates its durable workflow on handoff."""
    initialize(live.settings.data_dir)
    meeting_id = uuid.uuid4().hex
    recording_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO meetings(id, title, language_hint) VALUES (?, ?, 'vi')",
            (meeting_id, title),
        )
        connection.execute(
            "INSERT INTO extension_recording_sessions("
            "id, owner_id, client_recording_id, capture_epoch, payload_fingerprint, "
            "language_hint, language_hint_omitted, mime_type, state, epoch, "
            "next_sequence, next_offset) "
            "VALUES (?, ?, ?, ?, ?, 'vi', 1, 'audio/webm;codecs=opus', 'archived', 1, 0, 0)",
            (
                session_id,
                "browser-owner-" + uuid.uuid4().hex,
                "browser-client",
                "browser-capture",
                "f" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO recordings("
            "id, meeting_id, extension_session_id, storage_key, content_type, duration_seconds) "
            "VALUES (?, ?, ?, NULL, NULL, NULL)",
            (recording_id, meeting_id, session_id),
        )
        connection.commit()
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"recordings/{session_id}.m4a",
            duration_seconds=12.0,
        )
        connection.commit()
    return meeting_id


def test_settings_show_explicit_recipe_and_waiting_reason_in_vi_then_en_dark(live, signed_in):
    meeting_id = _seed_ready_processing_meeting(live)
    context, page = signed_in(color_scheme="dark", viewport={"width": 1024, "height": 860})
    page = open_page(context, live, f"/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_have_text("Automatic processing browser fixture")
    expect(page.locator("#automatic-processing-panel")).to_be_visible()
    expect(page.locator("#automatic-processing-steps")).to_contain_text("Transcript")
    expect(page.locator("#automatic-processing-steps")).to_contain_text("Notes")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"

    page.locator("#settings-open").click()
    page.get_by_role("tab", name="Xử lý tự động").click()
    title = page.locator("#automatic-preferences-title")
    version = page.locator("#automatic-preferences-version")
    expect(title).to_be_visible()
    expect(version).to_have_text("v1")
    expect(version).to_be_visible()
    title_box, version_box = title.bounding_box(), version.bounding_box()
    assert title_box["y"] < version_box["y"] + version_box["height"]
    assert version_box["y"] < title_box["y"] + title_box["height"]
    assert version_box["x"] >= title_box["x"] + title_box["width"]
    expect(page.locator("#automatic-preferences-provider")).to_have_value("")
    expect(page.locator("#automatic-preferences-provider-help")).to_contain_text("Transcript")
    expect(page.locator("#automatic-preferences-save")).to_be_disabled()
    form = page.locator("#automatic-preferences-form")
    comboboxes = form.get_by_role("combobox")
    assert comboboxes.count() == 3
    assert form.get_by_role("combobox", name="Ngôn ngữ transcript").count() == 1
    assert form.get_by_role("combobox", name="Provider notes").count() == 1
    assert form.get_by_role("combobox", name="Ngôn ngữ đầu ra notes").count() == 1
    expect(page.locator("#automatic-preferences-asr-owner")).to_be_visible()
    expect(page.locator("#automatic-preferences-asr-owner")).not_to_have_text("")
    asr_link = page.get_by_role("link", name="ASR Model")
    expect(asr_link).to_have_count(1)
    expect(asr_link).to_have_attribute("href", "#settings-panel-asr")
    provider_link = page.get_by_role("link", name="AI Provider")
    expect(provider_link).to_have_count(1)
    expect(provider_link).to_have_attribute("href", "#settings-panel-ai")

    page.get_by_role("tab", name="Giao diện").click()
    with page.expect_navigation():
        page.get_by_role("radio", name="English").check()
    page.get_by_role("tab", name="Automatic processing").click()
    expect(page.locator("#automatic-preferences-provider-help")).to_contain_text("The transcript")
    expect(page.locator("#automatic-preferences-save")).to_be_disabled()
    context.close()


@pytest.mark.parametrize(
    ("width", "theme", "language"),
    list(product((1440, 390), ("light", "dark"), ("vi", "en"))),
    ids=[
        f"{'desktop' if width == 1440 else 'mobile'}-{language}-{theme}"
        for width, theme, language in product((1440, 390), ("light", "dark"), ("vi", "en"))
    ],
)
def test_stage_policy_preserves_dependent_draft_and_focuses_after_conflict(
    live, signed_in, width, theme, language
):
    context, page = signed_in(
        color_scheme=theme,
        viewport={"width": width, "height": 860},
    )
    try:
        page.evaluate("(lang) => localStorage.setItem('asr-studio:lang', lang)", language)
        page.reload(wait_until="domcontentloaded")
        _open_automatic_processing(page, language=language)

        form = page.locator("#processing-stage-policy-form")
        expect(form).to_have_attribute("aria-busy", "false")
        asr = page.locator("#processing-stage-policy-asr")
        diarization = page.locator("#processing-stage-policy-diarization")
        notes = page.locator("#processing-stage-policy-notes")
        save = page.locator("#processing-stage-policy-save")
        reload_button = page.locator("#processing-stage-policy-reload")
        status = page.locator("#processing-stage-policy-status")
        error = page.locator("#processing-stage-policy-error")
        notice = page.locator("#processing-stage-policy-notice")

        expect(asr).to_be_checked()
        expect(diarization).to_be_checked()
        expect(notes).to_be_checked()
        expect(status).to_contain_text(
            "Máy chủ chưa cho phép chạy stage. Bản ghi không được xem là đã xử lý."
            if language == "vi"
            else "The server does not allow stages to run. Do not treat the recording as processed."
        )
        advanced = page.locator(".stage-policy-advanced")
        advanced.locator("summary").click()
        expect(page.locator("#processing-stage-policy-version")).to_have_text("v1")
        expect(page.locator("#processing-stage-policy-format")).to_have_text("v1")
        expect(page.locator("#processing-stage-policy-admission")).to_contain_text("disabled")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        baseline = _processing_request(page, "/api/processing-stage-policy/v1")

        # The dependent values stay checked in the draft while ASR makes them
        # ineffective. A separate real save advances the shared version so the
        # UI's next PUT receives the backend's actual 409 conflict.
        asr.uncheck()
        expect(diarization).to_be_disabled()
        expect(notes).to_be_disabled()
        expect(diarization).to_be_checked()
        expect(notes).to_be_checked()
        expect(notice).to_be_hidden()
        _processing_request(
            page,
            "/api/processing-stage-policy/v1",
            "PUT",
            {
                "expected_preference_version": baseline["preference_version"],
                "format_version": 1,
                "asr": True,
                "diarization": True,
                "notes": True,
            },
        )

        save.click()
        expect(error).to_be_visible()
        expect(notice).to_be_hidden()
        expect(status).to_be_focused()
        expect(status).to_contain_text(
            "Cài đặt đã đổi ở nơi khác" if language == "vi" else "The settings changed elsewhere"
        )
        expect(asr).not_to_be_checked()
        expect(diarization).to_be_checked()
        expect(notes).to_be_checked()

        # Conflict does not overwrite the draft. Reload is explicit, then the
        # same draft can be reviewed and saved against the new version.
        reload_button.click()
        expect(form).to_have_attribute("aria-busy", "false")
        expect(asr).not_to_be_checked()
        expect(diarization).to_be_checked()
        expect(notes).to_be_checked()
        expect(save).to_be_enabled()
        save.click()
        expected_notice = (
            "Đã lưu công tắc"
            if language == "vi"
            else "Processing switches for new Extension recordings saved"
        )
        expect(notice).to_contain_text(expected_notice)
        expect(status).to_be_focused()
        expect(error).to_be_hidden()
        expect(status).to_contain_text(
            "Máy chủ chưa cho phép chạy stage"
            if language == "vi"
            else "The server does not allow stages to run"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        final = _processing_request(page, "/api/processing-stage-policy/v1")
        assert final["policy"] == {"asr": False, "diarization": True, "notes": True}
    finally:
        context.close()


def test_stage_policy_save_keeps_focus_when_user_moves_during_request(live, signed_in, monkeypatch):
    from asr_studio import processing_routes

    entered, release = threading.Event(), threading.Event()
    save_policy = processing_routes.save_processing_stage_policy

    def delayed_save(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=15), "test did not release the real stage-policy save"
        return save_policy(*args, **kwargs)

    monkeypatch.setattr(processing_routes, "save_processing_stage_policy", delayed_save)
    context, page = signed_in(viewport={"width": 1440, "height": 900})
    try:
        _open_automatic_processing(page)
        page.locator("#processing-stage-policy-asr").uncheck()
        page.locator("#processing-stage-policy-save").click()
        assert entered.wait(timeout=5)
        summary = page.locator(".stage-policy-advanced summary")
        summary.focus()
        summary.press("Enter")
        expect(summary).to_be_focused()
        release.set()
        expect(page.locator("#processing-stage-policy-notice")).to_contain_text("Đã lưu công tắc")
        expect(summary).to_be_focused()
        expect(page.locator("#processing-stage-policy-status")).to_contain_text(
            "Máy chủ chưa cho phép"
        )
        actual = _processing_request(page, "/api/processing-stage-policy/v1")
        assert actual["policy"]["asr"] is False
    finally:
        release.set()
        context.close()


@pytest.mark.parametrize("draft_before_leaving", [False, True])
def test_switching_back_after_own_asr_default_save_uses_the_new_version(
    live, signed_in, worker, draft_before_leaving
):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(8000)
    _open_automatic_processing(page)
    before = _processing_request(page, "/api/processing-preferences")
    version = page.locator("#automatic-preferences-version")
    expect(version).to_have_text(f"v{before['version']}")
    language = page.locator("#automatic-preferences-asr-language")
    if draft_before_leaving:
        language.select_option("en")

    page.locator("#settings-panel-processing [data-settings-tab='asr']").click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith("/api/asr-models/default")
        )
    ) as changed:
        page.locator("#asr-default-select").select_option("whisper-large-v3")
    assert changed.value.status == 200, changed.value.text()
    after = _processing_request(page, "/api/processing-preferences")
    assert after["version"] == before["version"] + 1
    assert after["asr"]["choice"] == "whisper-large-v3"

    page.get_by_role("tab", name="Xử lý tự động").click()
    expect(page.locator("#automatic-preferences-form")).to_have_attribute("aria-busy", "false")
    if draft_before_leaving:
        expect(language).to_have_value("en")
    else:
        language.select_option("en")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/processing-preferences")
        )
    ) as next_save:
        page.locator("#automatic-preferences-save").click()
    observed = next_save.value
    assert observed.status == 200, (observed.request.post_data_json, observed.json())
    assert observed.request.post_data_json["expected_version"] == after["version"]
    expect(version).to_have_text(f"v{after['version'] + 1}")


def test_owner_refresh_adopts_untouched_output_language_and_keeps_local_language_draft(
    live, signed_in, worker
):
    _, page = signed_in(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    page.set_default_timeout(8000)
    _open_automatic_processing(page)
    before = _processing_request(page, "/api/processing-preferences")
    page.locator("#automatic-preferences-asr-language").select_option("en")

    changed_elsewhere = page.request.patch(
        f"{live.url}/api/processing-preferences",
        headers={"Origin": live.settings.public_url},
        data={
            "expected_version": before["version"],
            "asr_language": "vi",
            "notes_provider": None,
            "notes_output_language": "vi",
        },
    )
    assert changed_elsewhere.status == 200, changed_elsewhere.text()

    page.locator("#settings-panel-processing [data-settings-tab='asr']").click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith("/api/asr-models/default")
        )
    ) as changed:
        page.locator("#asr-default-select").select_option("whisper-large-v3")
    assert changed.value.status == 200, changed.value.text()
    latest = _processing_request(page, "/api/processing-preferences")
    assert latest["notes"]["output_language"] == "vi"

    page.get_by_role("tab", name="Xử lý tự động").click()
    expect(page.locator("#automatic-preferences-version")).to_have_text(f"v{latest['version']}")
    expect(page.locator("#automatic-preferences-asr-language")).to_have_value("en")
    expect(page.locator("#automatic-preferences-output-language")).to_have_value("vi")


def test_cancel_success_restores_focus_and_cannot_resurrect_workflow(live, signed_in):
    meeting_id = _seed_ready_processing_meeting(live, "Cancel automatic workflow")
    context, page = signed_in(viewport={"width": 1024, "height": 860})
    page = open_page(context, live, f"/meetings/{meeting_id}")
    cancel = page.locator("#automatic-processing-cancel")
    expect(cancel).to_be_visible()
    cancel.focus()
    expect(cancel).to_be_focused()
    cancel.click()
    expect(page.locator("#automatic-processing-message")).to_contain_text("Đã hủy xử lý tự động")
    expect(page.locator("#automatic-processing-cancel")).to_be_hidden()
    expect(page.locator("#automatic-processing-title")).to_be_focused()
    expect(page.locator("#status-transcript")).to_have_attribute("data-state", "warning")
    context.close()


def test_manual_generation_and_notes_revision_keep_history_status_over_waiting_automatic_workflow(
    live, signed_in
):
    meeting_id = _seed_ready_processing_meeting(live, "Manual status precedence")
    source_meeting_id, _revision, _snapshot, _segments = seed_cited_notes(
        live.settings.data_dir, title="Manual status source"
    )
    context, page = signed_in(viewport={"width": 1024, "height": 860})
    source_meeting = context.request.get(f"{live.url}/api/meetings/{source_meeting_id}").json()
    source_notes = context.request.get(f"{live.url}/api/meetings/{source_meeting_id}/notes").json()
    for revision in source_notes["revisions"]:
        revision["meeting_id"] = meeting_id

    def meeting_with_manual_result(route):
        payload = route.fetch().json()
        payload["transcription"] = {**source_meeting["transcription"], "task": None}
        route.fulfill(json=payload)

    context.route(f"{live.url}/api/meetings/{meeting_id}", meeting_with_manual_result)
    context.route(
        f"{live.url}/api/meetings/{meeting_id}/notes",
        lambda route: route.fulfill(json=source_notes),
    )
    page = open_page(context, live, f"/meetings/{meeting_id}")
    expect(page.locator("#status-transcript")).to_have_attribute("data-state", "done")
    expect(page.locator("#status-notes")).to_have_attribute("data-state", "done")
    context.close()


def test_active_manual_notes_status_keeps_history_status_over_waiting_automatic_workflow(
    live, signed_in
):
    meeting_id = _seed_ready_processing_meeting(live, "Active manual notes status")
    source_meeting_id, _revision, _snapshot, _segments = seed_cited_notes(
        live.settings.data_dir, title="Active notes source"
    )
    context, page = signed_in(viewport={"width": 1024, "height": 860})
    source_meeting = context.request.get(f"{live.url}/api/meetings/{source_meeting_id}").json()
    source_notes = context.request.get(f"{live.url}/api/meetings/{source_meeting_id}/notes").json()
    source_notes["task"] = {**source_notes["task"], "status": "running"}

    def meeting_with_manual_task(route):
        payload = route.fetch().json()
        payload["transcription"] = {**source_meeting["transcription"], "task": None}
        route.fulfill(json=payload)

    context.route(f"{live.url}/api/meetings/{meeting_id}", meeting_with_manual_task)
    context.route(
        f"{live.url}/api/meetings/{meeting_id}/notes",
        lambda route: route.fulfill(json=source_notes),
    )
    page = open_page(context, live, f"/meetings/{meeting_id}")
    expect(page.locator("#status-notes")).to_have_attribute("data-state", "active")
    context.close()


def test_stale_apply_settings_failure_keeps_error_focus_and_status(live, signed_in):
    meeting_id = _seed_ready_processing_meeting(live, "Stale settings workflow")
    context, page = signed_in(viewport={"width": 1024, "height": 860})
    page = open_page(context, live, f"/meetings/{meeting_id}")
    cancel_workflow(
        live.settings.data_dir,
        meeting_id,
        request_id="browser-stale-cancel",
        expected_epoch=1,
    )
    apply = page.locator("#automatic-processing-apply-settings")
    expect(apply).to_be_visible()
    apply.focus()
    apply.click()
    expect(page.locator("#automatic-processing-error")).to_contain_text("Workflow")
    expect(page.locator("#automatic-processing-title")).to_be_focused()
    expect(page.locator("#automatic-processing-panel")).to_have_attribute("aria-busy", "false")
    context.close()


@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_preference_save_failure_then_success_preserves_focus_and_status(
    live, signed_in, tmp_path, language, theme
):
    import os

    from test_browser_history import _prepare_browser_asr_model

    from asr_studio.runtime import RuntimeProfile, write_runtime_profile
    from asr_studio.worker import acquire_worker_lock, write_heartbeat

    model = _prepare_browser_asr_model(tmp_path / "model")
    lock = acquire_worker_lock(live.settings.data_dir)
    assert lock is not None
    write_runtime_profile(live.settings.data_dir, RuntimeProfile("cpu", "int8", model))
    write_heartbeat(live.settings.data_dir, "idle")
    context, page = signed_in(color_scheme=theme, viewport={"width": 1024, "height": 860})
    try:
        page.evaluate("lang => localStorage.setItem('asr-studio:lang',lang)", language)
        page.reload()
        page.locator("#settings-open").click()
        page.get_by_role(
            "tab", name="Xử lý tự động" if language == "vi" else "Automatic processing"
        ).click()
        expect(page.locator("#automatic-preferences-asr-language")).to_have_value("vi")
        form = page.locator("#automatic-preferences-form")
        expect(form).to_have_attribute("aria-busy", "false")
        notice = page.locator("#automatic-preferences-notice")
        expect(notice).not_to_contain_text("[object Object]")
        page.locator("#automatic-preferences-asr-language").select_option("en")
        save = page.locator("#automatic-preferences-save")
        expect(save).to_be_enabled()
        held = []

        def hold_save(route):
            if route.request.method == "PATCH":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/processing-preferences", hold_save)
        save.focus()
        page.keyboard.press("Enter")
        expect(save).to_be_disabled()
        expect(form).to_have_attribute("aria-busy", "true")
        assert held
        held[0].fulfill(status=503, json={"detail": {"code": "unavailable"}})
        expect(page.locator("#automatic-preferences-error")).to_be_visible()
        expect(save).to_be_enabled()
        expect(save).to_be_focused()
        expect(form).to_have_attribute("aria-busy", "false")
        expect(notice).not_to_contain_text("[object Object]")
        page.unroute("**/api/processing-preferences", hold_save)
        processing_reads = []

        def record_processing_read(request):
            if request.method == "GET" and request.url.endswith("/api/processing-preferences"):
                processing_reads.append(request)

        page.on("request", record_processing_read)
        page.keyboard.press("Enter")
        expect(notice).to_have_text(
            "Đã lưu cài đặt xử lý tự động."
            if language == "vi"
            else "Automatic-processing settings saved."
        )
        assert processing_reads == []
        expect(page.locator("#automatic-preferences-title")).to_be_focused()
        expect(save).to_be_disabled()
        saved = page.evaluate(
            "async () => (await (await fetch('/api/processing-preferences')).json())"
        )
        assert saved["asr"]["language"] == "en"
        assert saved["notes"]["provider"] is None
    finally:
        context.close()
        os.close(lock)


@pytest.mark.parametrize("first_form", ["stage", "workflow"])
def test_stage_and_workflow_saves_refresh_shared_version_without_losing_drafts(
    live, signed_in, worker, first_form
):
    context, page = signed_in(viewport={"width": 1440, "height": 900})
    try:
        _open_automatic_processing(page)
        stage_asr = page.locator("#processing-stage-policy-asr")
        language = page.locator("#automatic-preferences-asr-language")
        expect(page.locator("#processing-stage-policy-form")).to_have_attribute(
            "aria-busy", "false"
        )
        stage_asr.uncheck()
        language.select_option("en")
        forms = [first_form, "workflow" if first_form == "stage" else "stage"]
        for form in forms:
            if form == "stage":
                expect(stage_asr).not_to_be_checked()
                page.locator("#processing-stage-policy-save").click()
                expect(page.locator("#processing-stage-policy-notice")).to_contain_text(
                    "Đã lưu công tắc"
                )
                expect(page.locator("#processing-stage-policy-error")).to_be_hidden()
                expect(page.locator("#processing-stage-policy-status")).to_be_focused()
            else:
                expect(language).to_have_value("en")
                page.locator("#automatic-preferences-save").click()
                expect(page.locator("#automatic-preferences-notice")).to_contain_text(
                    "Đã lưu cài đặt xử lý tự động"
                )
                expect(page.locator("#automatic-preferences-error")).to_be_hidden()
        actual_stage = _processing_request(page, "/api/processing-stage-policy/v1")
        actual_workflow = _processing_request(page, "/api/processing-preferences")
        assert actual_stage["policy"] == {"asr": False, "diarization": True, "notes": True}
        assert actual_workflow["asr"]["language"] == "en"
    finally:
        context.close()
