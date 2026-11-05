"""Real-browser checks for choosing an ASR model per run and comparing the resulting transcripts."""

from __future__ import annotations

import os
import re
import shutil
import uuid

import pytest
from playwright.sync_api import expect
from seeding import prepare_diarization_model, seed_meeting
from test_browser_history import _prepare_browser_asr_model
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.runtime import RuntimeProfile, read_runtime_profile, write_runtime_profile
from asr_studio.tasks import (
    claim_next_task,
    finish_interrupted_task,
    publish_complete_generation,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser

PREPARE_FLAG = "--asr-model pho-whisper-large"
INVITES_START = "Bấm “Bắt đầu chép lời”"
INVITES_RETRY = "hãy bấm để thử lại"
NO_CONFIDENCE = "“cần kiểm tra” không xét độ tin cậy của chữ"


@pytest.fixture
def worker(live):
    """A healthy GPU worker whose default model is prepared, seen by the running server."""
    data_dir = live.settings.data_dir
    default_model = _prepare_browser_asr_model(data_dir.parent / "whisper-large-v3")
    # The server keeps this settings object and reads the model paths on each request.
    object.__setattr__(live.settings, "asr_gpu_model_path", default_model)
    object.__setattr__(
        live.settings,
        "diarization_model_path",
        prepare_diarization_model(data_dir.parent / "diarization-model"),
    )
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
    selector = page.get_by_label("Model ASR")
    expect(selector).to_have_value("auto")
    expect(selector.locator("option")).to_have_text(
        [
            "Tự động · sẵn sàng",
            "Whisper large-v3 · sẵn sàng",
            "Whisper small · chưa sẵn sàng",
            "PhoWhisper-large · sẵn sàng",
            "Qwen3-ASR-1.7B · chưa sẵn sàng",
        ]
    )
    expect(page.locator("#transcription-model-help")).to_have_text(
        "Theo phần cứng của máy: whisper-large-v3."
    )

    page.get_by_role("button", name="Bắt đầu chép lời").click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
    expect(selector).to_be_disabled()
    first_generation = finish_run(live, "Bản chép lời đầu tiên.")
    meta = page.locator("#generation-meta")
    expect(page.locator("#generation-title")).to_have_text("Transcript #1", timeout=8_000)
    expect(meta).to_contain_text("whisper-large-v3 · GPU · CUDA · float16")

    selector.select_option("pho-whisper-large")
    expect(page.locator("#transcription-model-help")).to_contain_text("Tối ưu cho tiếng Việt")
    expect(page.locator("#transcription-model-help")).to_contain_text("Không dành cho English")
    page.get_by_role("button", name="Chạy lại bản ghi").click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
    expect(selector).to_have_value("pho-whisper-large")
    second_generation = finish_run(live, "Bản chép lời thứ hai.")
    expect(page.locator("#generation-title")).to_have_text("Transcript #2", timeout=8_000)
    expect(meta).to_contain_text("pho-whisper-large · GPU · CUDA · float16")
    assert [body["asr_model"] for body in sent] == ["auto", "pho-whisper-large"]

    # Each transcript stays readable under the model that produced it.
    generations = page.locator("#generation-selector")
    expect(generations.locator("option")).to_have_text(
        [
            "Transcript #1 · whisper-large-v3 · lịch sử",
            "Transcript #2 · pho-whisper-large · hiện tại",
        ]
    )
    generations.select_option(first_generation)
    expect(page.locator("#generation-title")).to_have_text("Transcript #1")
    expect(meta).to_contain_text("whisper-large-v3 · GPU · CUDA · float16")
    expect(page.locator(".segment-text")).to_have_text("Bản chép lời đầu tiên.")
    expect(page.locator("#generation-mode")).to_have_text("Bản lịch sử · Chỉ đọc.")
    generations.select_option(second_generation)
    expect(page.locator("#generation-title")).to_have_text("Transcript #2")
    expect(page.locator(".segment-text")).to_have_text("Bản chép lời thứ hai.")

    # The choice belonged to that run: the system default is untouched and a new page starts over.
    assert read_runtime_profile(data_dir).model_path == worker
    page.reload()
    expect(selector).to_have_value("auto")


def test_an_unprepared_model_cannot_start_and_says_how_to_prepare_it(live, signed_in, worker):
    _context, page = signed_in(viewport={"width": 360, "height": 800})
    meeting_id = seed_meeting(
        live.settings.data_dir, "Unprepared model", "2026-10-17", duration_seconds=30.0
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_label("Model ASR")
    start = page.get_by_role("button", name="Bắt đầu chép lời")
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
    selector = page.get_by_label("Model ASR")
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
    page.get_by_label("Model ASR").select_option("qwen3-asr-1.7b")
    expect(page.locator("#transcription-model-help")).to_contain_text(
        "đừng so số này với model khác"
    )


def test_renaming_the_meeting_keeps_the_selector_and_its_guard(live, signed_in, worker):
    _context, page = signed_in()
    meeting_id = seed_meeting(
        live.settings.data_dir, "Before rename", "2026-10-17", duration_seconds=30.0
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_label("Model ASR")
    start = page.get_by_role("button", name="Bắt đầu chép lời")
    selector.select_option("pho-whisper-large")
    expect(start).to_be_disabled()

    page.locator("#meeting-actions").click()
    page.get_by_role("button", name="Sửa tên và ngày").click()
    page.locator("#edit-title").fill("After rename")
    page.get_by_role("button", name="Lưu thay đổi").click()
    expect(page.locator("#edit-status")).to_have_text("Đã lưu tên và ngày họp.")

    expect(selector).to_be_visible()
    expect(selector).to_have_value("pho-whisper-large")
    expect(start).to_be_disabled()
    expect(page.locator("#transcription-model-guidance")).to_be_visible()
    expect(page.locator("#runtime-backend")).to_have_text(
        "GPU · CUDA · float16", use_inner_text=False
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
    selector = page.get_by_label("Model ASR")
    start = page.get_by_role("button", name="Bắt đầu chép lời")
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

    page.goto(f"{live.url}/meetings/{meeting_id}")
    selector = page.get_by_label("Model ASR")
    expect(selector).to_have_value("auto")
    expect(page.locator("#transcription-model-help")).to_have_text(
        "Theo Cài đặt: pho-whisper-large."
    )
    page.get_by_role("button", name="Bắt đầu chép lời").click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
    finish_run(live, "Theo mặc định.")
    meta = page.locator("#generation-meta")
    expect(meta).to_contain_text("pho-whisper-large · GPU · CUDA · float16", timeout=8_000)

    # One run with another model leaves the default where it was.
    selector.select_option("whisper-large-v3")
    page.get_by_role("button", name="Chạy lại bản ghi").click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
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
    expect(page.get_by_label("Model ASR")).to_have_value("auto")
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
    expect(note).to_have_text("Theo phần cứng của máy: whisper-large-v3.")

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
