"""Real-browser checks for choosing an ASR model per run and comparing the resulting transcripts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from notes_helpers import seed_cited_notes, seed_completed_transcript
from playwright.sync_api import expect
from seeding import prepare_diarization_model, prepare_recording, seed_meeting
from test_browser_history import _prepare_browser_asr_model, _prepare_browser_qwen_model
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.runtime import RuntimeProfile, read_runtime_profile, write_runtime_profile
from asr_studio.storage import database_path
from asr_studio.tasks import (
    add_current_generation_speaker,
    claim_next_task,
    finish_interrupted_task,
    publish_complete_generation,
    reassign_current_generation_speaker,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser

PREPARE_FLAG = "--asr-model pho-whisper-large"
INVITES_START = "Bấm “Chép lời”"
INVITES_RETRY = "hãy bấm để thử lại"
NO_CONFIDENCE = "“cần kiểm tra” không xét độ tin cậy của chữ"


def open_processing_details(page):
    """Activate the native processing disclosure before using its completed controls."""
    details = page.locator("#transcript-processing")
    # The shell starts this disclosure open, but the first saved-generation paint
    # closes it. Wait for that real render before inspecting the native state.
    expect(page.locator("#transcript-generation")).to_be_visible()
    expect(page.locator("#generation-title")).to_have_text(re.compile(r"^Transcript #\d+$"))
    summary = details.locator(":scope > summary")
    summary.focus()
    expect(summary).to_be_focused()
    if not details.evaluate("element => element.open"):
        summary.press("Enter")
    expect(details).to_have_attribute("open", "")
    return details


@pytest.fixture
def worker(live):
    """A healthy GPU worker whose default model is prepared, seen by the running server."""
    data_dir = live.settings.data_dir
    default_model = _prepare_browser_qwen_model(data_dir.parent / "qwen3-asr-1.7b")
    _prepare_browser_asr_model(data_dir.parent / "whisper-large-v3")
    # The server keeps this settings object and reads the model paths on each request.
    object.__setattr__(live.settings, "asr_gpu_model_path", default_model)
    object.__setattr__(
        live.settings,
        "diarization_model_path",
        prepare_diarization_model(data_dir.parent / "diarization-model"),
    )
    object.__setattr__(live.settings, "asr_qwen_model_path", default_model)
    lock = acquire_worker_lock(data_dir)
    assert lock is not None
    write_runtime_profile(data_dir, RuntimeProfile("cuda", "float16", default_model))
    write_heartbeat(data_dir, "idle")
    yield default_model
    os.close(lock)


def finish_run(live, text, avg_logprob=None):
    """Do what the worker does once ASR, diarization and alignment are done."""
    owner_id = f"browser-worker-{uuid.uuid4().hex}"
    task = claim_next_task(live.settings.data_dir, owner_id=owner_id)
    assert task is not None
    assert update_task_progress(
        live.settings.data_dir,
        task["id"],
        owner_id=owner_id,
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=2.0,
        segment_count=1,
    )
    published = publish_complete_generation(
        live.settings.data_dir,
        task["id"],
        owner_id=owner_id,
        segments=[
            {
                "id": uuid.uuid4().hex,
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "text": text,
                "original_speaker_id": "SPEAKER_00",
                "speaker_id": "SPEAKER_00",
                "avg_logprob": avg_logprob,
            }
        ],
        speakers=[
            {
                "speaker_id": "SPEAKER_00",
                "display_name": "Speaker 1",
                "first_seen_seconds": 1.0,
                "last_seen_seconds": 2.0,
            }
        ],
        language_probability=None,
        elapsed_seconds=1.0,
    )
    assert published is not None
    return published["id"]


def test_a_choice_goes_with_one_run_and_the_transcripts_can_be_compared(live, signed_in, worker):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    object.__setattr__(
        live.settings,
        "asr_phowhisper_model_path",
        _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large"),
    )
    meeting_id = seed_meeting(data_dir, "Model comparison", "2026-10-17", duration_seconds=30.0)
    prepare_recording(data_dir, meeting_id)
    sent = []
    page.on(
        "request",
        lambda request: (
            sent.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/tasks")
            else None
        ),
    )

    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    expect(selector).to_have_value("auto")
    expect(selector.locator("option")).to_have_text(
        [
            "Tự động · sẵn sàng",
            "Whisper large-v3 · sẵn sàng",
            "Whisper small · chưa sẵn sàng",
            "PhoWhisper-large · sẵn sàng",
            "Qwen3-ASR-1.7B · sẵn sàng",
        ]
    )
    expect(page.locator("#transcription-model-help")).to_have_text(
        "Theo phần cứng của máy: qwen3-asr-1.7b."
    )

    page.get_by_role("button", name="Chép lời").click()
    expect(page.get_by_role("button", name="Hủy")).to_be_visible()
    expect(selector).to_be_disabled()
    first_generation = finish_run(live, "Bản chép lời đầu tiên.")
    meta = page.locator("#generation-meta")
    expect(page.locator("#generation-title")).to_have_text("Transcript #1", timeout=8_000)
    expect(meta).to_contain_text("qwen3-asr-1.7b · GPU · CUDA · bfloat16")

    open_processing_details(page)
    selector.select_option("pho-whisper-large")
    expect(page.locator("#transcription-model-help")).to_contain_text("Tối ưu tiếng Việt")
    expect(page.locator("#transcription-model-help")).to_contain_text("Không dành cho English")
    page.get_by_role("button", name="Chạy lại").click()
    expect(page.get_by_role("button", name="Hủy")).to_be_visible()
    expect(page.locator("#transcription-rerun-model")).to_have_value("pho-whisper-large")
    page.locator("#transcription-rerun-submit").click()
    second_generation = finish_run(live, "Bản chép lời thứ hai.")
    expect(page.locator("#generation-title")).to_have_text("Transcript #2", timeout=8_000)
    expect(meta).to_contain_text("pho-whisper-large · GPU · CUDA · float16")
    assert [body["asr_model"] for body in sent] == ["auto", "pho-whisper-large"]

    # Each transcript stays readable under the model that produced it.
    generations = page.locator("#generation-selector")
    expect(generations.locator("option")).to_have_text(
        [
            "Transcript #1 · qwen3-asr-1.7b · lịch sử",
            "Transcript #2 · pho-whisper-large · hiện tại",
        ]
    )
    generations.select_option(first_generation)
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(meta).to_contain_text("qwen3-asr-1.7b · GPU · CUDA · bfloat16")
    expect(page.locator(".segment-text")).to_have_text("Bản chép lời đầu tiên.")
    expect(page.locator("#generation-mode")).to_have_text("Bản lịch sử · Chỉ đọc.")
    generations.select_option(second_generation)
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    expect(page.locator(".segment-text")).to_have_text("Bản chép lời thứ hai.")

    # The choice belonged to that run: the system default is untouched and a new page starts over.
    assert read_runtime_profile(data_dir).model_path == worker
    page.reload()
    open_processing_details(page)
    expect(selector).to_have_value("auto")


@pytest.mark.parametrize(
    ("viewport", "artifact_name", "scheme"),
    [
        ({"width": 1440, "height": 1000}, "desktop", "light"),
        ({"width": 390, "height": 844}, "mobile", "dark"),
    ],
)
def test_completed_transcript_rerun_uses_server_confirmed_diarization_choices(
    live, signed_in, worker, viewport, artifact_name, scheme, tmp_path
):
    """Exercise the bounded rerun modal against the real capability endpoint."""
    _context, page = signed_in(viewport=viewport, color_scheme=scheme)
    meeting_id, generation_id, _segments = seed_completed_transcript(
        live.settings.data_dir, title="Diarization rerun fixture"
    )
    prepare_recording(live.settings.data_dir, meeting_id)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    page.get_by_role("button", name="Chạy lại", exact=True).click()

    dialog = page.locator("#transcription-rerun-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#rerun-capability-status")).to_contain_text("Lựa chọn được xác nhận")
    expect(page.get_by_role("radio", name=re.compile("Community-1"))).to_be_checked()
    expect(page.get_by_role("radio", name=re.compile("Tắt nhận dạng người nói"))).to_be_visible()

    evidence_dir = Path(os.environ.get("ASR_UI_EVIDENCE_DIR", str(tmp_path)))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=evidence_dir / f"{artifact_name}-rerun-modal.png", animations="disabled")

    page.get_by_role("radio", name=re.compile("Tắt nhận dạng người nói")).check()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as response:
        page.locator("#transcription-rerun-submit").click()
    assert response.value.status == 202
    accepted = response.value.json()
    assert accepted["diarization_mode"] == "off"
    assert accepted["status"] == "accepted"
    request = response.value.request.post_data_json
    assert request["meeting_id"] == meeting_id
    assert request["language_hint"] == "en"
    assert request["asr_model"] == "auto"
    assert request["diarization_mode"] == "off"
    assert request["expected_generation_id"] == generation_id
    assert set(request) == {
        "meeting_id",
        "request_id",
        "language_hint",
        "asr_model",
        "diarization_mode",
        "expected_generation_id",
    }


def test_restored_rerun_replays_unavailable_original_choices_and_refreshes_capability(
    live, signed_in, worker
):
    _context, page = signed_in()
    meeting_id, generation_id, _segments = seed_completed_transcript(
        live.settings.data_dir, title="Restored rerun fixture"
    )
    storage_key = f"asr-studio:transcription-request:{meeting_id}"
    storage_value = json.dumps(
        {
            "requestId": "admitted-browser-rerun",
            "languageHint": "vi",
            "asrModel": "pho-whisper-large",
            "diarizationMode": "off",
            "expectedGenerationId": generation_id,
        }
    )
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(storage_key)}, JSON.stringify({storage_value}))"
    )
    capability_requests = []
    bodies = []

    def capability(route):
        capability_requests.append(route.request)
        route.fulfill(
            content_type="application/json",
            json={
                "contract_version": 1,
                "default_mode": "community-1",
                "explicit_mode_scope": "manual_transcript_rerun",
                "community_1": {"id": "community-1", "ready": True, "state": "ready"},
                "off": {
                    "id": "off",
                    "ready": False,
                    "state": "unavailable",
                    "reason": "worker_unavailable",
                },
            },
        )

    def uncertain_start(route):
        bodies.append(route.request.post_data_json)
        route.abort("connectionfailed")

    page.route(f"{live.url}/api/diarization-capability", capability)
    page.route(f"{live.url}/api/tasks", uncertain_start)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    page.get_by_role("button", name="Chạy lại", exact=True).click()

    expect(page.locator("#transcription-rerun-model")).to_have_value("pho-whisper-large")
    expect(page.locator("#transcription-rerun-model")).to_be_disabled()
    expect(page.locator("#transcription-rerun-language")).to_have_value("vi")
    expect(page.locator("#transcription-rerun-language")).to_be_disabled()
    off = page.get_by_role("radio", name=re.compile("Tắt nhận dạng người nói"))
    expect(off).to_be_disabled()
    expect(page.locator("#transcription-rerun-submit")).to_have_text("Xác minh yêu cầu chạy lại")
    expect(page.locator("#rerun-capability-status")).to_contain_text("Chưa có xác nhận từ server")
    expect(page.locator("#transcription-rerun-submit")).to_be_enabled()

    alert = page.locator("#transcription-error")
    status = page.locator("#transcription-status")
    start = page.locator("#start-transcription")
    page.locator("#transcription-rerun-submit").click()
    expect(alert).to_contain_text("Bấm lại để xác minh")
    expect(status).to_have_text("Transcript đã được lưu hoàn chỉnh.")
    expect(start).to_be_focused()
    start.click()
    expect(page.locator("#transcription-rerun-dialog")).to_be_visible()
    assert len(capability_requests) >= 2
    page.locator("#transcription-rerun-submit").click()
    expect(alert).to_contain_text("Bấm lại để xác minh")
    expect(status).to_have_text("Transcript đã được lưu hoàn chỉnh.")
    expect(start).to_be_focused()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert bodies[0] == {
        "meeting_id": meeting_id,
        "request_id": "admitted-browser-rerun",
        "language_hint": "vi",
        "asr_model": "pho-whisper-large",
        "diarization_mode": "off",
        "expected_generation_id": generation_id,
    }


@pytest.mark.parametrize("mode", ["community-1", "off"])
def test_rerun_refuses_empty_asr_weight_and_refreshes_ready_choices(live, signed_in, worker, mode):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    small = _prepare_browser_asr_model(data_dir.parent / "whisper-small")
    object.__setattr__(live.settings, "asr_cpu_model_path", small)
    meeting, generation, _segments = seed_completed_transcript(data_dir)
    prepare_recording(data_dir, meeting)
    page.goto(f"{live.url}/meetings/{meeting}#transcript")
    start = page.locator("#start-transcription")
    dialog = page.locator("#transcription-rerun-dialog")
    model = page.locator("#transcription-rerun-model")
    submit = page.locator("#transcription-rerun-submit")
    status = page.locator("#transcription-status")
    expect(status).to_have_text("Transcript đã được lưu hoàn chỉnh.")
    start.press("Enter")
    model.select_option("whisper-small")
    dialog.locator(f'[value="{mode}"]').check()
    expect(submit).to_be_enabled()

    (small / "model.bin").write_bytes(b"")
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as refused:
        submit.press("Enter")
    assert refused.value.status == 503
    assert refused.value.json()["detail"]["code"] == "model_unavailable"
    expect(start).to_be_focused()
    expect(status).to_have_text("Transcript đã được lưu hoàn chỉnh.")
    expect(page.locator("#transcription-error-message")).to_contain_text(
        "Model đã chọn chưa được chuẩn bị"
    )

    page.reload()
    expect(status).to_have_text("Transcript đã được lưu hoàn chỉnh.")
    start.press("Enter")
    expect(model.locator('option[value="whisper-small"]')).to_have_count(0)
    expect(model.locator('option[value="auto"]')).to_have_count(1)
    expect(model).to_have_value("auto")
    dialog.locator(f'[value="{mode}"]').check()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as accepted:
        submit.press("Enter")
    assert accepted.value.status == 202
    recipe = accepted.value.request.post_data_json
    assert recipe["asr_model"] == "auto"
    assert recipe["diarization_mode"] == mode
    assert recipe["expected_generation_id"] == generation
    assert recipe["request_id"] != refused.value.request.post_data_json["request_id"]


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_rollback_refusal_reopens_with_fresh_choices_and_request(
    live, signed_in, worker, monkeypatch, lang
):
    english = lang == "en"
    viewport = {"width": 390, "height": 844} if english else {"width": 1440, "height": 1000}
    _context, page = signed_in(viewport=viewport, color_scheme="dark" if english else "light")
    if english:
        page.get_by_role("button", name="Switch to English").click()
    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "0")
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    prepare_recording(live.settings.data_dir, meeting_id)
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/tasks")
            else None
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    start = page.locator("#start-transcription")
    dialog = page.locator("#transcription-rerun-dialog")
    submit = page.locator("#transcription-rerun-submit")
    cancel = page.locator("#transcription-rerun-cancel")
    status = dialog.get_by_role("status")
    off = dialog.get_by_role(
        "radio",
        name=re.compile("Turn off speaker recognition" if english else "Tắt nhận dạng người nói"),
    )
    community = dialog.get_by_role("radio", name=re.compile("Community-1"))
    start.click()
    expect(cancel).to_be_focused()
    off.check()

    monkeypatch.setenv("ASR_DIARIZATION_ROLLBACK_FENCE", "1")
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as refusal:
        submit.click()
    assert refusal.value.status == 503
    assert refusal.value.json()["detail"]["code"] == "diarization_rollback_fence"
    explanation = (
        "Running with speaker recognition off is temporarily unavailable. "
        "Open Run again to choose Community-1 if it is ready."
        if english
        else "Chạy với chế độ tắt nhận dạng người nói đang tạm khóa. "
        "Mở Chạy lại để chọn Community-1 nếu đã sẵn sàng."
    )
    expect(page.locator("#transcription-error-message")).to_have_text(explanation)
    expect(page.locator("#transcription-status")).to_have_text(
        "The complete transcript is saved." if english else "Transcript đã được lưu hoàn chỉnh."
    )
    expect(start).to_be_focused()
    storage_key = f"asr-studio:transcription-request:{meeting_id}"
    assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) is None

    start.click()
    expect(cancel).to_be_focused()
    expect(off).to_be_disabled()
    expect(off).to_have_accessible_name(
        "Turn off speaker recognition · Off is temporarily unavailable."
        if english
        else "Tắt nhận dạng người nói · Chế độ tắt đang tạm khóa."
    )
    expect(community).to_be_checked()
    expect(community).to_be_enabled()
    expect(status).to_contain_text(
        "The server confirmed these choices" if english else "Lựa chọn được xác nhận từ server"
    )
    expect(submit).to_have_text("Run again" if english else "Chạy lại")
    expect(submit).to_be_enabled()
    assert len(requests) == 1

    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as accepted:
        submit.click()
    assert accepted.value.status == 202
    expect(dialog).to_be_hidden()
    expect(page.locator("#transcription-error")).to_be_hidden()
    expect(page.locator("#transcription-status")).to_have_text(
        "Checking the worker before processing starts."
        if english
        else "Đang kiểm tra worker trước khi bắt đầu xử lý."
    )
    expect(page.locator("#transcription-status")).to_be_focused()
    assert len(requests) == 2
    assert [body["diarization_mode"] for body in requests] == ["off", "community-1"]
    assert all(body["expected_generation_id"] == generation_id for body in requests)
    assert requests[0]["request_id"] != requests[1]["request_id"]


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_community1_lost_after_readiness_offers_recovery(live, signed_in, worker, lang):
    english = lang == "en"
    viewport = {"width": 1440, "height": 1000} if english else {"width": 390, "height": 844}
    _context, page = signed_in(viewport=viewport, color_scheme="light" if english else "dark")
    if english:
        page.get_by_role("button", name="Switch to English").click()
    data_dir = live.settings.data_dir
    meeting, generation, _segments = seed_completed_transcript(data_dir)
    prepare_recording(data_dir, meeting)
    page.goto(f"{live.url}/meetings/{meeting}#transcript")
    start = page.locator("#start-transcription")
    dialog = page.locator("#transcription-rerun-dialog")
    submit = page.locator("#transcription-rerun-submit")
    community = dialog.locator('[value="community-1"]')
    off = dialog.locator('[value="off"]')
    status = page.locator("#transcription-status")
    completed_status = (
        "The complete transcript is saved." if english else "Transcript đã được lưu hoàn chỉnh."
    )
    expect(status).to_have_text(completed_status)
    start.press("Enter")
    expect(page.locator("#transcription-rerun-cancel")).to_be_focused()
    expect(community).to_be_enabled()
    expect(community).to_be_checked()

    # Change only the prepared artifact after the real readiness response was rendered.
    (live.settings.diarization_model_path / "embedding/pytorch_model.bin").unlink()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as refused:
        submit.press("Enter")
    assert refused.value.status == 503
    assert refused.value.json()["detail"]["code"] == "diarization_model_unavailable"
    assert refused.value.request.post_data_json["diarization_mode"] == "community-1"
    expect(dialog).to_be_hidden()
    expect(start).to_be_focused()
    expect(status).to_have_text(completed_status)
    expected = (
        "Community-1 is unavailable. Prepare the model on the server, or open Run again "
        "and turn off speaker recognition if that choice is available."
        if english
        else "Community-1 chưa sẵn sàng. Chuẩn bị model trên máy chủ, hoặc mở Chạy lại "
        "và tắt nhận dạng người nói nếu có thể chọn."
    )
    expect(page.locator("#transcription-error-message")).to_have_text(expected)
    storage_key = f"asr-studio:transcription-request:{meeting}"
    assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) is None

    start.press("Enter")
    expect(community).to_be_disabled()
    expect(community).to_be_checked()
    expect(off).to_be_enabled()
    expect(submit).to_be_disabled()
    off.check()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as accepted:
        submit.press("Enter")
    assert accepted.value.status == 202
    recipe = accepted.value.request.post_data_json
    assert recipe["diarization_mode"] == "off"
    assert recipe["expected_generation_id"] == generation
    assert recipe["request_id"] != refused.value.request.post_data_json["request_id"]


@pytest.mark.parametrize(("lang", "mode"), [("vi", "off"), ("en", "community-1")])
def test_missing_recording_refusal_restores_controls_and_saved_notes(
    live, signed_in, worker, lang, mode
):
    english = lang == "en"
    viewport = {"width": 1440, "height": 1000} if english else {"width": 390, "height": 844}
    _context, page = signed_in(viewport=viewport, color_scheme="light" if english else "dark")
    if english:
        page.get_by_role("button", name="Switch to English").click()
    data_dir = live.settings.data_dir
    meeting, revision, snapshot, _segments = seed_cited_notes(data_dir)
    audio = prepare_recording(data_dir, meeting)
    page.goto(f"{live.url}/meetings/{meeting}#transcript")
    expect(page.locator("#meeting-tab-transcript")).to_have_attribute("aria-selected", "true")
    start = page.locator("#start-transcription")
    dialog = page.locator("#transcription-rerun-dialog")
    cancel = page.locator("#transcription-rerun-cancel")
    submit = page.locator("#transcription-rerun-submit")
    status = page.locator("#transcription-status")
    expect(start).to_be_visible()
    expect(start).to_be_enabled()
    start.press("Enter")
    expect(cancel).to_be_focused()
    page.keyboard.press("Escape")
    expect(start).to_be_focused()
    page.keyboard.press("Enter")
    community = dialog.locator('[value="community-1"]')
    expect(community).to_be_enabled()
    if mode == "off":
        community.focus()
        page.keyboard.press("ArrowDown")
        off = dialog.locator('[value="off"]')
        expect(off).to_be_focused()
        expect(off).to_be_checked()
    expect(submit).to_be_enabled()
    backup = audio.with_suffix(".bak")
    audio.rename(backup)

    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as refusal:
        submit.press("Enter")
    assert refusal.value.status == 409
    assert refusal.value.json()["detail"]["code"] == "recording_unavailable"
    body = refusal.value.request.post_data_json
    assert body["diarization_mode"] == mode
    assert body["expected_generation_id"] == snapshot["generation_id"]
    expect(dialog).to_be_hidden()
    expect(page.locator("#transcription-error-message")).to_have_text(
        "The original recording could not be opened. Restore it before running again."
        if english
        else "Không thể mở bản ghi gốc. Khôi phục bản ghi rồi chạy lại."
    )
    expect(status).to_have_text(
        "The complete transcript is saved." if english else "Transcript đã được lưu hoàn chỉnh."
    )
    expect(start).to_be_focused()
    storage_key = f"asr-studio:transcription-request:{meeting}"
    assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) is None
    expect(page.locator(".segment-text").first).to_have_text("We need a launch plan.")
    if evidence_dir := os.environ.get("ASR_UI_EVIDENCE_DIR"):
        page.screenshot(
            path=Path(evidence_dir) / f"{lang}-recording-refusal.png", animations="disabled"
        )

    page.locator("#meeting-tab-notes").click()
    export = page.locator("#notes-export")
    expect(export).to_be_enabled()
    with page.expect_download() as download:
        export.click()
    assert Path(download.value.path()).read_text(encoding="utf-8") == revision["markdown"]
    page.locator("#meeting-tab-transcript").click()

    backup.rename(audio)
    start.press("Enter")
    expect(cancel).to_be_focused()
    expect(submit).to_have_text("Run again" if english else "Chạy lại")
    expect(submit).to_be_enabled()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as accepted:
        submit.press("Enter")
    assert accepted.value.status == 202
    assert accepted.value.request.post_data_json["request_id"] != body["request_id"]
    expect(page.locator("#transcription-error")).to_be_hidden()
    expect(status).to_be_focused()


def publish_fixture_run(data_dir, task_id, segments, **metadata):
    owner_id = "browser-fixture-run"
    task = claim_next_task(data_dir, owner_id=owner_id)
    assert task and task["id"] == task_id
    assert update_task_progress(
        data_dir,
        task_id,
        owner_id=owner_id,
        stage="publishing",
        elapsed_seconds=1.0,
        transcript_end_seconds=segments[-1]["end_seconds"] if segments else 0.0,
        segment_count=len(segments),
    )
    result = publish_complete_generation(
        data_dir,
        task_id,
        owner_id=owner_id,
        segments=segments,
        language_probability=None,
        elapsed_seconds=1.0,
        **metadata,
    )
    assert result
    return result


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_transcript_details_follow_saved_diarization_provenance(live, signed_in, worker, lang):
    english = lang == "en"
    _context, page = signed_in()
    if english:
        page.get_by_role("button", name="Switch to English").click()
    data_dir = live.settings.data_dir
    meeting_id, legacy, _segments = seed_completed_transcript(data_dir)
    prepare_recording(data_dir, meeting_id)
    with sqlite3.connect(database_path(data_dir)) as db:
        db.execute(
            "UPDATE transcript_generations SET diarization_model_name = NULL, "
            "regular_turn_count = 0, alignment_policy = NULL WHERE id = ?",
            (legacy,),
        )
        db.execute(
            "UPDATE transcript_segments SET speaker_id = NULL WHERE generation_id = ?", (legacy,)
        )

    accepted = page.request.post(
        f"{live.url}/api/tasks",
        headers={"Origin": live.url},
        data={
            "meeting_id": meeting_id,
            "request_id": "browser-fixture-off",
            "diarization_mode": "off",
            "expected_generation_id": legacy,
        },
    )
    assert accepted.status == 202
    segments = [
        {"id": uuid.uuid4().hex, "text": text, "start_seconds": start, "end_seconds": start + 1}
        for start, text in enumerate(["Unassigned speech.", "Manually assigned speech."])
    ]
    off = publish_fixture_run(
        data_dir,
        accepted.json()["id"],
        segments,
        alignment_policy=None,
        diarization_model_name=None,
    )["id"]
    speaker = add_current_generation_speaker(
        data_dir,
        meeting_id,
        expected_generation_id=off,
        expected_source_version=1,
        display_name="Morgan",
    )
    reassign_current_generation_speaker(
        data_dir,
        meeting_id,
        segments[1]["id"],
        expected_generation_id=off,
        expected_source_version=speaker["source_version"],
        expected_assignment_version=1,
        speaker_id=speaker["speaker_id"],
    )

    off_label = "Speaker recognition off" if english else "Đã tắt nhận dạng người nói"
    unknown = "Unknown" if english else "Chưa xác định"
    summary = page.get_by_text(
        "Transcript details" if english else "Chi tiết transcript", exact=True
    )
    meta = page.locator("#generation-meta")
    speakers = page.locator(".segment-speaker")
    generations = page.locator("#generation-selector")
    evidence_dir = os.environ.get("ASR_UI_EVIDENCE_DIR")

    page.goto(f"{live.url}/meetings/{meeting_id}")
    open_processing_details(page)
    expect(summary).to_be_visible()
    summary.press("Enter")
    expect(summary).to_be_focused()
    expect(meta).to_be_visible()
    expect(generations).to_have_value(off)
    expect(meta).to_contain_text(off_label)
    expect(speakers).to_have_text([unknown, "Morgan"])
    if evidence_dir:
        page.locator("#transcript-generation").screenshot(
            path=Path(evidence_dir) / f"{lang}-current-off.png"
        )
    generations.select_option(legacy)
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(meta).not_to_contain_text(off_label)
    expect(meta).not_to_contain_text("Diarization")
    expect(speakers).to_have_text([unknown, unknown])
    if evidence_dir:
        page.locator("#transcript-generation").screenshot(
            path=Path(evidence_dir) / f"{lang}-legacy-unknown.png"
        )

    # Publish a synthetic no-speech run through the same task and generation APIs.
    generations.select_option(off)
    page.locator("#start-transcription").click()
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and reply.url.endswith("/api/tasks")
    ) as accepted:
        page.locator("#transcription-rerun-submit").click()
    assert accepted.value.status == 202
    community = publish_fixture_run(data_dir, accepted.value.json()["id"], [])
    expect(generations).to_have_value(community["id"], timeout=8_000)
    expect(meta).to_contain_text("Diarization · Community-1")
    expect(page.locator("#transcript-empty")).to_be_visible()
    expect(page.locator("#transcript-empty")).to_have_text(
        "The model returned no speech segments."
        if english
        else "Model không trả về đoạn lời nói nào."
    )

    generations.select_option(off)
    open_processing_details(page)
    expect(page.locator("#generation-mode")).to_have_text(
        "Historical version · Read-only." if english else "Bản lịch sử · Chỉ đọc."
    )
    expect(meta).to_be_visible()
    expect(meta).to_contain_text(off_label)
    expect(speakers).to_have_text([unknown, "Morgan"])
    if evidence_dir:
        page.locator("#transcript-generation").screenshot(
            path=Path(evidence_dir) / f"{lang}-historical-off.png"
        )


def test_an_unprepared_model_cannot_start_and_says_how_to_prepare_it(live, signed_in, worker):
    _context, page = signed_in(viewport={"width": 360, "height": 800})
    meeting_id = seed_meeting(
        live.settings.data_dir, "Unprepared model", "2026-10-17", duration_seconds=30.0
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    start = page.get_by_role("button", name="Chép lời")
    guidance = page.locator("#transcription-model-guidance")
    expect(start).to_be_enabled()
    expect(guidance).to_be_hidden()
    status = page.locator("#transcription-status")
    expect(status).to_contain_text(INVITES_START)

    selector.select_option("pho-whisper-large")
    expect(selector.locator('option[value="pho-whisper-large"]')).to_have_text(
        "PhoWhisper-large · chưa sẵn sàng"
    )
    expect(start).to_be_disabled()
    expect(guidance).to_contain_text("chưa được chuẩn bị")
    # The button is off and the guidance says what to do: nothing may still invite a click on it.
    expect(status).to_have_text("Bản ghi đã lưu.")
    command = page.get_by_label("Lệnh chuẩn bị model")
    assert PREPARE_FLAG in command.input_value()
    assert (
        page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        <= 0
    )

    # The server refuses it too: the disabled button is not the only guard.
    refused = page.request.post(
        f"{live.url}/api/tasks",
        headers={"Origin": live.url},
        data={
            "meeting_id": meeting_id,
            "request_id": "browser-unprepared-1",
            "asr_model": "pho-whisper-large",
        },
    )
    assert refused.status == 503

    selector.select_option("auto")
    expect(start).to_be_enabled()
    expect(guidance).to_be_hidden()
    expect(status).to_contain_text(INVITES_START)


def test_an_interrupted_run_invites_a_retry_only_with_a_usable_model(live, signed_in, worker):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    meeting_id = seed_meeting(data_dir, "Interrupted", "2026-10-17", duration_seconds=30.0)
    accepted = page.request.post(
        f"{live.url}/api/tasks",
        headers={"Origin": live.url},
        data={"meeting_id": meeting_id, "request_id": "browser-interrupted-1"},
    )
    assert accepted.ok, accepted.text()
    owner_id = f"browser-worker-{uuid.uuid4().hex}"
    task = claim_next_task(data_dir, owner_id=owner_id)
    assert finish_interrupted_task(data_dir, task["id"], owner_id=owner_id, elapsed_seconds=1)

    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    status = page.locator("#transcription-status")
    expect(status).to_contain_text(INVITES_RETRY)

    selector.select_option("pho-whisper-large")
    expect(status).to_have_text("Worker đã dừng giữa tác vụ. Không tự chạy lại.")

    selector.select_option("auto")
    expect(status).to_contain_text(INVITES_RETRY)


def test_only_a_transcript_without_confidence_scores_qualifies_its_review_count(
    live, signed_in, worker
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    meeting_id = seed_meeting(data_dir, "Review count", "2026-10-17", duration_seconds=30.0)
    generation_ids = []
    for number, avg_logprob in ((1, -0.2), (2, None)):
        accepted = page.request.post(
            f"{live.url}/api/tasks",
            headers={"Origin": live.url},
            data={"meeting_id": meeting_id, "request_id": f"browser-review-count-{number}"},
        )
        assert accepted.ok, accepted.text()
        generation_ids.append(finish_run(live, f"Bản {number}.", avg_logprob=avg_logprob))

    page.goto(f"{live.url}/meetings/{meeting_id}")
    title = page.locator("#generation-title")
    meta = page.locator("#generation-meta")
    expect(title).to_have_text("Transcript #2")
    expect(meta).to_contain_text(NO_CONFIDENCE)
    page.locator("#generation-selector").select_option(generation_ids[0])
    expect(title).to_have_text("Transcript #1")
    expect(meta).not_to_contain_text(NO_CONFIDENCE)

    # The same warning where the model is chosen, before any count exists.
    open_processing_details(page)
    page.get_by_role("combobox", name="Model ASR", exact=True).select_option("qwen3-asr-1.7b")
    expect(page.locator("#transcription-model-help")).to_contain_text("Không có điểm tin cậy")


def test_renaming_the_meeting_keeps_the_selector_and_its_guard(live, signed_in, worker):
    _context, page = signed_in()
    meeting_id = seed_meeting(
        live.settings.data_dir, "Before rename", "2026-10-17", duration_seconds=30.0
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    start = page.get_by_role("button", name="Chép lời")
    selector.select_option("pho-whisper-large")
    expect(start).to_be_disabled()

    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa thông tin").click()
    page.locator("#edit-title").fill("After rename")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.")

    expect(selector).to_be_visible()
    expect(selector).to_have_value("pho-whisper-large")
    expect(start).to_be_disabled()
    expect(page.locator("#transcription-model-guidance")).to_be_visible()
    expect(page.locator("#runtime-backend")).to_have_text(
        "GPU · CUDA · bfloat16", use_inner_text=False
    )


def test_a_model_removed_after_the_page_loaded_is_refused_and_the_page_recovers(
    live, signed_in, worker
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    prepared = _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large")
    object.__setattr__(live.settings, "asr_phowhisper_model_path", prepared)
    meeting_id = seed_meeting(data_dir, "Stale list", "2026-10-17", duration_seconds=30.0)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    start = page.get_by_role("button", name="Chép lời")
    selector.select_option("pho-whisper-large")
    expect(start).to_be_enabled()

    (prepared / "model.bin").rename(prepared / "moved-away")
    start.click()
    expect(page.locator("#transcription-error-message")).to_contain_text(
        "Model đã chọn chưa được chuẩn bị"
    )
    # No task exists, so the choice is free again instead of being locked to a dead request.
    expect(selector).to_be_enabled()
    selector.select_option("auto")
    expect(start).to_be_enabled()

    page.reload()
    selector.select_option("pho-whisper-large")
    expect(start).to_be_disabled()
    expect(page.locator("#transcription-model-guidance")).to_be_visible()


def _choose_default(page, live, asr_model):
    reply = page.request.put(
        f"{live.url}/api/asr-models/default",
        data={"asr_model": asr_model},
        headers={"Origin": live.url},
    )
    assert reply.ok, reply.text()


def test_automatic_runs_the_default_from_settings_and_one_run_can_still_differ(
    live, signed_in, worker
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    object.__setattr__(
        live.settings,
        "asr_phowhisper_model_path",
        _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large"),
    )
    _choose_default(page, live, "pho-whisper-large")
    meeting_id = seed_meeting(data_dir, "Default model", "2026-10-17", duration_seconds=30.0)
    prepare_recording(data_dir, meeting_id)

    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_role("combobox", name="Model ASR", exact=True)
    expect(selector).to_have_value("auto")
    expect(page.locator("#transcription-model-help")).to_have_text(
        "Theo Cài đặt: pho-whisper-large."
    )
    page.get_by_role("button", name="Chép lời").click()
    expect(page.get_by_role("button", name="Hủy")).to_be_visible()
    finish_run(live, "Theo mặc định.")
    meta = page.locator("#generation-meta")
    expect(meta).to_contain_text("pho-whisper-large · GPU · CUDA · float16", timeout=8_000)

    # One run with another model leaves the default where it was.
    open_processing_details(page)
    selector.select_option("whisper-large-v3")
    page.get_by_role("button", name="Chạy lại").click()
    expect(page.get_by_role("button", name="Hủy")).to_be_visible()
    page.locator("#transcription-rerun-submit").click()
    finish_run(live, "Một lần khác.")
    expect(meta).to_contain_text("whisper-large-v3 · GPU · CUDA · float16", timeout=8_000)
    body = page.request.get(f"{live.url}/api/asr-models").json()
    assert body["default_model"] == "pho-whisper-large"


def test_automatic_is_refused_with_guidance_when_the_chosen_default_is_gone(
    live, signed_in, worker
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    pho = _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large")
    object.__setattr__(live.settings, "asr_phowhisper_model_path", pho)
    _choose_default(page, live, "pho-whisper-large")
    shutil.rmtree(pho)
    meeting_id = seed_meeting(data_dir, "Default gone", "2026-10-17", duration_seconds=30.0)

    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.get_by_role("combobox", name="Model ASR", exact=True)).to_have_value("auto")
    command = page.get_by_label("Lệnh chuẩn bị model")
    expect(command.first).to_have_value(re.compile("--asr-model pho-whisper-large"))
    assert page.request.get(f"{live.url}/api/asr-models").json()["default_model"] == (
        "pho-whisper-large"
    )


def test_the_open_meeting_reads_the_default_again_when_settings_closes(live, signed_in, worker):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    data_dir = live.settings.data_dir
    object.__setattr__(
        live.settings,
        "asr_phowhisper_model_path",
        _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large"),
    )
    meeting_id = seed_meeting(data_dir, "Open meeting", "2026-10-17", duration_seconds=30.0)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    note = page.locator("#transcription-model-help")
    expect(note).to_have_text("Theo phần cứng của máy: qwen3-asr-1.7b.")

    page.locator("#settings-open").click()
    page.get_by_role("tab", name="ASR Model").click()
    page.get_by_label("Model mặc định").select_option("pho-whisper-large")
    expect(page.locator("#asr-default")).to_contain_text("PhoWhisper-large")
    page.keyboard.press("Escape")

    expect(note).to_have_text("Theo Cài đặt: pho-whisper-large.")


def test_a_broken_default_tells_the_create_dialog_before_the_upload(live, signed_in, worker):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    data_dir = live.settings.data_dir
    pho = _prepare_browser_asr_model(data_dir.parent / "pho-whisper-large")
    object.__setattr__(live.settings, "asr_phowhisper_model_path", pho)
    _choose_default(page, live, "pho-whisper-large")
    shutil.rmtree(pho)
    page.goto(f"{live.url}/")
    page.locator("header [data-open-meeting-dialog], .header-create").first.click()
    expect(page.locator("#create-asr-model-help")).to_contain_text("Mở Cài đặt")


@pytest.mark.parametrize("layout, width, height", [("desktop", 1280, 900), ("mobile", 390, 844)])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_asr_catalog_and_community_dependency(live, signed_in, layout, width, height, scheme):
    """Serve a synthetic catalog through loopback; keep the real transcript name editor."""
    _context, page = signed_in(viewport={"width": width, "height": height}, color_scheme=scheme)
    english = scheme == "dark"
    if english:
        page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    note = (
        "Transcripts require Community-1 to separate speakers."
        if english
        else "Transcript cần Community-1 để tách người nói."
    )
    name_note = (
        "Renaming updates only the speaker’s display name."
        if english
        else "Đổi tên chỉ cập nhật tên hiển thị của người nói."
    )
    ready, install, unsafe = "fixture+v2", "fixture+compact", "fixture%2Fprivate"
    state = {
        "asr_models": [
            {"id": "auto", "model_name": "fixture-weights", "ready": True, "chosen": True},
            {"id": "fixture-base", "model_name": "fixture-weights", "ready": True},
            {"id": ready, "label": "Fixture V2", "model_name": "fixture-weights", "ready": True},
            {
                "id": install,
                "label": "/private/weights",
                "model_name": "Fixture Compact",
                "ready": False,
                "reason": "not_prepared",
                "install": {"state": "idle"},
            },
            {"id": unsafe, "label": "Unsafe row", "ready": True},
        ],
        "default_model": ready,
        "hardware_model": "fixture-weights",
        "installer": {"available": True, "models_writable": True},
    }
    meeting_id, generation_id, _segments = seed_completed_transcript(
        live.settings.data_dir, title="ASR fixture meeting"
    )
    default_attempts, install_requests = [], []
    install_row = state["asr_models"][3]

    def default_response(route):
        assert route.request.method == "PUT"
        default_attempts.append(route.request.post_data_json["asr_model"])
        if len(default_attempts) == 1:
            route.fulfill(status=409, json={"detail": {"code": "not_prepared"}})
        else:
            state["default_model"] = default_attempts[-1]
            route.fulfill(json={"default_model": state["default_model"]})

    def install_response(route):
        install_requests.append((route.request.method, route.request.url))
        install_row["install"] = (
            {"state": "running", "phase": "downloading", "bytes_done": 32, "bytes_total": 128}
            if route.request.method == "POST"
            else {"state": "idle"}
        )
        route.fulfill(status=202, json={"install": install_row["install"]})

    def meeting_response(route):
        response = route.fetch()
        body = response.json()
        body["transcription"]["asr_models"] = state["asr_models"]
        route.fulfill(response=response, json=body)

    def evidence(label):
        directory = os.environ.get("ASR_UI_EVIDENCE_DIR")
        if directory:
            target = Path(directory) / f"{layout}-{scheme}-{label}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=target, animations="disabled")

    page.route("**/api/asr-models", lambda route: route.fulfill(json=state))
    page.route("**/api/asr-models/default", default_response)
    page.route("**/api/asr-models/*/install", install_response)
    page.route(f"**/api/meetings/{meeting_id}", meeting_response)
    page.goto(f"{live.url}/")
    expect(page.locator("html")).to_have_attribute("data-theme", scheme)
    gear = page.locator("#settings-open")
    gear.click()
    page.get_by_role("tab", name="ASR Model").click()
    expect(page.locator("#asr-transcript-dependency")).to_have_text(note)
    rows = page.locator("#asr-models > li")
    expect(rows).to_have_count(3)
    expect(rows.locator("strong")).to_have_text(
        ["fixture-weights", "Fixture V2", "Fixture Compact"]
    )
    badge = rows.locator('[data-kind="default"]')
    expect(badge).to_have_count(1)
    assert badge.evaluate("element => element.closest('li').dataset.model") == ready

    default = page.locator("#asr-default-select")
    expect(default.locator(f'option[value="{install}"]')).to_be_disabled()
    default.focus()
    default.select_option("fixture-base")
    expect(page.locator("#asr-default-refusal")).to_be_visible()
    expect(default).to_have_value(ready)
    expect(default).to_be_focused()
    default.select_option("fixture-base")
    expect(page.locator("#asr-default-refusal")).to_be_hidden()
    expect(page.locator("#asr-default")).to_contain_text("fixture-weights")
    expect(default).to_be_focused()
    assert default_attempts == ["fixture-base", "fixture-base"]
    default.select_option(ready)
    expect(page.locator("#asr-default")).to_contain_text("Fixture V2")

    compact = rows.filter(has_text="Fixture Compact")
    button = compact.get_by_role("button", name="Install model" if english else "Cài đặt model")
    button.press("Enter")
    progress = compact.locator("progress")
    expect(progress).to_have_js_property("value", 0.25)
    expect(compact.locator(".asr-progress-detail")).to_contain_text("/")
    cancel = compact.get_by_role("button", name="Cancel" if english else "Hủy")
    expect(cancel).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
    evidence("settings-progress")
    cancel.press("Enter")
    expect(button).to_be_focused()
    url = f"{live.url}/api/asr-models/fixture%2Bcompact/install"
    assert install_requests == [("POST", url), ("DELETE", url)]
    page.keyboard.press("Escape")
    expect(gear).to_be_focused()

    create = page.locator(".header-create")
    create.click()
    expect(page.locator("#auto-transcribe-help")).to_contain_text(note)
    selector = page.locator("#create-asr-model")
    expect(selector.locator("option")).to_have_count(4)
    assert [option.get_attribute("value") for option in selector.locator("option").all()] == [
        "auto",
        "fixture-base",
        ready,
        install,
    ]
    expect(selector.locator(f'option[value="{install}"]')).to_be_disabled()
    selector.select_option(ready)
    expect(selector).to_have_value(ready)
    if (layout, scheme) == ("mobile", "dark"):
        selector.scroll_into_view_if_needed()
        evidence("new-meeting")
    page.keyboard.press("Escape")
    expect(create).to_be_focused()

    page.goto(f"{live.url}/meetings/{meeting_id}")
    open_processing_details(page)
    selector = page.locator("#transcription-model")
    expect(selector.locator("option")).to_have_count(4)
    expect(selector.locator(f'option[value="{ready}"]')).to_contain_text("Fixture V2")
    selector.select_option(ready)
    expect(selector).to_have_value(ready)
    expect(page.locator("#transcription-dependency")).to_have_text(note)
    expect(page.locator("#speaker-name-help")).to_have_text(name_note)
    if (layout, scheme) == ("desktop", "light"):
        page.locator("#speaker-name-help").scroll_into_view_if_needed()
        evidence("rerun-and-names")

    context = page.locator("#transcript-context")
    if not context.evaluate("element => element.open"):
        context.locator(":scope > summary").click()
    page.locator("#speaker-catalog .speaker-chip").press("Enter")
    name = page.locator(".speaker-name-form input")
    expect(name).to_be_focused()
    name.fill("Alex Fixture")
    with page.expect_response(
        lambda response: response.request.method == "PATCH" and "/speakers/" in response.url
    ) as renamed:
        name.press("Enter")
    assert renamed.value.ok
    path = f"/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers/SPEAKER_00"
    assert renamed.value.url.endswith(path)
    chip = page.locator("#speaker-catalog .speaker-chip")
    expect(chip).to_contain_text("Alex Fixture")
    expect(chip).to_be_focused()
    expect(selector).to_have_value(ready)
    expect(page.locator("#transcription-dependency")).to_have_text(note)
