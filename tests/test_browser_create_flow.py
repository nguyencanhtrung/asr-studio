"""Real-browser tests for the one-step create dialog and the automatic transcription start."""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import os
import re
import sqlite3
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from media_tools import require_media_tools
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from seeding import prepare_diarization_model, seed_meeting
from test_browser_history import _prepare_browser_asr_model
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.runtime import RuntimeProfile, write_runtime_profile
from asr_studio.storage import database_path
from asr_studio.tasks import accept_transcription_task, cancel_task
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser

MEETING_URL = re.compile(r".*/meetings/[0-9a-f]+$")
TECHNICAL_STATE_CODE = re.compile(
    r"\b(IDLE|SELECTED|UPLOADING|CHECKING|READY|CREATING|SAVED|ERROR|ABORTED)\b"
)
HANDOFF_KEY = "asr-studio:auto-transcribe:"


@pytest.fixture
def media_tools():
    require_media_tools()


@pytest.fixture
def ready_worker(live):
    """A healthy worker heartbeat with prepared models, so POST /api/tasks is accepted."""
    root = live.settings.data_dir.parent
    model_path = _prepare_browser_asr_model(root / "asr-model")
    diarization_path = prepare_diarization_model(root / "diarization-model")
    live.stop()
    live.settings = dataclasses.replace(live.settings, diarization_model_path=diarization_path)
    live.start()
    lock_fd = acquire_worker_lock(live.settings.data_dir)
    assert lock_fd is not None
    write_runtime_profile(
        live.settings.data_dir,
        RuntimeProfile(device="cpu", compute_type="int8", model_path=model_path),
    )
    stop = threading.Event()

    def beat():
        write_heartbeat(live.settings.data_dir, "idle")
        while not stop.wait(2):
            write_heartbeat(live.settings.data_dir, "idle")

    heartbeat = threading.Thread(target=beat, daemon=True)
    heartbeat.start()
    try:
        yield SimpleNamespace(model_path=model_path, diarization_path=diarization_path)
    finally:
        stop.set()
        heartbeat.join(timeout=5)
        os.close(lock_fd)


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 16_000)
    return buffer.getvalue()


def _recording(name: str, *, real: bool = False) -> dict[str, object]:
    return {"name": name, "mimeType": "audio/wav", "buffer": _wav_bytes() if real else b"x"}


def _track_requests(page) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    page.on(
        "request", lambda request: requests.append((request.method, urlsplit(request.url).path))
    )
    return requests


def _count(requests, method: str, path: str) -> int:
    return sum(1 for item in requests if item == (method, path))


def _assert_in_dialog_viewport(dialog, locator, name):
    dialog_box = dialog.bounding_box()
    element_box = locator.bounding_box()
    assert dialog_box is not None and element_box is not None, name
    assert dialog_box["x"] - 1 <= element_box["x"]
    assert dialog_box["y"] - 1 <= element_box["y"]
    assert element_box["x"] + element_box["width"] <= dialog_box["x"] + dialog_box["width"] + 1
    assert element_box["y"] + element_box["height"] <= dialog_box["y"] + dialog_box["height"] + 1


def _open_dialog(page):
    page.get_by_role("button", name="Cuộc họp mới").first.click()
    dialog = page.get_by_role("dialog", name="Cuộc họp mới")
    expect(dialog).to_be_visible()
    expect(page.locator("#recording-file")).to_be_enabled(timeout=10_000)
    return dialog


def _fill_and_create(page, name="Weekly sync.wav", *, language="en", auto=None):
    """Choose a real recording, a language and the auto-transcribe choice, then press create."""
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording(name, real=True))
    page.locator(f'input[name="language_hint"][value="{language}"]').check()
    if auto is not None:
        page.locator("#auto-transcribe").set_checked(auto)
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    return dialog


def _meeting_id(page, previous: str | None = None) -> str:
    """Wait for the meeting page that the dialog opened (not the one already showing)."""
    page.wait_for_url(
        lambda url: bool(MEETING_URL.match(url)) and not url.endswith(f"/{previous}"),
        timeout=15_000,
    )
    return page.url.rstrip("/").rsplit("/", 1)[-1]


def _task_count(live, meeting_id: str) -> int:
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE meeting_id = ? AND kind = 'transcription'",
            (meeting_id,),
        ).fetchone()
    return row[0]


