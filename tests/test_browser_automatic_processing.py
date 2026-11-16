"""Real-browser coverage for automatic-processing preferences and workflow actions."""

from __future__ import annotations

import sqlite3
import uuid

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
    page.get_by_role("tab", name="Thiết bị").click()
    expect(page.locator("#automatic-preferences-version")).to_have_text("v1")
    expect(page.locator("#automatic-preferences-provider")).to_have_value("")
    expect(page.locator("#automatic-preferences-provider-help")).to_contain_text("Transcript")
    expect(page.locator("#automatic-preferences-save")).to_be_disabled()
    assert page.locator("#automatic-preferences-asr-choice option").count() >= 1

    page.get_by_role("tab", name="Theme").click()
    with page.expect_navigation():
        page.get_by_role("radio", name="English").check()
    page.get_by_role("tab", name="Devices").click()
    expect(page.locator("#automatic-preferences-provider-help")).to_contain_text("The transcript")
    expect(page.locator("#automatic-preferences-save")).to_be_disabled()
    context.close()


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
        page.get_by_role("tab", name="Thiết bị" if language == "vi" else "Devices").click()
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
            if route.request.method == "PUT":
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
        page.keyboard.press("Enter")
        expect(notice).to_have_text(
            "Đã lưu cài đặt xử lý tự động."
            if language == "vi"
            else "Automatic-processing settings saved."
        )
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
