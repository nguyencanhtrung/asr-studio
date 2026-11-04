"""Real-browser checks for the transcription progress bar, the notes step list and their controls.

Tasks are seeded the way a worker leaves them (claimed, then progress reported), so the page
reads them through the real task and notes endpoints and its own polling.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from urllib.parse import urlsplit

import pytest
from notes_helpers import seed_completed_transcript
from playwright.sync_api import expect
from seeding import prepare_diarization_model, seed_meeting
from test_browser_history import _prepare_browser_asr_model
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes_provider import accept, claim

from asr_studio.notes import publish_notes_revision
from asr_studio.runtime import RuntimeProfile, write_runtime_profile
from asr_studio.tasks import (
    accept_transcription_task,
    cancel_task,
    claim_next_task,
    fail_task,
    finish_canceled_task,
    finish_interrupted_task,
    update_task_progress,
)
from asr_studio.worker import acquire_worker_lock, write_heartbeat

pytestmark = pytest.mark.browser

STEP_NAMES = ["Nhận yêu cầu", "Khởi chạy", "Tạo notes", "Lưu notes"]
STATE_TEXT = {
    "done": "Đã xong",
    "current": "Đang chạy",
    "upcoming": "Chưa tới",
    "failed": "Thất bại",
    "canceled": "Đã hủy",
    "interrupted": "Bị gián đoạn",
}
CONTRAST_JS = r"""selector => {
  const channels = value => value.match(/[\d.]+/g).map(Number);
  const linear = c => (c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  const luminance = ([r, g, b]) => 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  const backdrop = element => {
    for (let node = element; node; node = node.parentElement) {
      const [r, g, b, a = 1] = channels(getComputedStyle(node).backgroundColor);
      if (a > 0) return [r, g, b];
    }
    return [255, 255, 255];
  };
  return [...document.querySelectorAll(selector)].filter(element => element.offsetParent).map(
    element => {
      const [high, low] = [
        luminance(channels(getComputedStyle(element).color)),
        luminance(backdrop(element)),
      ].sort((a, b) => b - a);
      return (high + 0.05) / (low + 0.05);
    }
  );
}"""


def running_transcription(live, *, duration=100.0, stage="transcribing", end=25.0):
    """A meeting whose transcription a worker has claimed and reported progress on."""
    data_dir = live.settings.data_dir
    meeting_id = seed_meeting(data_dir, "Task progress", "2026-10-17", duration_seconds=duration)
    owner_id = f"progress-worker-{uuid.uuid4().hex}"
    task = accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id=owner_id,
        model_path=_prepare_browser_asr_model(data_dir.parent / "asr-model"),
        diarization_model_path=prepare_diarization_model(data_dir.parent / "diarization-model"),
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed is not None and claimed["id"] == task["id"]
    report(live, task["id"], owner_id, stage, end)
    return meeting_id, task["id"], owner_id


def report(live, task_id, owner_id, stage, end, elapsed=1.0):
    assert update_task_progress(
        live.settings.data_dir,
        task_id,
        owner_id=owner_id,
        stage=stage,
        elapsed_seconds=elapsed,
        transcript_end_seconds=end,
        segment_count=1,
    )


def running_notes(live, *, stage="generating_notes", provider="chatgpt", elapsed=12.34):
    """A meeting whose notes task a worker has claimed and moved to the given stage."""
    data_dir = live.settings.data_dir
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    task = accept(
        data_dir,
        meeting_id,
        generation_id,
        request_id=f"progress-{uuid.uuid4().hex}",
        provider=provider,
    )
    owner_id = claim(data_dir, task["id"])
    report(live, task["id"], owner_id, stage, 0, elapsed)
    return meeting_id, task["id"], owner_id


def end_notes_run(live, task_id, owner_id, ending):
    data_dir = live.settings.data_dir
    if ending == "failed":
        assert fail_task(
            data_dir,
            task_id,
            owner_id=owner_id,
            error_code="provider_unavailable",
            message="Provider text from the server.",
            elapsed_seconds=20,
        )
    elif ending == "canceled":
        assert cancel_task(data_dir, task_id)["status"] == "stopping"
        assert finish_canceled_task(data_dir, task_id, owner_id=owner_id, elapsed_seconds=20)
    else:
        assert finish_interrupted_task(data_dir, task_id, owner_id=owner_id, elapsed_seconds=20)


def expect_steps(page, *states, timeout=8_000):
    """Four visible steps, each with the given state in its data attribute and in its text."""
    items = page.locator("#notes-steps > li:not([hidden])")
    expect(items).to_have_count(4)
    for index, (name, state) in enumerate(zip(STEP_NAMES, states, strict=True)):
        item = items.nth(index)
        expect(item).to_contain_text(name)
        expect(item).to_have_attribute("data-step-state", state, timeout=timeout)
        expect(item.locator(".notes-step-state")).to_have_text(STATE_TEXT[state])
        if state == "current":
            expect(item).to_have_attribute("aria-current", "step")
        else:
            expect(item).not_to_have_attribute("aria-current", "step")


def without_duration(page, duration):
    """Answer every task read with the recording length missing or zeroed."""

    def blank(payload):
        if isinstance(payload, dict):
            if "audio_duration_seconds" in payload:
                payload["audio_duration_seconds"] = duration
            for value in payload.values():
                blank(value)

    def patch(route):
        payload = route.fetch().json()
        blank(payload)
        route.fulfill(json=payload)

    page.route(re.compile(r"/api/(meetings|tasks)/[0-9a-f]{32}$"), patch)


def rgb(token):
    return "rgb({}, {}, {})".format(*(int(token[i : i + 2], 16) for i in (1, 3, 5)))


def theme_tokens(page):
    return page.evaluate(
        """() => Object.fromEntries(
          ["--accent", "--hairline", "--success", "--danger"].map(name => [
            name,
            getComputedStyle(document.documentElement).getPropertyValue(name).trim(),
          ])
        )"""
    )


def overflow(page):
    return page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def record_requests(page):
    requests = []
    page.on(
        "request", lambda request: requests.append((request.method, urlsplit(request.url).path))
    )
    return requests


def assert_only_existing_polls(requests, *polls, window):
    """Only GETs of polls the page already ran, or the provider-choice loads; each about 1/s."""
    known_reads = ("/api/notes-preferences", "/api/providers")
    unexpected = {
        request
        for request in requests
        if request[0] != "GET" or not (request[1] in polls or request[1].startswith(known_reads))
    }
    assert not unexpected, unexpected
    for poll in polls:
        assert requests.count(("GET", poll)) <= window + 2


def test_progress_follows_the_existing_poll_without_new_requests(live, signed_in):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_transcription(live, end=25)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    bar = page.get_by_role("progressbar", name="Đang chép lời")
    percent = page.locator("#task-progress-percent")
    expect(percent).to_have_text("25%")
    expect(bar).to_have_js_property("value", 0.25)
    expect(bar).to_have_js_property("max", 1)

    requests = record_requests(page)
    started = time.monotonic()
    # 99.8 s of 100 s must not read 100 %; an end past the recording length clamps to 100 %.
    for end, text, value in [(60, "60%", 0.6), (99.8, "99%", 0.998), (130, "100%", 1)]:
        report(live, task_id, owner_id, "transcribing", end)
        expect(percent).to_have_text(text, timeout=8_000)
        expect(bar).to_have_js_property("value", value)
    report(live, task_id, owner_id, "starting_diarization", 130)
    expect(page.get_by_role("progressbar", name="Chuẩn bị nhận dạng người nói")).to_be_visible(
        timeout=8_000
    )
    assert page.locator("#task-progress-bar").evaluate(
        "el => [el.hasAttribute('value'), el.matches(':indeterminate')]"
    ) == [False, True]
    expect(percent).to_be_empty()
    window = time.monotonic() - started

    task_poll = f"/api/tasks/{task_id}"
    assert_only_existing_polls(
        requests, task_poll, f"/api/meetings/{meeting_id}/notes", window=window
    )
    assert ("GET", task_poll) in requests

    assert fail_task(
        live.settings.data_dir,
        task_id,
        owner_id=owner_id,
        error_code="diarization_failed",
        message="Stopped.",
        elapsed_seconds=5,
    )
    expect(page.locator("#task-progress")).to_be_hidden(timeout=8_000)


@pytest.mark.parametrize("duration", [None, 0])
def test_progress_is_indeterminate_without_a_recording_length(live, signed_in, duration):
    _context, page = signed_in()
    meeting_id, _task_id, _owner_id = running_transcription(live, end=25)
    without_duration(page, duration)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    bar = page.get_by_role("progressbar", name="Đang chép lời")
    expect(bar).to_be_visible()
    assert bar.evaluate(
        "el => [el.hasAttribute('value'), el.position, el.matches(':indeterminate')]"
    ) == [False, -1, True]
    expect(page.locator("#task-progress-percent")).to_be_empty()


def test_transcription_cancel_names_what_it_cancels(live, signed_in):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_transcription(live)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    cancel = page.locator("#cancel-transcription")
    expect(cancel).to_be_visible()
    expect(cancel).to_have_text("Hủy chép lời")
    expect(page.get_by_role("button", name="Hủy xử lý")).to_have_count(0)
    cancel.click()
    expect(cancel).to_have_text("Đang dừng…")
    expect(cancel).to_be_disabled()
    assert finish_canceled_task(
        live.settings.data_dir, task_id, owner_id=owner_id, elapsed_seconds=2
    )
    expect(page.locator("#transcription-status")).to_contain_text("Đã hủy", timeout=8_000)
    expect(page.locator("#task-progress")).to_be_hidden()


def test_rerun_is_a_secondary_button_once_a_transcript_exists(live, signed_in):
    _context, page = signed_in()
    transcribed_id, _generation, _segments = seed_completed_transcript(live.settings.data_dir)
    fresh_id = seed_meeting(live.settings.data_dir, "Not transcribed yet", "2026-10-18")
    page.goto(f"{live.url}/meetings/{transcribed_id}")

    rerun = page.get_by_role("button", name="Chạy lại bản ghi")
    expect(rerun).to_have_class(re.compile(r"\bsecondary-button\b"))
    expect(rerun).not_to_have_class(re.compile(r"\bprimary-button\b"))
    accent = rgb(theme_tokens(page)["--accent"])
    assert rerun.evaluate("el => getComputedStyle(el).backgroundColor") != accent
    expect(page.locator("#task-progress")).to_be_hidden()

    page.goto(f"{live.url}/meetings/{fresh_id}")
    first_run = page.get_by_role("button", name="Bắt đầu chép lời")
    expect(first_run).to_have_class(re.compile(r"\bprimary-button\b"))
    expect(first_run).not_to_have_class(re.compile(r"\bsecondary-button\b"))
    assert first_run.evaluate("el => getComputedStyle(el).backgroundColor") == accent


def test_rerun_language_is_retained_across_an_uncertain_retry(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    requests = []

    def start_task(route):
        body = route.request.post_data_json
        requests.append(body)
        if len(requests) == 1:
            route.abort("connectionfailed")
            return
        route.fulfill(
            status=202,
            content_type="application/json",
            json={
                "id": "a" * 32,
                "request_id": body["request_id"],
                "meeting_id": meeting_id,
                "kind": "transcription",
                "model_name": "model",
                "language_hint": body["language_hint"],
                "device": "cpu",
                "compute_type": "int8",
                "status": "accepted",
                "worker_state": "checking-worker",
                "stage": "accepted",
                "result_scope": None,
                "audio_duration_seconds": 30,
                "transcript_end_seconds": 0,
                "segment_count": 0,
                "elapsed_seconds": 0,
                "error_code": None,
                "error_message": None,
            },
        )

    page.route(f"{live.url}/api/tasks", start_task)
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    language = page.locator("#transcription-language")
    expect(language).to_have_value("en")
    expect(language).to_have_accessible_name("Ngôn ngữ gợi ý lần chạy")
    language.select_option("vi")
    # An unrelated meeting render must not replace the user's choice with the previous
    # generation's language before the rerun request is sent.
    page.evaluate(
        """async meetingId => {
          const { meetingHistory } = await import("/assets/history.js");
          meetingHistory.setNotesSourceLock(meetingId, true, true);
          meetingHistory.setNotesSourceLock(meetingId, false, true);
        }""",
        meeting_id,
    )
    expect(language).to_have_value("vi")
    expect(language).not_to_be_disabled()
    page.get_by_role("button", name="Chạy lại bản ghi").click()
    expect(page.locator("#transcription-error")).to_be_visible()
    expect(language).to_have_value("vi")
    expect(language).to_be_disabled()

    # A reload has no task row to identify the uncertain request, so the session record
    # must still carry the language that was sent with the original request.
    page.reload(wait_until="domcontentloaded")
    expect(language).to_have_value("vi")
    expect(language).to_be_disabled()
    page.get_by_role("button", name="Chạy lại bản ghi").click()
    expect(page.get_by_role("button", name="Hủy chép lời")).to_be_visible()
    assert len(requests) == 2
    assert requests[0]["language_hint"] == "vi"
    assert requests[1] == requests[0]


def test_runtime_details_sit_in_a_closed_disclosure(live, signed_in):
    _context, page = signed_in()
    meeting_id = seed_meeting(live.settings.data_dir, "Runtime details")
    lock_fd = acquire_worker_lock(live.settings.data_dir)
    assert lock_fd is not None
    write_runtime_profile(
        live.settings.data_dir,
        RuntimeProfile(
            device="cuda", compute_type="float16", model_path="/models/whisper-large-v3"
        ),
    )
    write_heartbeat(live.settings.data_dir, "idle")
    try:
        page.goto(f"{live.url}/meetings/{meeting_id}")
        details = page.locator("details.processing-details")
        expect(details).to_have_count(1)
        expect(details.locator("summary")).to_have_text("Chi tiết xử lý")
        expect(details.locator("#runtime-profile")).to_have_count(1)
        expect(details).not_to_have_attribute("open", "")
        expect(page.locator("#runtime-backend")).to_be_hidden()
        expect(page.get_by_text("Compute backend")).to_be_hidden()

        details.locator("summary").click()
        expect(details).to_have_attribute("open", "")
        expect(page.get_by_text("Compute backend")).to_be_visible()
        expect(page.get_by_text("Model mặc định theo phần cứng")).to_be_visible()
        expect(page.locator("#runtime-backend")).to_have_text("GPU · CUDA · float16")
        expect(page.locator("#runtime-model")).to_have_text("whisper-large-v3")
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("stage", ["transcribing", "starting_diarization"])
def test_progress_fits_360px_in_both_themes_and_the_summary_shows_focus(
    live, signed_in, scheme, stage
):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 360, "height": 800})
    meeting_id, _task_id, _owner_id = running_transcription(live, stage=stage, end=42)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    expect(page.locator("#task-progress")).to_be_visible()
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    assert overflow(page) == 0
    box = page.locator("#task-progress").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 360
    tokens = theme_tokens(page)
    bar = page.locator("#task-progress-bar")
    assert bar.evaluate("el => getComputedStyle(el).color") == rgb(tokens["--accent"])
    if stage == "transcribing":
        expect(bar).to_have_attribute("value", "0.42")
        assert bar.evaluate("el => getComputedStyle(el).backgroundColor") == rgb(
            tokens["--hairline"]
        )
    else:
        assert bar.get_attribute("value") is None
        assert bar.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
        assert bar.evaluate("el => getComputedStyle(el).borderTopStyle") == "dashed"
    ratios = page.evaluate(
        CONTRAST_JS, "#task-progress-stage, #task-progress-percent, .processing-details > summary"
    )
    assert len(ratios) == 3 and min(ratios) >= 4.5

    page.locator("#cancel-transcription").focus()
    page.keyboard.press("Tab")
    summary = page.locator(".processing-details > summary")
    expect(summary).to_be_focused()
    ring = summary.evaluate(
        """el => {
          const style = getComputedStyle(el);
          return [el.matches(":focus-visible"), style.outlineStyle, style.outlineWidth,
                  style.outlineColor];
        }"""
    )
    assert ring == [True, "solid", "2px", rgb(tokens["--accent"])]
    page.keyboard.press("Enter")
    expect(page.locator("details.processing-details")).to_have_attribute("open", "")
    assert overflow(page) == 0


@pytest.mark.parametrize(
    ("preference", "animation"), [("no-preference", "task-progress-pulse"), ("reduce", "none")]
)
def test_indeterminate_pulse_stops_under_reduced_motion(live, signed_in, preference, animation):
    _context, page = signed_in(reduced_motion=preference)
    meeting_id, _task_id, _owner_id = running_transcription(live)
    without_duration(page, None)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    bar = page.locator("#task-progress-bar")
    expect(bar).to_be_visible()
    assert bar.evaluate("el => getComputedStyle(el).animationName") == animation


def test_progress_bar_is_drawn_in_forced_colors(live, signed_in):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_transcription(live, end=0)
    page.emulate_media(forced_colors="active")
    page.goto(f"{live.url}/meetings/{meeting_id}")

    # The palette is the browser's own, so compare paint: an empty track against no bar at all,
    # then the fill's edge moving with the value (a fill painted in Canvas never shows an edge).
    bar = page.locator("#task-progress-bar")
    percent = page.locator("#task-progress-percent")
    expect(percent).to_have_text("0%")
    track = bar.screenshot()
    bar.evaluate("el => { el.style.opacity = '0'; }")
    assert bar.screenshot() != track
    bar.evaluate("el => { el.style.opacity = ''; }")

    shots = []
    for end in (25, 50):
        report(live, task_id, owner_id, "transcribing", end)
        expect(percent).to_have_text(f"{end}%", timeout=8_000)
        shots.append(bar.screenshot())
    assert shots[0] != shots[1]


@pytest.mark.parametrize(
    ("stage", "states"),
    [
        ("starting", ("done", "current", "upcoming", "upcoming")),
        ("starting_notes", ("done", "current", "upcoming", "upcoming")),
        ("starting_provider", ("done", "current", "upcoming", "upcoming")),
        ("preparing_source", ("done", "current", "upcoming", "upcoming")),
        ("generating_notes", ("done", "done", "current", "upcoming")),
        ("generating", ("done", "done", "current", "upcoming")),
        ("chunking", ("done", "done", "current", "upcoming")),
        ("merging", ("done", "done", "current", "upcoming")),
        ("repairing", ("done", "done", "current", "upcoming")),
        ("rendering", ("done", "done", "current", "upcoming")),
        ("publishing", ("done", "done", "done", "current")),
        ("saving", ("done", "done", "done", "current")),
    ],
)
def test_notes_steps_map_each_backend_stage(live, signed_in, stage, states):
    _context, page = signed_in()
    meeting_id, _task_id, _owner_id = running_notes(live, stage=stage)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    expect_steps(page, *states)
    expect(page.locator("#notes-task-details")).to_be_visible()


def test_notes_steps_follow_a_live_run_with_elapsed_time_and_model(live, signed_in):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    task = accept(data_dir, meeting_id, generation_id, request_id="progress-live-run")
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect_steps(page, "current", "upcoming", "upcoming", "upcoming")
    expect(page.locator("#notes-task-elapsed")).to_have_text("Đã chạy 0.0 giây")

    requests = record_requests(page)
    started = time.monotonic()
    owner_id = claim(data_dir, task["id"])
    expect_steps(page, "done", "current", "upcoming", "upcoming")
    report(live, task["id"], owner_id, "generating_notes", 0, 12.34)
    expect_steps(page, "done", "done", "current", "upcoming")
    expect(page.locator("#notes-task-elapsed")).to_have_text("Đã chạy 12.3 giây")
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        "ChatGPT · gpt-6.1-sol · effort high"
    )
    report(live, task["id"], owner_id, "publishing", 0, 15)
    expect_steps(page, "done", "done", "done", "current")
    notes_poll = f"/api/meetings/{meeting_id}/notes"
    assert_only_existing_polls(requests, notes_poll, window=time.monotonic() - started)
    assert ("GET", notes_poll) in requests

    revision = publish_notes_revision(
        data_dir, task["id"], owner_id=owner_id, markdown="# Notes\n\nSaved.\n", elapsed_seconds=16
    )
    assert revision is not None
    expect_steps(page, "done", "done", "done", "done")
    expect(page.locator("#notes-step-end")).to_be_hidden()


@pytest.mark.parametrize("ending", ["failed", "canceled", "interrupted"])
def test_notes_step_that_ended_the_run_is_marked(live, signed_in, ending):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_notes(live)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect_steps(page, "done", "done", "current", "upcoming")

    end_notes_run(live, task_id, owner_id, ending)
    # The backend now reports the ending as the stage; the page kept the step it last saw.
    expect_steps(page, "done", "done", ending, "upcoming")
    expect(page.locator("#notes-step-end")).to_be_hidden()
    expect(page.locator("#notes-cancel")).to_be_hidden()


@pytest.mark.parametrize("ending", ["failed", "canceled", "interrupted"])
def test_notes_run_first_seen_after_it_ended_marks_no_step_done(live, signed_in, ending):
    _context, page = signed_in()
    meeting_id, task_id, owner_id = running_notes(live)
    end_notes_run(live, task_id, owner_id, ending)
    # A run that failed leaves no saved notes, so the meeting would open on its transcript.
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")

    end = page.locator("#notes-step-end")
    expect(end).to_be_visible()
    expect(end).to_have_text(STATE_TEXT[ending])
    expect(end).to_have_attribute("data-step-state", ending)
    steps = page.locator("#notes-steps > li:not(#notes-step-end)")
    expect(steps).to_have_count(4)
    for index, name in enumerate(STEP_NAMES):
        expect(steps.nth(index)).to_contain_text(name)
        expect(steps.nth(index)).to_have_attribute("data-step-state", "unknown")
        expect(steps.nth(index).locator(".notes-step-state")).to_be_empty()


def test_notes_cancel_names_what_it_cancels(live, signed_in):
    _context, page = signed_in()
    meeting_id, _task_id, _owner_id = running_notes(live)
    page.goto(f"{live.url}/meetings/{meeting_id}")

    cancel = page.locator("#notes-cancel")
    # The button ships hidden with static text; only the first render shows what the script sets.
    expect(cancel).to_be_visible()
    expect(cancel).to_have_text("Hủy tạo notes")
    expect(page.get_by_role("button", name="Hủy xử lý")).to_have_count(0)
    cancel.click()
    expect(cancel).to_have_text("Đang dừng…")
    expect(cancel).to_be_disabled()
    # Stopping replaces the stage on the server; the step list still shows where the run was.
    expect_steps(page, "done", "done", "current", "upcoming")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_notes_steps_fit_360px_in_both_themes(live, signed_in, scheme):
    _context, page = signed_in(color_scheme=scheme, viewport={"width": 360, "height": 800})
    meeting_id, task_id, owner_id = running_notes(live, provider="antigravity")
    page.goto(f"{live.url}/meetings/{meeting_id}")

    expect_steps(page, "done", "done", "current", "upcoming")
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    assert overflow(page) == 0
    box = page.locator("#notes-task-details").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 360
    tokens = theme_tokens(page)

    def mark(state):
        return page.locator(f'#notes-steps > li[data-step-state="{state}"]').first.evaluate(
            "el => getComputedStyle(el, '::before').backgroundColor"
        )

    assert mark("done") == rgb(tokens["--success"])
    assert mark("current") == rgb(tokens["--accent"])
    selector = "#notes-steps > li, .notes-step-state, .notes-task-meta > span"
    assert min(page.evaluate(CONTRAST_JS, selector)) >= 4.5

    end_notes_run(live, task_id, owner_id, "failed")
    expect_steps(page, "done", "done", "failed", "upcoming")
    assert mark("failed") == rgb(tokens["--danger"])
    failed = page.locator('#notes-steps > li[data-step-state="failed"]')
    assert failed.evaluate("el => getComputedStyle(el).color") == rgb(tokens["--danger"])
    assert min(page.evaluate(CONTRAST_JS, selector)) >= 4.5
    assert overflow(page) == 0


def test_expired_workspace_session_during_notes_poll_returns_to_access(live, signed_in):
    _context, page = signed_in()
    meeting_id, _task_id, _owner_id = running_notes(live)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-task-details")).to_be_visible()
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail": "Authentication required"}',
        ),
    )
    page.wait_for_url(f"{live.url}/access", timeout=8_000)