def _occupy_slot(live, ready_worker):
    """Another meeting's task owns the processing slot."""
    other = seed_meeting(live.settings.data_dir, "Occupier", duration_seconds=30)
    task = accept_transcription_task(
        live.settings.data_dir,
        meeting_id=other,
        request_id="slot-occupier",
        model_path=ready_worker.model_path,
        diarization_model_path=ready_worker.diarization_path,
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    return task["id"]


def _meeting_json(context, live, meeting_id):
    response = context.request.get(f"{live.url}/api/meetings/{meeting_id}")
    assert response.status == 200
    return response.json()


def test_title_is_prefilled_from_the_file_name_until_the_user_types(live, signed_in):
    _context, page = signed_in()
    _open_dialog(page)
    title = page.locator("#meeting-name")
    file_input = page.locator("#recording-file")
    expect(title).to_have_value("")

    file_input.set_input_files(_recording("Weekly sync.v2.wav"))
    expect(title).to_have_value("Weekly sync.v2")  # only the last extension is dropped

    file_input.set_input_files(_recording("Other call.wav"))
    expect(title).to_have_value("Other call")  # an untouched suggestion follows the new file

    title.fill("Họp riêng của tôi")
    file_input.set_input_files(_recording("Third.wav"))
    expect(title).to_have_value("Họp riêng của tôi")  # what the user typed is never replaced

    page.locator("#close-dialog").click()
    _open_dialog(page)
    file_input.set_input_files(_recording("x" * 250 + ".wav"))
    assert len(title.input_value()) == 200


def test_a_closed_file_picker_does_not_close_the_dialog(live, signed_in):
    _context, page = signed_in()
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Weekly sync.wav"))
    # Closing the OS picker without a new file fires a bubbling cancel at the input (not Escape).
    page.locator("#recording-file").evaluate(
        "el => el.dispatchEvent(new Event('cancel', { bubbles: true }))"
    )
    expect(dialog).to_be_visible()
    expect(page.locator("#meeting-name")).to_have_value("Weekly sync")

    page.keyboard.press("Escape")  # Escape still closes it
    expect(dialog).to_be_hidden()


def test_keyboard_dropzone_opens_picker_and_returns_focus_to_the_origin(live, signed_in):
    _context, page = signed_in()
    dialog = _open_dialog(page)
    dropzone = page.get_by_role("button", name=re.compile(r"Chọn tệp hoặc kéo thả tại đây"))

    expect(dropzone).to_be_enabled()
    expect(page.locator("#recording-file")).to_be_enabled()

    dropzone.focus()
    with page.expect_file_chooser() as chooser_info:
        dropzone.press("Enter")
    chooser_info.value.set_files(_recording("Keyboard picker.wav"))
    expect(page.locator("#selected-file")).to_be_visible()
    status = page.get_by_role("status").filter(has_text=re.compile("Keyboard picker"))
    expect(status).to_be_visible()
    expect(page.get_by_role("button", name="Đổi file")).to_be_visible()
    assert status.evaluate("el => !el.contains(document.querySelector('#change-file'))")
    expect(dropzone).to_be_focused()

    dropzone.focus()
    with page.expect_file_chooser() as chooser_info:
        dropzone.press("Space")
    chooser_info.value.set_files(_recording("Keyboard picker two.wav"))
    expect(page.locator("#selected-file-name")).to_have_text("Keyboard picker two.wav")
    expect(dropzone).to_be_focused()

    dropzone.focus()
    with page.expect_file_chooser():
        dropzone.press("Enter")
    page.locator("#recording-file").dispatch_event("cancel")
    expect(dialog).to_be_visible()
    expect(dropzone).to_be_focused()


def test_drag_drop_uses_the_policy_backed_native_dropzone(live, signed_in):
    _context, page = signed_in()
    _open_dialog(page)
    dropzone = page.get_by_role("button", name=re.compile(r"Chọn tệp hoặc kéo thả tại đây"))
    expect(page.locator("#recording-file")).to_have_attribute("accept", re.compile(r"\.wav"))

    page.evaluate(
        """() => {
          const dropzone = document.querySelector('#file-drop');
          const transfer = new DataTransfer();
          transfer.items.add(new File(['x'], 'Dragged.wav', {type: 'audio/wav'}));
          for (const type of ['dragenter', 'dragover']) {
            dropzone.dispatchEvent(new DragEvent(type, {
              bubbles: true, cancelable: true, dataTransfer: transfer,
            }));
          }
          dropzone.dispatchEvent(new DragEvent('drop', {
            bubbles: true, cancelable: true, dataTransfer: transfer,
          }));
        }"""
    )

    expect(dropzone).to_be_enabled()
    expect(page.locator("#selected-file-name")).to_have_text("Dragged.wav")
    expect(page.locator("#selected-file")).to_be_visible()


def test_title_prefill_trims_padding_around_the_file_name(live, signed_in):
    _context, page = signed_in()
    _open_dialog(page)
    title = page.locator("#meeting-name")
    file_input = page.locator("#recording-file")

    file_input.set_input_files(_recording("  spaced name  .wav"))
    expect(title).to_have_value("spaced name")

    # Leading padding must not use up the 200 characters the suggestion is capped at ...
    file_input.set_input_files(_recording("   " + "x" * 250 + ".wav"))
    expect(title).to_have_value("x" * 200)

    # ... and a cap that ends on a space must not leave that space behind.
    file_input.set_input_files(_recording("x" * 199 + " " + "y" * 50 + ".wav"))
    expect(title).to_have_value("x" * 199)


def test_title_prefill_is_the_name_that_is_saved_and_stays_editable(live, signed_in, media_tools):
    context, page = signed_in()
    _fill_and_create(page, "Điều phối sản phẩm.wav")
    meeting_id = _meeting_id(page)
    assert _meeting_json(context, live, meeting_id)["title"] == "Điều phối sản phẩm"
    expect(page.locator("#meeting-title")).to_have_text("Điều phối sản phẩm")

    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Another.wav", real=True))
    page.locator("#meeting-name").fill("Tên tôi tự gõ")
    page.locator('input[name="language_hint"][value="en"]').check()
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    second_id = _meeting_id(page, meeting_id)
    assert _meeting_json(context, live, second_id)["title"] == "Tên tôi tự gõ"


def test_last_language_is_remembered_after_a_successful_create(live, signed_in, media_tools):
    _context, page = signed_in()
    _open_dialog(page)
    expect(page.locator('input[name="language_hint"]:checked')).to_have_count(0)
    page.locator("#close-dialog").click()

    _fill_and_create(page, "First.wav", language="vi")
    first_id = _meeting_id(page)
    _open_dialog(page)
    expect(page.locator('input[name="language_hint"][value="vi"]')).to_be_checked()
    page.locator("#close-dialog").click()

    _fill_and_create(page, "Second.wav", language="en")
    _meeting_id(page, first_id)
    _open_dialog(page)
    expect(page.locator('input[name="language_hint"][value="en"]')).to_be_checked()


def test_stored_language_default_ignores_unknown_values(live, signed_in):
    _context, page = signed_in()
    page.evaluate("localStorage.setItem('asr-studio:last-language-hint', 'vi')")
    _open_dialog(page)
    expect(page.locator('input[name="language_hint"][value="vi"]')).to_be_checked()
    page.locator("#close-dialog").click()

    page.evaluate("localStorage.setItem('asr-studio:last-language-hint', 'fr')")
    _open_dialog(page)
    expect(page.locator('input[name="language_hint"]:checked')).to_have_count(0)


def test_one_button_uploads_checks_and_creates_in_order(live, signed_in, media_tools):
    context, page = signed_in()
    requests = _track_requests(page)
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Quarterly plan.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    create = dialog.get_by_role("button", name="Tạo cuộc họp")
    expect(create).to_be_enabled()  # a valid file and form are enough: no separate upload step
    expect(dialog.get_by_role("button", name="Tải lên và kiểm tra")).to_have_count(0)
    assert not [item for item in requests if item[0] != "GET" and "/api/uploads" in item[1]]

    create.click()
    meeting_id = _meeting_id(page)
    steps = [
        (method, re.sub(r"[0-9a-f]{32}", "<id>", path))
        for method, path in requests
        if "/api/uploads" in path or path == "/api/meetings"
    ]
    assert [step for step in steps if step[0] != "GET"] == [
        ("POST", "/api/uploads"),
        ("PUT", "/api/uploads/<id>/content"),
        ("POST", "/api/uploads/<id>/complete"),
        ("POST", "/api/meetings"),
    ]
    saved = _meeting_json(context, live, meeting_id)
    assert (saved["title"], saved["language_hint"]) == ("Quarterly plan", "en")


def test_lost_complete_response_reconciles_ready_upload_without_deleting_it(
    live, signed_in, media_tools
):
    context, page = signed_in()
    requests = _track_requests(page)
    completed_ids = []
    recovery = []

    def lose_response(route):
        response = route.fetch()
        assert response.status == 200
        completed_ids.append(urlsplit(route.request.url).path.split("/")[3])
        page.locator("#cancel-upload").focus()
        route.abort()

    def observe_recovery(route):
        if route.request.method == "GET" and completed_ids:
            recovery.append(
                page.evaluate("""() => ({
                focus: document.activeElement.id,
                phase: document.querySelector('#meeting-dialog').dataset.state,
                status: document.querySelector('#workflow-state').textContent
            })""")
            )
        route.continue_()

    page.route("**/api/uploads/*/complete", lose_response)
    page.route("**/api/uploads/*", observe_recovery)
    _fill_and_create(page, "Lost response.wav", language="vi", auto=False)
    meeting_id = _meeting_id(page)
    expect(page.locator("#meeting-title")).to_have_text("Lost response")
    assert recovery == [
        {
            "focus": "cancel-upload",
            "phase": "checking",
            "status": "Đang xác minh upload sau khi phản hồi hoàn tất bị gián đoạn.",
        }
    ]
    assert page.evaluate("document.activeElement.tagName") == "BODY"
    upload_path = f"/api/uploads/{completed_ids[0]}"
    assert _count(requests, "GET", upload_path) == 1
    assert _count(requests, "POST", f"{upload_path}/complete") == 1
    assert _count(requests, "DELETE", upload_path) == 0
    assert _count(requests, "POST", "/api/meetings") == 1
    assert _meeting_json(context, live, meeting_id)["language_hint"] == "vi"


@pytest.mark.upload_chunk_bytes(4096)
def test_browser_uploads_sequential_chunks_and_recovers_after_a_failed_request(
    live, signed_in, media_tools
):
    context, page = signed_in()
    requests = _track_requests(page)
    source = _wav_bytes()
    chunks = []
    failed = False

    page.expose_function(
        "recordUploadChunk",
        lambda offset, payload: chunks.append((int(offset), base64.b64decode(payload))),
    )
    page.evaluate(
        """
        (() => {
          const open = XMLHttpRequest.prototype.open;
          const send = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function (method, url, ...rest) {
            this.__uploadUrl = url;
            return open.call(this, method, url, ...rest);
          };
          XMLHttpRequest.prototype.send = function (body) {
            if (body instanceof Blob && this.__uploadUrl.includes('/content?offset=')) {
              const offset = new URL(this.__uploadUrl, location.href).searchParams.get('offset');
              const reader = new FileReader();
              reader.onload = () => window.recordUploadChunk(
                offset, String(reader.result).split(',')[1]
              );
              reader.readAsDataURL(body);
            }
            return send.call(this, body);
          };
        })()
        """
    )

    def abort_once(route):
        nonlocal failed
        if not failed:
            failed = True
            route.abort("connectionfailed")
        else:
            route.continue_()

    page.route("**/api/uploads/*/content**", abort_once)
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(
        {"name": "Chunked.wav", "mimeType": "audio/wav", "buffer": source}
    )
    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#auto-transcribe").set_checked(False)
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    expect(dialog).to_have_attribute("data-state", "uploading", timeout=10_000)
    expect(page.locator("#file-drop")).to_be_disabled()
    assert page.evaluate("document.querySelector('#file-drop').disabled === false")
    drop_result = page.evaluate(
        """() => {
          const dropzone = document.querySelector('#file-drop');
          const transfer = new DataTransfer();
          transfer.items.add(new File(['replacement'], 'Replacement.wav', {type: 'audio/wav'}));
          const event = new DragEvent('drop', {
            bubbles: true, cancelable: true, dataTransfer: transfer,
          });
          const dispatched = dropzone.dispatchEvent(event);
          return {
            defaultPrevented: !dispatched,
            selected: document.querySelector('#selected-file-name').textContent,
          };
        }"""
    )
    assert drop_result["defaultPrevented"]
    assert drop_result["selected"] == "Chunked.wav"
    page.locator("#cancel-upload").focus()
    expect(page.locator("#state-label")).to_have_text("Đang tải lên", timeout=10_000)
    expect(page.locator("#transfer-progress")).to_be_visible()
    expect(page.locator("#workflow-state")).to_be_visible()
    expect(page.locator("#workflow-state")).to_contain_text(
        re.compile(r"Đang (tải file lên|kiểm tra số byte|thử lại phần file)")
    )
    expect(page.locator("#cancel-upload")).to_be_visible()
    assert page.evaluate("document.activeElement?.id") == "cancel-upload"

    meeting_id = _meeting_id(page)
    page.unroute("**/api/uploads/*/content**")
    page.wait_for_timeout(100)
    by_offset = {}
    for offset, body in chunks:
        by_offset.setdefault(offset, body)
        assert len(body) <= 4096
    assert len(by_offset) >= 3
    ordered_offsets = sorted(by_offset, key=int)
    assert ordered_offsets == list(range(0, len(source), 4096))
    assert b"".join(by_offset[offset] for offset in ordered_offsets) == source
    assert _meeting_json(context, live, meeting_id)["title"] == "Chunked"

    chunked_audio = context.request.get(f"{live.url}/api/meetings/{meeting_id}/audio")
    assert chunked_audio.status == 200
    chunked_audio_bytes = chunked_audio.body()

    baseline_policy = {
        "extensions": ["wav"],
        "max_size_bytes": 4 * 1024**3,
        "max_duration_seconds": 4 * 60 * 60,
        "chunk_bytes": len(source),
    }
    page.route(
        "**/api/uploads/policy",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(baseline_policy),
        ),
    )
    try:
        baseline_before = len(requests)
        baseline_dialog = _open_dialog(page)
        page.locator("#recording-file").set_input_files(
            {"name": "Chunked.wav", "mimeType": "audio/wav", "buffer": source}
        )
        page.locator('input[name="language_hint"][value="en"]').check()
        page.locator("#auto-transcribe").set_checked(False)
        baseline_dialog.get_by_role("button", name="Tạo cuộc họp").click()
        baseline_id = _meeting_id(page, meeting_id)
        baseline_content_requests = [
            item
            for item in requests[baseline_before:]
            if item[0] == "PUT" and item[1].endswith("/content")
        ]
        assert len(baseline_content_requests) == 1
    finally:
        page.unroute("**/api/uploads/policy")

    baseline_audio = context.request.get(f"{live.url}/api/meetings/{baseline_id}/audio")
    assert baseline_audio.status == 200
    assert baseline_audio.body() == chunked_audio_bytes


