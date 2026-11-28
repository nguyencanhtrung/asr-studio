"""Real-app proof for a default-disabled stage-policy hold."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from itertools import product
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_browser_automatic_processing import _processing_request
from test_browser_history import live as live
from test_browser_history import open_page
from test_browser_history import signed_in as signed_in

from asr_studio.extension_auth import ExtensionSessionIdentity
from asr_studio.extension_ingest import (
    _process_archive_session,
    begin_chunk,
    create_recording_session,
    finalize_recording_session,
    publish_ready_derivative,
)
from asr_studio.storage import database_path, initialize

pytestmark = pytest.mark.browser
RUN_RENDERED = Path("/tmp/asr-visual-correction/issue-277-r4-ui/rendered")


def _manifest(content: bytes) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    lines = f"v1\n0:0:{len(content)}:{digest}\n".encode()
    return {
        "finalize_id": "stage-hold-finalize",
        "capture_epoch": "stage-hold-capture",
        "epoch": 1,
        "capture_complete": True,
        "total_bytes": len(content),
        "chunk_count": 1,
        "manifest_hash": hashlib.sha256(lines).hexdigest(),
        "chunks": [{"sequence": 0, "offset": 0, "length": len(content), "sha256": digest}],
    }


def _create_ready_format1_workflow(live, client_id: str) -> str:
    """Use the real extension receipt/handoff boundary with disposable test bytes."""
    data_dir = live.settings.data_dir
    initialize(data_dir)
    identity = ExtensionSessionIdentity(
        device_record_id=f"device-{client_id}",
        owner_id=f"owner-{client_id}",
        device_id=f"extension-{client_id}",
        server_url=live.url,
        generation=1,
        scope="ingest",
    )
    payload = {
        "client_recording_id": client_id,
        "capture_epoch": "stage-hold-capture",
        "mime_type": "audio/webm;codecs=opus",
        "title": f"Stage hold {client_id}",
        "metadata": {"channels": 1, "sample_rate": 48000},
        "stage_policy": {"format_version": 1},
    }
    created, status = create_recording_session(
        data_dir,
        identity=identity,
        payload=payload,
        backend_id="s" * 32,
        settings=live.settings,
    )
    assert status == 201, created
    session_id = str(created["id"])
    content = f"disposable-stage-hold-{client_id}".encode()
    digest = hashlib.sha256(content).hexdigest()
    lease = begin_chunk(
        data_dir,
        identity=identity,
        session_id=session_id,
        sequence=0,
        offset=0,
        sha256=digest,
        epoch=1,
        declared_length=len(content),
        settings=live.settings,
    )
    lease.write(content)
    assert lease.commit()["next_offset"] == len(content)
    finalized = finalize_recording_session(
        data_dir,
        identity=identity,
        session_id=session_id,
        payload=_manifest(content),
        backend_id="s" * 32,
        settings=live.settings,
    )
    assert finalized["state"] == "finalizing"
    _process_archive_session(
        data_dir,
        session_id,
        settings=live.settings,
        stop_event=threading.Event(),
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        assert publish_ready_derivative(
            connection,
            session_id,
            derivative_key=f"extension-derivatives/{session_id}.m4a",
            duration_seconds=1.0,
        )
        connection.commit()
        meeting_id = connection.execute(
            "SELECT meeting_id FROM recordings WHERE extension_session_id = ?", (session_id,)
        ).fetchone()[0]
    return str(meeting_id)


@pytest.mark.parametrize(
    ("width", "theme", "language"),
    list(product((1440, 390), ("light", "dark"), ("vi", "en"))),
    ids=[
        f"{'desktop' if width == 1440 else 'mobile'}-{language}-{theme}"
        for width, theme, language in product((1440, 390), ("light", "dark"), ("vi", "en"))
    ],
)
def test_default_disabled_format1_hold_has_guidance_and_only_cancel_is_keyboard_action(
    live, signed_in, width, theme, language
):
    context, bootstrap = signed_in(
        color_scheme=theme,
        viewport={"width": width, "height": 900},
    )
    try:
        context.add_init_script(f"localStorage.setItem('asr-studio:lang', {language!r})")
        meeting_id = _create_ready_format1_workflow(live, f"{language}-{width}-{uuid.uuid4().hex}")
        gate = _processing_request(bootstrap, "/api/processing-stage-policy/v1")
        assert gate["admission"]["state"] == "disabled"
        page = open_page(context, live, f"/meetings/{meeting_id}")

        workflow = _processing_request(page, f"/api/meetings/{meeting_id}/processing")
        assert workflow["state"] == "needs_attention"
        assert workflow["steps"][0]["waiting_reason"] == "stage_policy_admission_disabled"
        expect(page.locator("#automatic-processing-panel")).to_be_visible()
        expect(page.locator("#automatic-processing-message")).to_contain_text(
            (
                "Máy chủ chưa cho phép tự động xử lý. Kiểm tra cấu hình rồi tải lại trạng thái; "
                "ứng dụng không tự thử lại."
            )
            if language == "vi"
            else (
                "The server has not enabled automatic processing. Check the configuration "
                "and reload the status; "
                "the app will not retry automatically."
            )
        )
        expect(page.locator("#automatic-processing-message")).not_to_contain_text("Confirm")
        expect(page.locator("#automatic-processing-source")).to_contain_text(
            "Nguồn hiện tại được giữ nguyên"
            if language == "vi"
            else "The current source is preserved"
        )
        expect(page.locator("#automatic-processing-source")).not_to_contain_text("Xác nhận")
        for selector in (
            "#automatic-processing-retry-notes",
            "#automatic-processing-apply-settings",
            "#automatic-processing-use-source",
        ):
            expect(page.locator(selector)).to_be_hidden()
        expect(page.locator("#automatic-processing-error")).to_be_hidden()
        RUN_RENDERED.mkdir(parents=True, exist_ok=True)
        page.locator("#automatic-processing-panel").screenshot(
            path=str(RUN_RENDERED / f"stage-hold-{width}-{theme}-{language}.png")
        )

        cancel = page.locator("#automatic-processing-cancel")
        expect(cancel).to_be_visible()
        cancel.focus()
        expect(cancel).to_be_focused()
        cancel.press("Enter")
        expect(page.locator("#automatic-processing-message")).to_contain_text(
            "Đã hủy xử lý tự động" if language == "vi" else "Automatic processing was canceled"
        )
    finally:
        context.close()