def test_cancel_keeps_the_form_and_retry_reruns_the_whole_flow(live, signed_in, media_tools):
    context, page = signed_in()
    requests = _track_requests(page)
    held = []
    page.route("**/api/uploads/*/content**", lambda route: held.append(route))
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Retro.wav", real=True))
    page.locator("#meeting-name").fill("Retro quý 4")
    page.locator("#meeting-date-input").fill("2026-10-15")
    page.locator('input[name="language_hint"][value="vi"]').check()
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    expect(dialog).to_have_attribute("data-state", "uploading", timeout=10_000)

    expect(page.locator("#auto-transcribe")).to_be_disabled()
    expect(page.locator("#create-asr-model")).to_be_disabled()

    dialog.get_by_role("button", name="Hủy tải lên").click()
    expect(dialog).to_have_attribute("data-state", "aborted")
    expect(page.locator("#auto-transcribe")).to_be_enabled()
    expect(page.locator("#create-asr-model")).to_be_enabled()
    expect(page.locator("#meeting-name")).to_have_value("Retro quý 4")
    expect(page.locator("#meeting-date-input")).to_have_value("2026-10-15")
    expect(page.locator('input[name="language_hint"][value="vi"]')).to_be_checked()
    assert _count(requests, "POST", "/api/meetings") == 0
    expect(page.locator("#workflow-state")).to_be_visible()
    expect(page.locator("#workflow-state")).to_contain_text("Upload đã bị hủy")
    retry = dialog.get_by_role("button", name="Tải lên lại")
    expect(retry).to_be_enabled()

    page.unroute("**/api/uploads/*/content**")
    retry.click()
    meeting_id = _meeting_id(page)  # retry ran upload, check and create
    saved = _meeting_json(context, live, meeting_id)
    assert (saved["title"], saved["meeting_date"], saved["language_hint"]) == (
        "Retro quý 4",
        "2026-10-15",
        "vi",
    )
    assert _count(requests, "POST", "/api/meetings") == 1


@pytest.mark.parametrize("how", ["failure", "cancel"])
def test_retry_stays_disabled_while_the_form_is_invalid(live, signed_in, media_tools, how):
    _context, page = signed_in()
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Retry gate.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    if how == "failure":
        page.route(
            "**/api/uploads/*/content**",
            lambda route: route.fulfill(
                status=403,
                content_type="application/json",
                body='{"detail": {"code": "invalid_media", "message": "Test upload refusal"}}',
            ),
        )
    else:
        page.route("**/api/uploads/*/content**", lambda route: None)  # the upload just hangs
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    if how == "cancel":
        expect(dialog).to_have_attribute("data-state", "uploading", timeout=10_000)
        dialog.get_by_role("button", name="Hủy tải lên").click()
    state = "aborted" if how == "cancel" else "error"
    expect(dialog).to_have_attribute("data-state", state, timeout=10_000)

    retry = dialog.get_by_role("button", name="Tải lên lại")
    title = page.locator("#meeting-name")
    expect(retry).to_be_enabled()
    for blank in ("", "   "):
        title.fill(blank)
        expect(retry).to_be_disabled()
        title.fill("Retry gate")
        expect(retry).to_be_enabled()

    # No gesture unselects a radio, so clear the language the way a form reset would.
    page.evaluate(
        """() => {
          const radio = document.querySelector('input[name="language_hint"]:checked');
          radio.checked = false;
          radio.dispatchEvent(new Event("change", { bubbles: true }));
        }"""
    )
    expect(retry).to_be_disabled()


def test_failed_create_keeps_the_form_and_a_retry_only_creates(live, signed_in, media_tools):
    _context, page = signed_in()
    requests = _track_requests(page)

    def reject_create(route):
        if route.request.method != "POST":
            route.fallback()
            return
        route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail": {"code": "invalid_meeting", "message": "Máy chủ từ chối."}}',
        )

    page.route("**/api/meetings", reject_create)
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Roadmap.wav", real=True))
    page.locator("#meeting-date-input").fill("2026-10-16")
    page.locator('input[name="language_hint"][value="en"]').check()
    dialog.get_by_role("button", name="Tạo cuộc họp").click()

    expect(dialog).to_have_attribute("data-state", "error", timeout=10_000)
    expect(page.locator("#form-error")).to_contain_text("Cuộc họp chưa được lưu")
    expect(page.locator("#meeting-name")).to_have_value("Roadmap")
    expect(page.locator("#meeting-date-input")).to_have_value("2026-10-16")
    expect(page.locator('input[name="language_hint"][value="en"]')).to_be_checked()
    retry = dialog.get_by_role("button", name="Thử tạo lại")
    expect(retry).to_be_enabled()

    page.locator("#meeting-name").press("End")
    page.locator("#meeting-name").press_sequentially(" v2")
    expect(dialog).to_have_attribute("data-state", "ready")
    expect(page.locator("#state-label")).to_have_text("Sẵn sàng tạo")
    assert not TECHNICAL_STATE_CODE.search(dialog.inner_text())

    page.unroute("**/api/meetings")
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    _meeting_id(page)
    expect(page.locator("#meeting-title")).to_have_text("Roadmap v2")
    assert _count(requests, "POST", "/api/uploads") == 1  # the ready file was not uploaded again
    assert _count(requests, "POST", "/api/meetings") == 2


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_state_labels_are_vietnamese_and_never_technical_codes(
    live, signed_in, media_tools, tmp_path, scheme
):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 360, "height": 640})
    dialog = _open_dialog(page)
    label = page.locator("#state-label")

    def expect_state(machine_state, text):
        expect(dialog).to_have_attribute("data-state", machine_state, timeout=10_000)
        expect(label).to_have_text(text)
        assert not TECHNICAL_STATE_CODE.search(dialog.inner_text())

    expect_state("idle", "Chưa chọn file")
    page.locator("#recording-file").set_input_files(_recording("States.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    expect_state("selected", "Đã chọn file")
    create = dialog.get_by_role("button", name="Tạo cuộc họp")
    retry = dialog.get_by_role("button", name="Tải lên lại")
    cancel = dialog.get_by_role("button", name="Hủy tải lên")

    page.route(
        "**/api/uploads/*/content**",
        lambda route: route.fulfill(
            status=403,
            content_type="application/json",
            body='{"detail": {"code": "invalid_media", "message": "Test upload refusal"}}',
        ),
    )
    create.click()
    expect_state("error", "Có lỗi")
    page.unroute("**/api/uploads/*/content**")

    content = dialog.locator(".dialog-content")
    assert content.evaluate("el => el.scrollHeight > el.clientHeight")
    content.evaluate("el => { el.scrollTop = 0; }")
    assert content.evaluate("el => el.scrollTop") == 0

    footer = dialog.locator(".dialog-actions")
    progress_group = page.locator("#transfer-progress")
    progressbar = page.locator("#upload-progress")
    progress_value = page.locator("#upload-progress-value")
    workflow = page.locator("#workflow-state")
    create = dialog.get_by_role("button", name="Tạo cuộc họp")

    page.route("**/api/uploads/*/content**", lambda route: None)
    retry.click()
    expect_state("uploading", "Đang tải lên")
    expect(progress_group).to_be_visible()
    expect(progressbar).to_be_visible()
    expect(progress_value).to_have_text(re.compile(r"\d+%|đã gửi"))
    expect(workflow).to_be_visible()
    expect(cancel).to_be_visible()
    for name, locator in (
        ("footer", footer),
        ("transfer progress", progress_group),
        ("progressbar", progressbar),
        ("progress value", progress_value),
        ("workflow status", workflow),
        ("cancel action", cancel),
        ("create action", create),
    ):
        _assert_in_dialog_viewport(dialog, locator, name)
    assert content.evaluate("el => el.scrollTop") == 0
    cancel.click()
    expect_state("aborted", "Đã hủy")
    page.unroute("**/api/uploads/*/content**")

    content.evaluate("el => { el.scrollTop = 0; }")
    page.route("**/api/uploads/*/complete", lambda route: None)
    retry.click()
    expect_state("checking", "Đang kiểm tra")
    expect(progress_group).to_be_visible()
    expect(progressbar).to_be_visible()
    assert progressbar.get_attribute("value") is None
    expect(progress_value).to_contain_text("Đang kiểm tra")
    expect(workflow).to_be_visible()
    expect(cancel).to_be_visible()
    for name, locator in (
        ("footer while checking", footer),
        ("indeterminate progress", progress_group),
        ("indeterminate progressbar", progressbar),
        ("checking value", progress_value),
        ("checking workflow status", workflow),
        ("checking cancel action", cancel),
        ("checking create action", create),
    ):
        _assert_in_dialog_viewport(dialog, locator, name)
    assert content.evaluate("el => el.scrollTop") == 0
    screenshot_dir = os.environ.get("ASR_ISSUE_255_SCREENSHOT_DIR")
    screenshot_override = os.environ.get("ASR_ISSUE_255_SCREENSHOT")
    if screenshot_dir:
        screenshot_path = Path(screenshot_dir) / f"held-transfer-mobile-{scheme}.png"
    elif screenshot_override and scheme == "light":
        screenshot_path = Path(screenshot_override)
    else:
        screenshot_path = tmp_path / f"issue-255-held-transfer-mobile-{scheme}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path), animations="disabled", caret="hide")
    assert screenshot_path.stat().st_size > 10_000
    cancel.click()
    expect_state("aborted", "Đã hủy")
    page.unroute("**/api/uploads/*/complete")

    expect(retry).to_be_visible()
    for name, locator in (
        ("footer after cancel", footer),
        ("retry action", retry),
        ("retry workflow status", workflow),
        ("retry create action", create),
    ):
        _assert_in_dialog_viewport(dialog, locator, name)

    held = []

    def hold_create(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.fallback()

    page.route("**/api/meetings", hold_create)
    retry.click()
    expect_state("creating", "Đang tạo")
    held[0].continue_()
    page.unroute("**/api/meetings")
    _meeting_id(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_create_dialog_desktop_layout_and_optional_screenshot(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 1280, "height": 900})
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Desktop upload evidence.wav"))
    page.locator('input[name="language_hint"][value="vi"]').check()

    assert (
        page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        == 0
    )
    assert page.locator(".dialog-content").evaluate("el => el.scrollWidth - el.clientWidth") == 0
    dialog_box = dialog.bounding_box()
    assert dialog_box is not None and dialog_box["width"] <= 700
    for locator in (
        page.locator("#selected-file-copy"),
        page.locator("#meeting-name"),
        page.locator("#workflow-state"),
        dialog.get_by_role("button", name="Tạo cuộc họp"),
    ):
        _assert_in_dialog_viewport(dialog, locator, "desktop dialog control")

    screenshot_dir = os.environ.get("ASR_ISSUE_255_SCREENSHOT_DIR")
    if screenshot_dir:
        screenshot_path = Path(screenshot_dir) / f"new-meeting-desktop-{scheme}.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), animations="disabled", caret="hide")
        assert screenshot_path.stat().st_size > 10_000


def test_state_label_is_vietnamese_while_the_policy_is_still_loading(live, signed_in):
    _context, page = signed_in()
    held = []
    page.route("**/api/uploads/policy", lambda route: held.append(route))
    with page.expect_request("**/api/uploads/policy"):
        page.get_by_role("button", name="Cuộc họp mới").first.click()
    dialog = page.get_by_role("dialog", name="Cuộc họp mới")
    label = page.locator("#state-label")

    expect(dialog).to_be_visible()
    expect(page.locator("#recording-file")).to_be_disabled()  # the policy has not arrived yet
    expect(label).to_have_text("Chưa chọn file")
    assert not TECHNICAL_STATE_CODE.search(dialog.inner_text())

    held[0].continue_()
    expect(page.locator("#recording-file")).to_be_enabled(timeout=10_000)
    expect(label).to_have_text("Chưa chọn file")


def test_auto_transcribe_checkbox_defaults_on_and_is_remembered(live, signed_in):
    _context, page = signed_in()
    dialog = _open_dialog(page)
    checkbox = dialog.get_by_label("Chép lời ngay sau khi tạo")
    expect(checkbox).to_be_checked()
    expect(dialog.get_by_text("Bỏ chọn để chép lời thủ công")).to_be_visible()

    checkbox.uncheck()
    assert page.evaluate("localStorage.getItem('asr-studio:auto-transcribe')") == "0"
    page.locator("#close-dialog").click()
    _open_dialog(page)
    expect(checkbox).not_to_be_checked()

    page.reload(wait_until="domcontentloaded")
    _open_dialog(page)
    expect(checkbox).not_to_be_checked()

    checkbox.check()
    page.locator("#close-dialog").click()
    _open_dialog(page)
    expect(checkbox).to_be_checked()


def test_create_dialog_model_selector_shows_readiness_and_resets_per_run(live, signed_in):
    _context, page = signed_in()
    choices = [
        {
            "id": "auto",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "whisper-large-v3",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "pho-whisper-large",
            "model_name": "pho-whisper-large",
            "ready": False,
            "reason": "not_prepared",
            "prepare_command": "docker compose run prepare",
        },
        {
            "id": "qwen3-asr-1.7b",
            "model_name": "qwen3-asr-1.7b",
            "ready": False,
            "reason": "gpu_required",
            "prepare_command": None,
        },
    ]
    page.route(
        "**/api/asr-models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"asr_models": choices}),
        ),
    )

    dialog = _open_dialog(page)
    selector = page.locator("#create-asr-model")
    expect(dialog.get_by_label("Model chép lời")).to_be_visible()
    expect(selector.locator("option")).to_have_count(4)
    expect(selector.locator('option[value="auto"]')).to_be_enabled()
    expect(selector.locator('option[value="pho-whisper-large"]')).to_be_disabled()
    expect(selector.locator('option[value="qwen3-asr-1.7b"]')).to_be_disabled()
    expect(page.locator("#create-asr-model-help")).to_contain_text("Model chưa sẵn sàng")

    page.locator("#auto-transcribe").uncheck()
    expect(page.locator("#create-asr-model-control")).to_be_hidden()
    page.locator("#auto-transcribe").check()
    expect(page.locator("#create-asr-model-control")).to_be_visible()
    selector.select_option("whisper-large-v3")
    page.locator("#close-dialog").click()
    _open_dialog(page)
    expect(selector).to_have_value("auto")
    assert (
        page.evaluate("() => Object.keys(localStorage).some(key => key.includes('asr_model'))")
        is False
    )


def test_ready_create_model_is_sent_once_and_handoff_is_consumed(live, signed_in, media_tools):
    _context, page = signed_in()
    choices = [
        {
            "id": "auto",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "whisper-large-v3",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "pho-whisper-large",
            "model_name": "pho-whisper-large",
            "ready": False,
            "reason": "not_prepared",
            "prepare_command": None,
        },
        {
            "id": "qwen3-asr-1.7b",
            "model_name": "qwen3-asr-1.7b",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
    ]
    page.route(
        "**/api/asr-models",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"asr_models": choices})
        ),
    )
    task_requests = []

    def accept_task(route):
        if route.request.method != "POST":
            route.fallback()
            return
        body = route.request.post_data_json
        task_requests.append(body)
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "a" * 32,
                    "request_id": body["request_id"],
                    "status": "accepted",
                    "language_hint": body["language_hint"],
                    "asr_model": body["asr_model"],
                    "stage": "queued",
                    "worker_state": "healthy",
                }
            ),
        )

    page.route("**/api/tasks", accept_task)
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Named model.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#create-asr-model").select_option("qwen3-asr-1.7b")
    with page.expect_response("**/api/tasks", timeout=15_000):
        dialog.get_by_role("button", name="Tạo cuộc họp").click()
    meeting_id = _meeting_id(page)
    assert len(task_requests) == 1
    assert task_requests[0]["asr_model"] == "qwen3-asr-1.7b"
    assert page.evaluate(f"sessionStorage.getItem('{HANDOFF_KEY}{meeting_id}')") is None

    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1_000)
    assert len(task_requests) == 1


def test_failed_model_catalog_hides_control_but_creation_still_works(live, signed_in, media_tools):
    _context, page = signed_in()
    page.route(
        "**/api/asr-models",
        lambda route: route.fulfill(status=500, content_type="application/json", body="{}"),
    )
    dialog = _open_dialog(page)
    expect(page.locator("#create-asr-model-control")).to_be_hidden()
    page.locator("#recording-file").set_input_files(
        _recording("Catalog unavailable.wav", real=True)
    )
    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#auto-transcribe").uncheck()
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    _meeting_id(page)


def test_removed_model_refusal_keeps_selection_guidance_and_does_not_retry(
    live, ready_worker, signed_in, media_tools
):
    _context, page = signed_in()
    dialog_choices = [
        {
            "id": "auto",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "whisper-large-v3",
            "model_name": "whisper-large-v3",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "pho-whisper-large",
            "model_name": "pho-whisper-large",
            "ready": True,
            "reason": None,
            "prepare_command": None,
        },
        {
            "id": "qwen3-asr-1.7b",
            "model_name": "qwen3-asr-1.7b",
            "ready": False,
            "reason": "gpu_required",
            "prepare_command": None,
        },
    ]
    meeting_choices = [
        {
            **choice,
            "ready": choice["id"] == "auto",
            "reason": None if choice["id"] == "auto" else "not_prepared",
            "prepare_command": (
                "docker compose run --rm worker model prepare --asr-model pho-whisper-large"
            )
            if choice["id"] == "pho-whisper-large"
            else None,
        }
        for choice in dialog_choices
    ]
    page.route(
        "**/api/asr-models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"asr_models": dialog_choices}),
        ),
    )

    def replace_meeting_models(route):
        if route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body["transcription"]["asr_models"] = meeting_choices
        route.fulfill(response=response, json=body)

    page.route("**/api/meetings/*", replace_meeting_models)
    task_posts = []

    def refuse_removed_model(route):
        if route.request.method != "POST":
            route.fallback()
            return
        task_posts.append(route.request.post_data_json)
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": {"code": "model_unavailable", "message": "removed"}}),
        )

    page.route("**/api/tasks", refuse_removed_model)
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Removed model.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#create-asr-model").select_option("pho-whisper-large")
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    _meeting_id(page)

    expect(page.locator("#transcription-error")).to_contain_text("Model đã chọn chưa được chuẩn bị")
    expect(page.locator("#transcription-model")).to_have_value("pho-whisper-large")
    expect(page.locator("#transcription-model-guidance")).to_be_visible()
    expect(page.locator("#transcription-model-guidance-text")).to_contain_text(
        "Model này chưa được chuẩn bị"
    )
    expect(page.locator("#transcription-model-command")).to_have_value(
        "docker compose run --rm worker model prepare --asr-model pho-whisper-large"
    )
    page.wait_for_timeout(1_000)
    assert len(task_posts) == 1


def test_checkbox_on_starts_transcription_exactly_once(live, ready_worker, signed_in, media_tools):
    context, page = signed_in()
    requests = _track_requests(page)
    _fill_and_create(page)
    meeting_id = _meeting_id(page)

    expect(page.locator("#cancel-transcription")).to_be_visible(timeout=10_000)
    expect(page.locator("#start-transcription")).to_be_hidden()
    assert _count(requests, "POST", "/api/tasks") == 1
    assert _task_count(live, meeting_id) == 1
    assert page.evaluate(f"sessionStorage.getItem('{HANDOFF_KEY}{meeting_id}')") is None

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Weekly sync")
    page.go_back()
    page.go_forward()
    expect(page.locator("#meeting-title")).to_have_text("Weekly sync")
    page.wait_for_timeout(1_000)
    assert _count(requests, "POST", "/api/tasks") == 1
    assert _task_count(live, meeting_id) == 1
    task = _meeting_json(context, live, meeting_id)["transcription"]["task"]
    assert task["status"] in {"accepted", "running"}


def test_busy_machine_shows_the_reason_and_the_manual_button_without_retry(
    live, ready_worker, signed_in, media_tools
):
    context, page = signed_in()
    requests = _track_requests(page)
    occupier = _occupy_slot(live, ready_worker)
    _fill_and_create(page)
    meeting_id = _meeting_id(page)

    error = page.locator("#transcription-error")
    expect(error).to_contain_text("Máy đang bận", timeout=10_000)
    expect(error).to_contain_text("không tự thử lại")
    start = page.get_by_role("button", name="Chép lời")
    expect(start).to_be_visible()
    expect(start).to_be_enabled()
    page.wait_for_timeout(1_500)
    assert _count(requests, "POST", "/api/tasks") == 1  # never retried by itself
    assert _task_count(live, meeting_id) == 0

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Weekly sync")
    expect(start).to_be_visible()
    page.wait_for_timeout(1_000)
    assert _count(requests, "POST", "/api/tasks") == 1  # a reload does not send it either

    cancel_task(live.settings.data_dir, occupier)
    start.click()
    expect(page.locator("#cancel-transcription")).to_be_visible(timeout=10_000)
    assert _count(requests, "POST", "/api/tasks") == 2
    assert _task_count(live, meeting_id) == 1
    assert _meeting_json(context, live, meeting_id)["transcription"]["task"] is not None


def test_unavailable_worker_shows_the_reason_and_the_manual_button(live, signed_in, media_tools):
    _context, page = signed_in()
    requests = _track_requests(page)
    _fill_and_create(page)
    meeting_id = _meeting_id(page)

    expect(page.locator("#transcription-error")).to_contain_text(
        "Worker chép lời chưa sẵn sàng", timeout=10_000
    )
    expect(page.get_by_role("button", name="Chép lời")).to_be_enabled()
    page.wait_for_timeout(1_500)
    assert _count(requests, "POST", "/api/tasks") == 1
    assert _task_count(live, meeting_id) == 0


def test_checkbox_off_keeps_the_manual_start(live, ready_worker, signed_in, media_tools):
    _context, page = signed_in()
    requests = _track_requests(page)
    _fill_and_create(page, auto=False)
    meeting_id = _meeting_id(page)

    start = page.get_by_role("button", name="Chép lời")
    expect(start).to_be_enabled(timeout=10_000)
    expect(page.locator("#transcription-error")).to_be_hidden()
    page.wait_for_timeout(1_000)
    assert _count(requests, "POST", "/api/tasks") == 0
    assert _task_count(live, meeting_id) == 0
    assert page.evaluate(f"sessionStorage.getItem('{HANDOFF_KEY}{meeting_id}')") is None

    start.click()
    expect(page.locator("#cancel-transcription")).to_be_visible(timeout=10_000)
    assert _count(requests, "POST", "/api/tasks") == 1


def test_a_blocked_handoff_never_starts_transcription_and_still_creates(
    live, ready_worker, signed_in, media_tools
):
    _context, page = signed_in()
    requests = _track_requests(page)
    page.add_init_script(
        """
        const setItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
          if (String(key).startsWith("asr-studio:auto-transcribe:")) {
            throw new DOMException("blocked", "QuotaExceededError");
          }
          return setItem.call(this, key, value);
        };
        """
    )
    page.reload(wait_until="domcontentloaded")
    _fill_and_create(page)
    _meeting_id(page)

    expect(page.get_by_role("button", name="Chép lời")).to_be_enabled(timeout=10_000)
    page.wait_for_timeout(1_000)
    assert _count(requests, "POST", "/api/tasks") == 0


def test_a_refused_file_cannot_be_resubmitted_until_another_is_chosen(live, signed_in, media_tools):
    _context, page = signed_in()
    page.route(
        "**/api/uploads/*/complete",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail": {"code": "invalid_media", "message": "Không đọc được file."}}',
        ),
    )
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Broken.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    create = dialog.get_by_role("button", name="Tạo cuộc họp")
    create.click()

    expect(dialog).to_have_attribute("data-state", "error", timeout=10_000)
    expect(page.locator("#form-error")).to_contain_text("File không đạt yêu cầu")
    expect(create).to_be_disabled()  # the same file would be refused again
    expect(dialog.get_by_role("button", name="Tải lên lại")).to_be_hidden()
    expect(page.locator("#meeting-name")).to_have_value("Broken")  # the form keeps its data

    page.unroute("**/api/uploads/*/complete")
    page.locator("#recording-file").set_input_files(_recording("Fixed.wav", real=True))
    expect(page.locator("#meeting-name")).to_have_value("Fixed")
    expect(create).to_be_enabled()
    create.click()
    _meeting_id(page)


def test_the_button_is_locked_once_the_meeting_is_saved(live, signed_in, media_tools):
    _context, page = signed_in()
    requests = _track_requests(page)
    # The dialog is gone with the old page, so keep what it showed at the moment it was saved.
    page.add_init_script(
        """
        new MutationObserver(() => {
          const dialog = document.querySelector("#meeting-dialog");
          if (dialog && dialog.dataset.state === "saved") {
            sessionStorage.setItem("saved-snapshot", JSON.stringify({
              createDisabled: document.querySelector("#create-meeting").disabled,
              retryHidden: document.querySelector("#retry-upload").hidden,
            }));
          }
        }).observe(document, { attributes: true, childList: true, subtree: true });
        """
    )
    page.reload(wait_until="domcontentloaded")
    _fill_and_create(page)
    _meeting_id(page)

    snapshot = json.loads(page.evaluate("sessionStorage.getItem('saved-snapshot')"))
    assert snapshot == {"createDisabled": True, "retryHidden": True}
    assert _count(requests, "POST", "/api/uploads") == 1
    assert _count(requests, "POST", "/api/meetings") == 1


def test_auto_start_is_skipped_for_a_meeting_that_already_has_a_task(live, signed_in):
    _context, page = signed_in()
    requests = _track_requests(page)
    finished, _generation, _segments = seed_completed_transcript(live.settings.data_dir)
    only_a_task = seed_meeting(live.settings.data_dir, "Earlier run", duration_seconds=30)
    model_path = _prepare_browser_asr_model(live.settings.data_dir.parent / "asr-model")
    diarization_path = prepare_diarization_model(live.settings.data_dir.parent / "diarization")
    earlier = accept_transcription_task(
        live.settings.data_dir,
        meeting_id=only_a_task,
        request_id="earlier-run",
        model_path=model_path,
        diarization_model_path=diarization_path,
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    cancel_task(live.settings.data_dir, earlier["id"])

    for meeting_id in (finished, only_a_task):
        page.evaluate(f"sessionStorage.setItem('{HANDOFF_KEY}{meeting_id}', '1')")
        page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
        expect(page.locator("#start-transcription")).to_be_enabled(timeout=10_000)
        page.wait_for_timeout(500)
        # The one-shot key is spent even when it is not acted on.
        assert page.evaluate(f"sessionStorage.getItem('{HANDOFF_KEY}{meeting_id}')") is None
    assert _count(requests, "POST", "/api/tasks") == 0


def _contrast(page, selector):
    return page.evaluate(
        """selector => {
          const parse = value => value.match(/[\\d.]+/g).map(Number);
          const luminance = ([r, g, b]) => {
            const linear = [r, g, b].map(channel => {
              const unit = channel / 255;
              return unit <= 0.04045 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
          };
          const element = document.querySelector(selector);
          let node = element;
          let background = [255, 255, 255];
          while (node) {
            const parsed = parse(getComputedStyle(node).backgroundColor);
            if (parsed.length < 4 || parsed[3] > 0) {
              background = parsed.slice(0, 3);
              break;
            }
            node = node.parentElement;
          }
          const values = [
            luminance(parse(getComputedStyle(element).color)),
            luminance(background),
          ].sort((a, b) => b - a);
          return (values[0] + 0.05) / (values[1] + 0.05);
        }""",
        selector,
    )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_create_dialog_fits_360px_and_stays_legible_in_both_themes(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 360, "height": 740})
    dialog = _open_dialog(page)
    long_name = "Cuộc họp điều phối rất dài " * 6 + ".wav"
    page.locator("#recording-file").set_input_files(_recording(long_name))
    page.locator('input[name="language_hint"][value="vi"]').check()
    page.locator("#auto-transcribe").scroll_into_view_if_needed()

    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    assert (
        page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        == 0
    )
    assert page.locator(".dialog-content").evaluate("el => el.scrollWidth - el.clientWidth") == 0
    frame = dialog.bounding_box()
    for selector in (
        ".auto-transcribe-option",
        "#auto-transcribe-help",
        "#create-asr-model-control",
        "#create-asr-model-help",
        "#state-label",
    ):
        box = page.locator(selector).bounding_box()
        assert box["x"] >= frame["x"] and box["x"] + box["width"] <= frame["x"] + frame["width"]
    for selector in (
        ".auto-transcribe-option",
        "#auto-transcribe-help",
        "#create-asr-model-help",
        "#state-label",
        "#selected-file-name",
        "#workflow-state",
    ):
        assert _contrast(page, selector) >= 4.5, selector

    file_name_metrics = page.locator("#selected-file-name").evaluate(
        """el => {
          const style = getComputedStyle(el);
          const lineHeight = parseFloat(style.lineHeight);
          return {
            whiteSpace: style.whiteSpace,
            lineCount: Math.round(el.scrollHeight / lineHeight),
          };
        }"""
    )
    assert file_name_metrics["whiteSpace"] == "normal"
    assert file_name_metrics["lineCount"] >= 2

    content = page.locator(".dialog-content")
    assert content.evaluate("el => el.scrollHeight > el.clientHeight")
    content.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(page.locator("#workflow-state")).to_be_visible()
    dialog_box = dialog.bounding_box()
    footer_box = dialog.locator(".dialog-actions").bounding_box()
    assert footer_box["y"] + footer_box["height"] <= dialog_box["y"] + dialog_box["height"] + 1
    assert page.locator("#workflow-state").evaluate("el => el.closest('.dialog-actions') !== null")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_new_dialog_controls_show_a_keyboard_focus_ring(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 1280, "height": 900})
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Focus.wav"))
    radio = page.locator('input[name="language_hint"][value="vi"]')
    radio.check()
    radio.focus()
    page.keyboard.press("Tab")
    checkbox = page.locator("#auto-transcribe")
    expect(checkbox).to_be_focused()

    def ring(locator):
        return locator.evaluate(
            "el => ({style: getComputedStyle(el).outlineStyle, "
            "width: parseFloat(getComputedStyle(el).outlineWidth)})"
        )

    assert ring(checkbox)["style"] == "solid" and ring(checkbox)["width"] >= 2
    model = dialog.locator("#create-asr-model")
    create = dialog.get_by_role("button", name="Tạo cuộc họp")
    page.keyboard.press("Tab")
    expect(model).to_be_focused()
    assert ring(model)["style"] == "solid" and ring(model)["width"] >= 2
    page.keyboard.press("Tab")
    expect(create).to_be_focused()
    assert ring(create)["style"] == "solid" and ring(create)["width"] >= 2


def test_reduced_motion_collapses_the_dialog_transitions(live, signed_in, media_tools):
    _context, page = signed_in(reduced_motion="reduce", viewport={"width": 1280, "height": 900})
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(_recording("Calm.wav", real=True))
    page.locator('input[name="language_hint"][value="en"]').check()
    for selector in (
        "#create-meeting",
        "#auto-transcribe",
        ".auto-transcribe-option",
        ".language-option",
        ".upload-state-icon > span",
        ".file-drop",
    ):
        durations = page.locator(selector).first.evaluate(
            "el => { const style = getComputedStyle(el);"
            " return [style.transitionDuration, style.animationDuration]"
            ".flatMap(value => value.split(',')).map(parseFloat); }"
        )
        assert max(durations) <= 0.001, (selector, durations)

    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    _meeting_id(page)
