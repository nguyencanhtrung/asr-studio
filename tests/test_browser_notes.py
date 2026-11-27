from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import wave
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from notes_helpers import StubNotesProvider, rerun_transcript, seed_completed_transcript
from playwright.sync_api import expect
from seeding import prepare_recording
from test_browser_history import live as live
from test_browser_history import open_ready_rerun
from test_browser_history import ready_rerun_worker as ready_rerun_worker
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes_provider import ANTIGRAVITY_MODEL, mock_providers
from test_browser_notes_provider import fake_notes_provider as fake_vendor_notes_provider  # noqa: F401

from asr_studio import notes_routes
from asr_studio.media import normalize_media
from asr_studio.notes import (
    accept_notes_task,
    get_note,
    get_notes_source,
    publish_notes_revision,
    update_note,
)
from asr_studio.notes_document import render_markdown
from asr_studio.notes_history import install_notes_history
from asr_studio.notes_lifecycle import empty_trash, list_trash, trash_version
from asr_studio.storage import database_path
from asr_studio.tasks import claim_next_task, finish_canceled_task, transcription_state

pytestmark = pytest.mark.browser


class BrowserExplicitStub(StubNotesProvider):
    @contextmanager
    def notes_task_pin_for_run(self, *, model: str, reasoning_effort: str):
        with self.store.operation():
            yield {
                "provider": "chatgpt",
                "connection_version": 7,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }


@pytest.fixture
def signed_in(browser_signed_in, live):  # noqa: F811
    """Signed-in pages with a saved model on a connected ChatGPT, which a new Notes run requires."""

    def make(*args, **kwargs):
        context, page = browser_signed_in(*args, **kwargs)
        # The preference API checks the model statically, without a network call.
        status = page.evaluate(
            """async () => (await fetch("/api/notes-preferences", {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                provider: "chatgpt", model: "gpt-6.1-sol", reasoning_effort: "high",
              }),
            })).status"""
        )
        assert status == 200
        # A provider that is not connected turns the action into "Kết nối …", and none is in a test.
        mock_providers(page, live)
        return context, page

    return make


def test_notes_recovers_when_transcription_submission_finishes_after_navigation(
    live, signed_in, ready_rerun_worker
):
    _context, page = signed_in()
    first_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    other_id, _other_generation, _other_segments = seed_completed_transcript(
        live.settings.data_dir, title="Other meeting"
    )
    prepare_recording(live.settings.data_dir, first_id)
    held_requests = []
    page.route(f"{live.url}/api/tasks", lambda route: held_requests.append(route))
    page.goto(f"{live.url}/meetings/{first_id}")
    expect(page.locator("#notes-start")).to_be_enabled()
    dialog = open_ready_rerun(page)
    assert held_requests == []
    dialog.get_by_role("button", name="Chạy lại", exact=True).click()
    expect(page.locator("#notes-start")).to_be_disabled()
    page.locator(f'a[href="/meetings/{other_id}"]').first.click()
    expect(page.locator("#meeting-title")).to_have_text("Other meeting")
    expect(page.locator("#notes-start")).to_be_disabled()
    assert len(held_requests) == 1
    assert held_requests[0].request.post_data_json["expected_generation_id"] == generation_id
    held_requests.pop().fulfill(
        status=409, json={"detail": {"code": "system_busy", "message": "Processing is busy."}}
    )
    expect(page.locator("#notes-start")).to_be_enabled()


@pytest.mark.parametrize("cached", [True, False])
def test_notes_waits_for_transcription_request_and_active_task(live, signed_in, cached, request):
    _context, page = signed_in()
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    if cached:
        request.getfixturevalue("ready_rerun_worker")
        prepare_recording(live.settings.data_dir, meeting_id)
    else:
        with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
            connection.execute(
                "UPDATE meetings SET current_generation_id = NULL WHERE id = ?", (meeting_id,)
            )
    task = transcription_state(live.settings.data_dir, meeting_id)["task"]
    task.update(id="new-transcription-task", status="accepted", stage="queued", worker_state="idle")
    held_requests = []
    notes_requests = []
    started = False

    def meeting_response(route):
        payload = route.fetch().json()
        meeting = payload.get("meeting", payload)
        if started:
            meeting["transcription"]["task"] = task
        route.fulfill(json=payload)

    page.route(f"{live.url}/api/meetings/{meeting_id}", meeting_response)
    page.route(f"{live.url}/api/tasks", lambda route: held_requests.append(route))
    page.route(f"{live.url}/api/tasks/{task['id']}", lambda route: route.fulfill(json=task))
    page.on(
        "request",
        lambda request: (
            notes_requests.append(request.url)
            if request.method == "POST" and request.url.endswith("/notes-runs")
            else None
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    if cached:
        expect(page.locator("#notes-start")).to_be_enabled()
        dialog = open_ready_rerun(page)
        assert held_requests == []
        dialog.get_by_role("button", name="Chạy lại", exact=True).click()
    else:
        expect(page.locator("#notes-status")).to_contain_text("Cần transcript hoàn chỉnh")
        page.locator("#start-transcription").click()
    expect(page.locator("#notes-start")).to_be_disabled()
    expect(page.locator("#notes-status")).to_contain_text("Chờ tác vụ kết thúc")
    assert len(held_requests) == 1
    body = held_requests[0].request.post_data_json
    if cached:
        assert body["expected_generation_id"] == generation_id
        assert body["diarization_mode"] == "community-1"
    else:
        assert set(body) == {"meeting_id", "request_id", "language_hint", "asr_model"}
    started = True
    held_requests.pop().fulfill(status=202, json=task)
    expect(page.locator("#cancel-transcription")).to_be_visible()
    expect(page.locator("#notes-start")).to_be_disabled()
    task.update(status="running", stage="transcribing")
    expect(page.locator("#transcription-status")).to_contain_text("Đang chép lời")
    expect(page.locator("#notes-start")).to_be_disabled()
    page.reload()
    expect(page.locator("#notes-status")).to_contain_text("Chờ tác vụ kết thúc")
    expect(page.locator("#notes-start")).to_be_disabled()
    task.update(status="stopping", stage="stopping")
    expect(page.locator("#cancel-transcription")).to_be_disabled()
    expect(page.locator("#notes-start")).to_be_disabled()
    task.update(status="canceled", stage="canceled")
    expect(page.locator("#start-transcription")).to_be_visible()
    if cached:
        expect(page.locator("#notes-start")).to_be_enabled()
    else:
        expect(page.locator("#notes-start")).to_be_disabled()
        expect(page.locator("#notes-status")).to_contain_text("Cần transcript hoàn chỉnh")
    assert notes_requests == []


@pytest.mark.parametrize("width", [1440, 390])
def test_notes_action_precedes_long_transcript(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        connection.execute(
            "UPDATE transcript_segments SET text = ? WHERE generation_id = ?",
            ("A long meeting discussion. " * 500, generation_id),
        )
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator("#transcript-segments > li")).to_have_count(2)
    # The long transcript is in the other tab, so it no longer pushes the action down the page.
    page.locator("#meeting-tab-transcript").click()
    transcript = page.locator("#transcription-panel").bounding_box()
    assert transcript is not None and transcript["height"] > page.viewport_size["height"]
    expect(page.locator("#notes-start")).to_be_hidden()
    page.locator("#meeting-tab-notes").click()
    expect(page.locator("#notes-start")).to_be_visible()
    expect(page.locator("#transcription-panel")).to_be_hidden()
    action = page.locator("#notes-start").bounding_box()
    assert action is not None
    assert action["y"] + action["height"] < transcript["height"]
    assert page.evaluate("document.documentElement.scrollHeight") < transcript["height"]


def test_output_language_options_use_short_labels(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    page.goto(f"{live.url}/meetings/{meeting_id}")
    options = page.locator("#notes-output-language option")
    expect(options).to_have_text(["English", "Tiếng Việt"])
    assert options.evaluate_all("items => items.map(item => item.value)") == ["en", "vi"]
    expect(page.locator("#notes-output-language")).to_have_value("en")


def test_language_copy_says_notes_follow_the_chosen_output_language(live, signed_in):
    _context, page = signed_in()
    page.goto(f"{live.url}/", wait_until="domcontentloaded")
    for place in (".notes-principle", ".meeting-language-reminder", ".dialog-promise"):
        expect(page.locator(place)).to_contain_text("ngôn ngữ đầu ra đã chọn")


def test_notes_late_asset_still_receives_current_meeting_context(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    held_assets = []
    page.route(f"{live.url}/assets/notes.js", lambda route: held_assets.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="commit")
    expect(page.locator("#meeting-title")).to_have_text("Planning meeting")
    assert len(held_assets) == 1
    held_assets.pop().continue_()
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator(".segment-edit-button").first).to_be_enabled()


@pytest.fixture
def fake_notes_provider(monkeypatch):
    original_accept = notes_routes.accept_notes_task

    def accept_with_stub(data_dir, *, provider, **kwargs):
        return original_accept(data_dir, provider=BrowserExplicitStub(), **kwargs)

    monkeypatch.setattr(notes_routes, "accept_notes_task", accept_with_stub)
    monkeypatch.setattr(notes_routes, "worker_is_healthy", lambda _path, *, role=None: True)


def _markdown(snapshot: dict[str, object]) -> str:
    segment_ids = [segment["id"] for segment in snapshot["segments"]]
    document = {
        "schema_version": 1,
        "overview": [
            {"text": "The team reviewed its launch plan.", "source_ids": [segment_ids[0]]}
        ],
        "key_topics": [
            {"text": "The team needs a short written draft.", "source_ids": [segment_ids[0]]}
        ],
        "decisions": [{"text": "Prepare the launch plan.", "source_ids": [segment_ids[0]]}],
        "open_questions": [{"text": "Who will review the draft?", "source_ids": [segment_ids[1]]}],
        "action_items": [
            {
                "text": "Send the draft.",
                "source_ids": [segment_ids[1]],
                "owner": "Morgan",
                "deadline_text": "tomorrow",
                "deadline_date": None,
            }
        ],
        "confirmation_items": [
            {"text": "Confirm the review date.", "source_ids": [segment_ids[1]]}
        ],
    }
    return render_markdown(document, snapshot)


def _accept(
    data_dir: Path,
    meeting_id: str,
    generation_id: str,
    *,
    request_id: str,
    expected_source_version: int = 1,
    output_language: str = "en",
):
    return accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=expected_source_version,
        expected_metadata_version=1,
        worker_ready=True,
        output_language=output_language,
    )


def _complete_fake_task(
    data_dir: Path, task_id: str, request_id: str, markdown: str, *, citations=None
):
    owner_id = f"notes-browser-worker-{request_id}"
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed is not None and claimed["id"] == task_id
    revision = publish_notes_revision(
        data_dir,
        task_id,
        owner_id=owner_id,
        markdown=markdown,
        elapsed_seconds=0.1,
        citations=citations,
    )
    assert revision is not None
    return revision


def _task_source_id(data_dir: Path, task_id: str) -> str:
    with sqlite3.connect(database_path(data_dir)) as connection:
        row = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row is not None and row[0]
    return row[0]


def _write_test_recording(data_dir: Path, meeting_id: str, tmp_path: Path) -> Path:
    with sqlite3.connect(database_path(data_dir)) as connection:
        row = connection.execute(
            "SELECT storage_key FROM recordings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
    assert row is not None
    audio_path = data_dir / row[0]
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    wave_path = tmp_path / "notes-source.wav"
    with wave.open(str(wave_path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 16_000 * 30)
    normalize_media(wave_path, audio_path, max_duration_seconds=60)
    return audio_path


def _browser_diagnostics(page):
    console_errors: list[str] = []
    page_errors: list[str] = []
    requests: list[str] = []
    http_errors: list[dict[str, object]] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))

    def capture_http_error(response) -> None:
        if response.status >= 400:
            http_errors.append({"status": response.status, "url": response.url})

    page.on("response", capture_http_error)
    return console_errors, page_errors, requests, http_errors


def _assert_no_token_urls(urls: list[str]) -> None:
    for raw_url in urls:
        query = parse_qs(urlsplit(raw_url).query)
        assert not {key.casefold() for key in query} & {"token", "access_token"}, raw_url


def test_active_vietnamese_task_overrides_stale_future_language_preference(live, signed_in):
    _context, page = signed_in()
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(
        live.settings.data_dir,
        title="Active Vietnamese notes language",
    )
    active = _accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-active-vietnamese-language",
        output_language="vi",
    )
    assert active["status"] == "accepted"
    assert active["output_language"] == "vi"
    preference_key = f"asr-studio:notes-output-language:{meeting_id}"
    page.evaluate("([key, value]) => localStorage.setItem(key, value)", [preference_key, "en"])

    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")

    language_select = page.locator("#notes-output-language")
    expect(language_select).to_have_value("vi", timeout=10_000)
    expect(language_select).to_be_disabled()
    assert page.evaluate("key => localStorage.getItem(key)", preference_key) == "en"
    page.context.close()


def test_notes_language_pending_request_survives_reload_and_saved_language_is_revision_pinned(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    page.add_init_script(
        "Object.defineProperty(window, 'localStorage', { configurable: true, "
        "get() { throw new Error('local storage unavailable'); } });"
    )
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(
        live.settings.data_dir,
        title="Output language browser check",
    )
    held_requests = []
    request_bodies = []

    def notes_request(route):
        body = json.loads(route.request.post_data or "{}")
        request_bodies.append(body)
        if len(request_bodies) == 1:
            held_requests.append(route)
        else:
            route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-output-language")).to_have_value("en")
    page.locator("#notes-change").click()
    page.locator("#notes-output-language").select_option("vi")
    expect(page.locator("#notes-output-language")).to_have_value("vi")
    page.locator("#notes-start").click()
    expect(page.locator("#notes-output-language")).to_be_disabled()
    assert len(held_requests) == 1
    first_body = request_bodies[0]
    assert first_body["output_language"] == "vi"
    assert first_body["expected_generation_id"] == generation_id
    assert first_body["expected_source_version"] == 1
    assert first_body["expected_metadata_version"] == 1
    held_requests.pop().fulfill(
        status=503,
        json={"detail": {"code": "temporary_failure", "message": "Try again."}},
    )
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    expect(page.locator("#notes-output-language")).to_be_disabled()

    page.reload()
    expect(page.locator("#notes-output-language")).to_have_value("vi")
    expect(page.locator("#notes-output-language")).to_be_disabled()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted_response:
        page.locator("#notes-start").click()
    assert accepted_response.value.status == 202
    accepted = accepted_response.value.json()
    second_body = request_bodies[1]
    assert second_body == first_body
    assert accepted["output_language"] == "vi"
    expect(page.locator("#notes-output-language")).to_be_disabled()

    _complete_fake_task(
        live.settings.data_dir,
        accepted["id"],
        "language-browser",
        "## Tổng quan\n\nBản notes tiếng Việt.\n",
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    expect(page.locator("#notes-revision-meta")).to_contain_text(
        "Ngôn ngữ: Tiếng Việt", timeout=10_000
    )
    expect(page.locator("#notes-output-language")).to_be_enabled()
    page.locator("#notes-change").click()  # a reload closed it
    page.locator("#notes-output-language").select_option("en")
    expect(page.locator("#notes-revision-meta")).to_contain_text("Ngôn ngữ: Tiếng Việt")
    expect(page.locator("#notes-revision-meta")).not_to_contain_text("Ngôn ngữ: English")


def test_notes_browser_generation_revisions_conflicts_copy_export_and_markdown_safety(
    live, signed_in, fake_notes_provider, tmp_path
):
    context, page = signed_in(
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
    )
    console_errors, page_errors, requests, http_errors = _browser_diagnostics(page)
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(
        live.settings.data_dir,
        title="Browser notes acceptance",
        meeting_date="2026-10-19",
    )
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)

    page.goto(f"{live.url}/meetings/{meeting_id}#notes", wait_until="domcontentloaded")
    expect(page.locator("#meeting-title")).to_have_text("Browser notes acceptance")
    expect(page.locator("#notes-start")).to_be_enabled()

    note_requests: list[dict[str, object]] = []

    def capture_request(request) -> None:
        if request.method == "POST" and request.url.endswith("/notes-runs"):
            note_requests.append(json.loads(request.post_data or "{}"))

    page.on("request", capture_request)
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted_response:
        page.locator("#notes-start").click()
    assert accepted_response.value.status == 202
    accepted = accepted_response.value.json()
    assert accepted["kind"] == "notes" and accepted["status"] == "accepted"
    assert note_requests == [
        {
            "request_id": note_requests[0]["request_id"],
            "expected_generation_id": generation_id,
            "expected_source_version": 1,
            "expected_metadata_version": 1,
            "output_language": "en",
            "provider": "chatgpt",
            "expected_model": "gpt-6.1-sol",
            "expected_reasoning_effort": "high",
        }
    ]
    assert note_requests[0]["request_id"]

    source_id = _task_source_id(live.settings.data_dir, accepted["id"])
    snapshot = get_notes_source(live.settings.data_dir, source_id)
    first_markdown = _markdown(snapshot)
    first_revision = _complete_fake_task(
        live.settings.data_dir,
        accepted["id"],
        "initial",
        first_markdown,
    )
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-revision-meta")).to_contain_text("Ngôn ngữ: English")
    expect(page.locator("#notes-steps > li:not([hidden])")).to_have_count(4)
    expect(page.locator('#notes-steps > li[data-step-state="done"]')).to_have_count(4)
    expect(page.locator("#status-transcript")).to_have_text("Transcript · Hoàn tất")
    expect(page.locator("#status-notes")).to_have_text("Notes · 1 bản")
    assert page.locator("#notes-preview h4").all_text_contents() == [
        "Overview",
        "Key Topics and Rationale",
        "Decisions",
        "Open Questions",
        "Confirmed Action Items",
        "Items Requiring Confirmation",
    ]
    expect(page.locator("#notes-preview")).to_contain_text("Prepare the launch plan.")
    expect(page.locator("#notes-preview")).to_contain_text("Send the draft.")
    expect(page.locator("#notes-preview a[data-notes-source-link]")).to_have_count(0)
    assert "token=" not in page.url and "access_token=" not in page.url

    screenshots = Path("/tmp/asr-issue9/screenshots")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.fonts.ready")
    desktop_overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    desktop_capture = screenshots / "issue9-notes-desktop.png"
    page.screenshot(path=str(desktop_capture), full_page=True, animations="disabled")

    page.set_viewport_size({"width": 390, "height": 844})
    narrow_overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    narrow_capture = screenshots / "issue9-notes-narrow.png"
    page.screenshot(path=str(narrow_capture), full_page=True, animations="disabled")
    assert desktop_capture.stat().st_size > 10_000
    assert narrow_capture.stat().st_size > 10_000
    assert desktop_overflow == 0
    assert narrow_overflow == 0

    draft = "# Local draft\n\nMy unsaved change is still here.\n"
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(draft)
    second_task = _accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-notes-revision-002",
    )
    second_source = get_notes_source(
        live.settings.data_dir,
        _task_source_id(live.settings.data_dir, second_task["id"]),
    )
    second_markdown = "# Newer revision\n\nThis is a second saved revision.\n"
    second_revision = _complete_fake_task(
        live.settings.data_dir,
        second_task["id"],
        "second",
        second_markdown,
    )
    assert second_revision["source_snapshot_id"] == second_source["id"]
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    expect(page.locator("#notes-revision-selector option")).to_have_count(2, timeout=10_000)
    expect(page.locator("#notes-revision-selector")).to_have_value(first_revision["id"])
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    expect(page.locator("#notes-draft-status")).to_contain_text("chưa lưu")

    remote_markdown = "# Another editor\n\nSaved before this draft.\n"
    update_note(
        live.settings.data_dir,
        first_revision["id"],
        expected_edit_version=1,
        markdown=remote_markdown,
    )
    with page.expect_response(lambda response: response.status == 409) as conflict_response:
        page.locator("#notes-save").click()
    expect(page.locator("#notes-conflict")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    expect(page.locator("#notes-conflict-message")).to_contain_text("Bản nháp của bạn vẫn được giữ")
    page.locator("#notes-keep-draft").click()
    expect(page.locator("#notes-conflict")).to_be_hidden(timeout=10_000)
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    expect(page.locator("#notes-revision-meta")).to_contain_text("Edit 3")
    assert get_note(live.settings.data_dir, first_revision["id"])["markdown"] == draft

    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.locator("#notes-copy").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("edit 3", timeout=10_000)
    assert page.evaluate("navigator.clipboard.readText()") == draft
    with page.expect_download() as download_info:
        page.locator("#notes-export").click()
    exported = download_info.value
    assert Path(exported.path()).read_text(encoding="utf-8") == draft

    hostile_markdown = (
        "# Security check\n\n"
        "<script>window.__notesXss = true</script>\n\n"
        "<img src=x onerror=window.__notesXss = true>\n\n"
        "[foreign](javascript:alert%281%29)\n"
    )
    update_note(
        live.settings.data_dir,
        first_revision["id"],
        expected_edit_version=3,
        markdown=hostile_markdown,
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    expect(page.locator("#notes-markdown")).to_have_value(hostile_markdown, timeout=10_000)
    page.locator("#notes-mode-view").click()
    assert page.locator("#notes-preview script, #notes-preview img").count() == 0
    assert page.locator('#notes-preview a[href^="https://"]').count() == 0
    expect(page.locator("#notes-preview")).to_contain_text(
        "<script>window.__notesXss = true</script>"
    )
    assert page.evaluate("window.__notesXss === undefined")
    _assert_no_token_urls(requests)
    assert not any("attacker.invalid" in url for url in requests)
    diagnostics = {
        "console_errors": console_errors,
        "page_errors": page_errors,
        "http_errors": http_errors,
        "desktop_overflow_px": desktop_overflow,
        "narrow_overflow_px": narrow_overflow,
        "screenshots": [str(desktop_capture), str(narrow_capture)],
    }
    Path("/tmp/asr-issue9/notes-browser-observations.json").write_text(
        json.dumps(diagnostics, indent=2), encoding="utf-8"
    )
    assert http_errors == [{"status": 409, "url": conflict_response.value.url}]
    assert console_errors == [
        "Failed to load resource: the server responded with a status of 409 (Conflict)"
    ]
    assert not page_errors
    context.close()


def test_saved_source_stays_frozen_and_seeks_same_audio_after_live_edit(
    live, signed_in, fake_notes_provider, tmp_path
):
    context, page = signed_in(viewport={"width": 1280, "height": 900})
    console_errors, page_errors, requests, http_errors = _browser_diagnostics(page)
    meeting_id, generation_id, segment_ids = seed_completed_transcript(
        live.settings.data_dir,
        title="Frozen source browser check",
        meeting_date="2026-10-19",
    )
    audio_path = _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    audio_digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()

    page.goto(f"{live.url}/meetings/{meeting_id}#notes", wait_until="domcontentloaded")
    expect(page.locator("#notes-start")).to_be_enabled()
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted_response:
        page.locator("#notes-start").click()
    assert accepted_response.value.status == 202
    accepted = accepted_response.value.json()
    source_id = _task_source_id(live.settings.data_dir, accepted["id"])
    snapshot = get_notes_source(live.settings.data_dir, source_id)
    original_text = snapshot["segments"][0]["text"]
    markdown = _markdown(snapshot)
    _complete_fake_task(live.settings.data_dir, accepted["id"], "source", markdown)
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    audio = page.locator("#meeting-audio")
    page.locator("#meeting-tab-transcript").click()  # the player is in the transcript tab
    expect(audio).to_be_visible()
    assert audio.get_attribute("src") == f"/api/meetings/{meeting_id}/audio"
    expect(page.locator("#notes-preview a[data-notes-source-link]")).to_have_count(0)

    page.goto(f"{live.url}/meetings/{meeting_id}?notes_source={source_id}&segment={segment_ids[0]}")
    expect(page.locator("#notes-source-view")).to_be_visible()
    expect(page.locator("#notes-source-text")).to_have_text(original_text)
    expect(page.locator("#notes-source-time")).to_have_text("00:00:01")
    expect(page.locator("#notes-source-status")).to_contain_text("Đã tua bản ghi", timeout=10_000)
    assert audio.evaluate("element => Math.abs(element.currentTime - 1) < 0.1")
    assert urlsplit(page.url).path == f"/meetings/{meeting_id}"
    assert parse_qs(urlsplit(page.url).query) == {
        "notes_source": [source_id],
        "segment": [segment_ids[0]],
    }

    page.locator("#meeting-tab-transcript").click()
    first_segment = page.locator(".transcript-segment").nth(0)
    first_segment.locator(".segment-edit-button").click()
    editor = first_segment.locator(".segment-text-editor")
    editor.fill("The live transcript was corrected after notes generation.")
    first_segment.locator('button[type="submit"]').click()
    expect(first_segment.locator(".segment-text")).to_have_text(
        "The live transcript was corrected after notes generation.", timeout=10_000
    )
    expect(page.locator("#transcript-mutation-status")).to_have_text("Đã lưu.")
    expect(page.locator("#notes-source-text")).to_have_text(original_text)
    expect(audio).to_have_attribute("src", f"/api/meetings/{meeting_id}/audio")
    assert hashlib.sha256(audio_path.read_bytes()).hexdigest() == audio_digest
    page.locator("#notes-source-seek").click()
    expect(page.locator("#notes-source-status")).to_contain_text("Đã tua bản ghi", timeout=10_000)
    assert audio.evaluate("element => Math.abs(element.currentTime - 1) < 0.1")
    # The card and the player it moves share the transcript tab.
    expect(page.locator("#meeting-tab-transcript")).to_have_attribute("aria-selected", "true")
    _assert_no_token_urls(requests)
    assert not console_errors
    assert not http_errors
    assert not page_errors
    context.close()


def test_failed_notes_read_keeps_old_revision_draft_source_and_other_meeting_navigation(
    live, signed_in, fake_notes_provider
):
    context, page = signed_in(viewport={"width": 1280, "height": 900})
    meeting_id, generation_id, _segment_ids = seed_completed_transcript(
        live.settings.data_dir,
        title="Notes read failure target",
        meeting_date="2026-10-19",
    )
    other_id, other_generation_id, _ = seed_completed_transcript(
        live.settings.data_dir,
        title="Other meeting remains usable",
        meeting_date="2026-10-18",
    )
    first_task = _accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-notes-old-revision",
    )
    source = get_notes_source(
        live.settings.data_dir,
        _task_source_id(live.settings.data_dir, first_task["id"]),
    )
    original_text = source["segments"][0]["text"]
    revision = _complete_fake_task(
        live.settings.data_dir,
        first_task["id"],
        "old-revision",
        _markdown(source),
    )
    active = _accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-notes-active-task",
    )
    assert active["status"] == "accepted"

    page.goto(f"{live.url}/meetings/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator("#notes-revision-selector")).to_have_value(revision["id"], timeout=10_000)
    page.locator("#meeting-tab-transcript").click()  # the lock reason is shown with the transcript
    expect(
        page.get_by_text("Tác vụ notes đang dùng nguồn; chỉnh sửa tạm thời bị khóa.", exact=False)
    ).to_be_visible()
    expect(page.locator(".segment-edit-button").first).to_be_disabled()
    page.locator("#meeting-tab-notes").click()
    draft = "# Draft while another notes task is running\n\nKeep this across navigation.\n"
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(draft)

    page.locator("#recent-list a").filter(has_text="Other meeting remains usable").click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text("Other meeting remains usable")
    expect(page.locator("#notes-start")).to_be_enabled()

    def fail_notes_read(route) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "Seeded notes-read failure"}),
        )

    page.route(f"**/api/meetings/{meeting_id}/notes", fail_notes_read)
    page.locator("#recent-list a").filter(has_text="Notes read failure target").click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-error")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    expect(page.locator("#notes-save")).to_be_enabled()
    assert page.locator("#notes-source-view").is_hidden()

    page.evaluate(
        """({ meetingId, sourceId, segmentId }) => {
          const url = `/meetings/${meetingId}?notes_source=${sourceId}&segment=${segmentId}`;
          window.history.pushState({}, "", url);
          window.dispatchEvent(new PopStateEvent("popstate"));
        }""",
        {
            "meetingId": meeting_id,
            "sourceId": revision["source_snapshot_id"],
            "segmentId": source["segments"][0]["id"],
        },
    )
    expect(page.locator("#notes-source-view")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-source-text")).to_have_text(original_text)
    # The saved source opens in the transcript tab; the draft waits in the notes tab.
    expect(page.locator("#meeting-tab-transcript")).to_have_attribute("aria-selected", "true")
    page.locator("#meeting-tab-notes").click()
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == draft

    page.unroute(f"**/api/meetings/{meeting_id}/notes")
    page.locator("#recent-list a").filter(has_text="Other meeting remains usable").click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text("Other meeting remains usable")
    assert other_generation_id
    context.close()


def _seed_saved_notes(data_dir: Path, *, title: str = "Notes frame"):
    """Seed a meeting with one saved notes revision, as the worker publishes it."""
    meeting_id, generation_id, segment_ids = seed_completed_transcript(data_dir, title=title)
    suffix = meeting_id[:8]
    task = _accept(data_dir, meeting_id, generation_id, request_id=f"seed-notes-{suffix}")
    snapshot = get_notes_source(data_dir, _task_source_id(data_dir, task["id"]))
    revision = _complete_fake_task(data_dir, task["id"], f"seed-{suffix}", _markdown(snapshot))
    return meeting_id, revision, snapshot, segment_ids


def _open_saved_notes(live, page, meeting_id: str) -> None:
    page.goto(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    # Saved work can paint before the provider catalogs finish; the launcher is
    # enabled only after the choice surface is authoritative for this meeting.
    expect(page.locator("#notes-start")).to_be_enabled(timeout=10_000)


@pytest.mark.parametrize("width", [1440, 390])
def test_explicit_regenerate_dialog_uses_historical_source_without_saving_preferences(
    live, signed_in, fake_notes_provider, width
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, revision, snapshot, _segment_ids = _seed_saved_notes(live.settings.data_dir)
    historical_generation_id = snapshot["generation_id"]
    rerun_transcript(live.settings.data_dir, meeting_id)
    with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
        current_generation_id = connection.execute(
            "SELECT current_generation_id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()[0]
    assert current_generation_id != historical_generation_id

    requests: list[dict[str, object]] = []
    preference_writes: list[str] = []

    def capture_request(request) -> None:
        if request.method == "POST" and request.url.endswith("/notes-runs"):
            requests.append(json.loads(request.post_data or "{}"))
        if request.method == "PUT" and request.url.endswith("/api/notes-preferences"):
            preference_writes.append(request.url)

    page.on("request", capture_request)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-mode-write").click()
    draft = "# Keep this draft\n\nDo not replace these unsaved words.\n"
    page.locator("#notes-markdown").fill(draft)
    page.locator("#notes-start").click()

    dialog = page.locator("#notes-regenerate-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("aria-labelledby", "notes-regenerate-title")
    expect(dialog).to_have_attribute("aria-describedby", "notes-regenerate-intro")
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-regenerate-provider"
    )
    source = page.locator("#notes-regenerate-source")
    source_values = source.locator("option").evaluate_all("items => items.map(item => item.value)")
    assert source_values == [historical_generation_id, current_generation_id]
    expect(source).to_have_value(current_generation_id)
    page.locator("#notes-regenerate-provider").select_option("chatgpt")
    page.locator("#notes-regenerate-model").select_option("gpt-6.1-sol")
    page.locator("#notes-regenerate-effort").select_option("high")
    source.select_option(historical_generation_id)
    page.locator("#notes-regenerate-language").select_option("vi")
    dialog_box = dialog.bounding_box()
    assert dialog_box is not None and dialog_box["width"] <= width

    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted_response:
        page.locator("#notes-regenerate-submit").click()
    assert accepted_response.value.status == 202
    assert len(requests) == 1
    body = requests[0]
    assert set(body) == {
        "request_id",
        "expected_generation_id",
        "expected_source_version",
        "expected_metadata_version",
        "output_language",
        "run",
    }
    assert body["expected_generation_id"] == current_generation_id
    assert body["expected_source_version"] == 1
    assert body["expected_metadata_version"] == 1
    assert body["output_language"] == "vi"
    assert body["run"] == {
        "provider": "chatgpt",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "generation_id": historical_generation_id,
        "source_version": 1,
    }
    assert preference_writes == []
    expect(dialog).to_be_hidden()
    expect(page.locator("#notes-cancel")).to_be_enabled()
    expect(page.locator("#notes-cancel")).to_be_focused()
    expect(page.locator("#notes-markdown")).to_have_value(draft)
    assert revision["id"]


def test_explicit_regenerate_retry_replays_the_same_request_and_cancel_returns_focus(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, _revision, snapshot, _segment_ids = _seed_saved_notes(live.settings.data_dir)
    rerun_transcript(live.settings.data_dir, meeting_id)
    requests: list[str] = []
    waiting = []

    def reject(route) -> None:
        route.fulfill(
            status=503,
            json={"detail": {"code": "temporary_failure", "message": "Try again."}},
        )

    def notes_request(route) -> None:
        requests.append(route.request.post_data or "")
        if len(requests) <= 2:
            waiting.append(route)
        else:
            reject(route)

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    dialog = page.locator("#notes-regenerate-dialog")
    submit = page.locator("#notes-regenerate-submit")
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")):
        submit.click()
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("aria-busy", "true")
    expect(submit).to_be_disabled()
    reject(waiting.pop())
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    expect(page.locator("#notes-regenerate-error")).to_contain_text("Hãy thử lại")
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    assert len(requests) == 1
    first_body = requests[0]
    expect(submit).to_have_text("Xác minh yêu cầu notes")
    expect(submit).to_be_enabled()
    expect(page.locator("#notes-regenerate-model")).to_be_disabled()
    expect(dialog).to_have_attribute("aria-busy", "false")

    page.reload()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")):
        page.locator("#notes-start").click()
    transcript = page.locator("#meeting-tab-transcript")
    transcript.click()
    expect(transcript).to_be_focused()
    reject(waiting.pop())
    expect(page.locator("#notes-status")).to_contain_text("Yêu cầu chưa được tự gửi lại")
    expect(dialog).to_be_hidden()
    expect(transcript).to_be_focused()
    page.locator("#meeting-tab-notes").click()
    expect(page.locator("#meeting-tab-notes")).to_be_focused()
    assert requests == [first_body, first_body]

    page.locator("#notes-start").click()
    expect(dialog).to_be_visible()
    expect(submit).to_be_enabled()
    expect(dialog).to_have_attribute("aria-busy", "false")
    assert requests == [first_body, first_body, first_body]

    page.locator("#notes-regenerate-cancel").click()
    page.locator("#notes-start").click()
    expect(page.locator("#notes-regenerate-provider")).to_be_disabled()
    expect(page.locator("#notes-regenerate-submit")).to_be_focused()

    page.locator("#notes-regenerate-cancel").click()
    expect(page.locator("#notes-regenerate-dialog")).to_be_hidden()
    expect(page.locator("#notes-start")).to_be_focused()
    assert snapshot["generation_id"]


@pytest.mark.parametrize("delayed_read", ["notes", "meeting"])
def test_notes_admission_keeps_cancel_and_poll_after_a_delayed_read(
    live, signed_in, fake_notes_provider, delayed_read
):
    _context, page = signed_in()
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    meeting_url = f"{live.url}/api/meetings/{meeting_id}"
    notes_url = f"{meeting_url}/notes"
    delayed_url = meeting_url if delayed_read == "meeting" else notes_url
    idle = page.request.get(delayed_url).json()
    _open_saved_notes(live, page, meeting_id)
    held_reads = []
    page.route(notes_url, lambda route: held_reads.append(route))
    held_details = []
    if delayed_read == "meeting":
        page.route(meeting_url, lambda route: held_details.append(route))
    start = page.locator("#notes-start")
    cancel = page.locator("#notes-cancel")
    edit = page.locator(".segment-edit-button").first
    lock = page.locator("#notes-lock-status")
    expect(edit).to_be_enabled()
    start.click()
    with page.expect_request(delayed_url):
        page.evaluate(
            """async id => {
              const { meetingHistory } = await import('/assets/history.js');
              void meetingHistory.refreshMeeting(id);
            }""",
            meeting_id,
        )
    with page.expect_response(lambda response: response.url.endswith("/notes-runs")) as response:
        page.locator("#notes-regenerate-submit").click()
    assert response.value.status == 202
    expect(cancel).to_be_enabled()
    expect(lock).to_be_visible()
    expect(edit).to_be_disabled()

    # The GET began before admission but its old state arrives after the accepted task.
    delayed = (held_details if delayed_read == "meeting" else held_reads).pop(0)
    with page.expect_request(notes_url, timeout=5_000):
        delayed.fulfill(json=idle)
    expect(cancel).to_be_visible()
    expect(cancel).to_be_enabled()
    expect(start).to_be_disabled()
    expect(lock).to_be_visible()
    expect(edit).to_be_disabled()

    task = response.value.json()
    _complete_fake_task(live.settings.data_dir, task["id"], task["request_id"], _markdown(snapshot))
    held_reads[0].continue_()
    expect(cancel).to_be_hidden()
    expect(lock).to_be_hidden()
    expect(edit).to_be_enabled()
    expect(page.locator("#notes-revision-selector option")).to_have_count(2)
    expect(start).to_be_enabled()


def test_late_notes_admission_receipt_keeps_cancel_on_the_newer_active_task(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    meeting_id, prior_revision, snapshot, _segments = _seed_saved_notes(data_dir)
    meeting_url = f"{live.url}/api/meetings/{meeting_id}"
    runs_url = f"{meeting_url}/notes-runs"
    notes_url = f"{meeting_url}/notes"
    held_admissions = []
    _open_saved_notes(live, page, meeting_id)
    page.route(runs_url, lambda route: held_admissions.append(route))

    dialog = page.locator("#notes-regenerate-dialog")
    page.locator("#notes-start").click()
    with page.expect_request(runs_url, timeout=5_000):
        page.locator("#notes-regenerate-submit").click()
    expect(dialog).to_have_attribute("aria-busy", "true")
    admission = held_admissions.pop()
    receipt = admission.fetch(timeout=5_000)
    assert receipt.status == 202
    first_task = receipt.json()
    first_revision = _complete_fake_task(
        data_dir, first_task["id"], first_task["request_id"], _markdown(snapshot)
    )
    second_task = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id="newer-notes-run"
    )
    assert first_task["id"] != second_task["id"]

    canonical_reads = []
    held_reads = []

    def read_notes(route):
        if canonical_reads:
            held_reads.append(route)
            return
        response = route.fetch(timeout=5_000)
        canonical_reads.append(response.json())
        route.fulfill(response=response)

    # Deliver one authoritative Notes GET, then hold every later read across Cancel.
    page.route(notes_url, read_notes)
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    revisions = page.locator("#notes-revision-selector")
    expect(revisions.locator("option")).to_have_count(2)
    assert canonical_reads[0]["task"]["id"] == second_task["id"]
    assert canonical_reads[0]["source_locked"] is True
    lock = page.locator("#notes-lock-status")
    edit = page.locator(".segment-edit-button").first
    expect(lock).to_be_visible()
    expect(edit).to_be_disabled()

    admission.fulfill(response=receipt)
    expect(dialog).to_be_hidden()
    expect(lock).to_be_visible()
    expect(edit).to_be_disabled()
    assert revisions.locator("option").evaluate_all("options => options.map(o => o.value)") == [
        first_revision["id"],
        prior_revision["id"],
    ]
    expect(revisions).to_have_value(prior_revision["id"])

    cancel = page.locator("#notes-cancel")
    expect(cancel).to_be_enabled()
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/cancel"),
        timeout=5_000,
    ) as canceled:
        cancel.click()
    assert canceled.value.url == f"{live.url}/api/tasks/{second_task['id']}/cancel"


@pytest.mark.parametrize("second_state", ["accepted", "running"])
def test_current_notes_task_can_be_canceled_while_an_older_cancel_reply_is_pending(
    live, signed_in, second_state
):
    _context, page = signed_in()
    data_dir = live.settings.data_dir
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(data_dir)
    _open_saved_notes(live, page, meeting_id)
    cancel = page.locator("#notes-cancel")
    notes_url = f"{live.url}/api/meetings/{meeting_id}/notes"
    refresh = "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)"

    first = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id="cancel-first-notes"
    )
    first_url = f"{live.url}/api/tasks/{first['id']}/cancel"
    held_first = []
    page.route(first_url, lambda route: held_first.append(route))
    page.evaluate(refresh, meeting_id)
    expect(cancel).to_be_enabled()
    with page.expect_request(first_url, timeout=5_000):
        cancel.click()
    expect(cancel).to_have_text("Đang dừng…")
    first_route = held_first.pop()
    first_response = first_route.fetch(timeout=5_000)
    assert first_response.status == 200
    assert first_response.json()["status"] == "canceled"

    second = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id="cancel-next-notes"
    )
    owner = "cancel-browser-worker"
    if second_state == "running":
        assert claim_next_task(data_dir, owner_id=owner)["id"] == second["id"]
    second_url = f"{live.url}/api/tasks/{second['id']}/cancel"
    held_second = []
    page.route(second_url, lambda route: held_second.append(route))
    with page.expect_response(notes_url, timeout=5_000):
        page.evaluate(refresh, meeting_id)
    expect(cancel).to_have_text("Hủy")
    expect(cancel).to_be_enabled()
    if second_state == "running":
        old_read = page.request.get(notes_url).json()
        assert old_read["task"]["id"] == second["id"]
        assert old_read["task"]["status"] == "running"
        held_reads = []
        with page.expect_request(notes_url, timeout=5_000):
            page.route(notes_url, lambda route: held_reads.append(route))
    with page.expect_request(second_url, timeout=5_000):
        cancel.click()
    expect(cancel).to_have_text("Đang dừng…")
    expect(cancel).to_be_disabled()

    with page.expect_response(first_url, timeout=5_000) as old_response:
        first_route.fulfill(response=first_response)
    old_response.value.finished()
    expect(cancel).to_have_text("Đang dừng…")
    expect(page.locator("#notes-lock-status")).to_be_visible()
    with page.expect_response(second_url, timeout=5_000) as accepted:
        held_second.pop().continue_()
    assert accepted.value.json()["id"] == second["id"]
    if second_state == "running":
        assert accepted.value.json()["status"] == "stopping"
        with page.expect_request(notes_url, timeout=5_000):
            held_reads.pop(0).fulfill(json=old_read)
        expect(cancel).to_have_text("Đang dừng…")
        expect(cancel).to_be_disabled()
        expect(page.locator("#notes-lock-status")).to_be_visible()
        assert finish_canceled_task(data_dir, second["id"], owner_id=owner, elapsed_seconds=0.1)
        held_reads.pop(0).continue_()
    else:
        assert accepted.value.json()["status"] == "canceled"
    expect(cancel).to_be_hidden()
    expect(page.locator("#notes-lock-status")).to_be_hidden()


def test_regenerate_waits_for_a_concurrent_notes_task_without_losing_the_draft(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in(viewport={"width": 390, "height": 900})
    data_dir = live.settings.data_dir
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(data_dir)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    dialog = page.locator("#notes-regenerate-dialog")
    submit = page.locator("#notes-regenerate-submit")
    language = page.locator("#notes-regenerate-language")
    language.select_option("vi")
    expect(submit).to_be_enabled()

    other = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id="notes-second-operator"
    )
    page.evaluate(
        "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
        meeting_id,
    )
    expect(dialog).to_be_visible()
    expect(submit).to_be_disabled()
    expect(page.locator("#notes-regenerate-status")).to_contain_text("Chờ tác vụ kết thúc")
    expect(language).to_have_value("vi")
    _complete_fake_task(data_dir, other["id"], "other", "# Other operator\n")

    expect(submit).to_be_enabled(timeout=10_000)
    expect(language).to_have_value("vi")
    expect(page.locator("#notes-regenerate-status")).to_be_empty()
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/notes-runs")
    ) as response:
        submit.click()
    assert response.value.status == 202
    assert response.value.request.post_data_json["output_language"] == "vi"
    assert response.value.json()["id"] != other["id"]


def test_explicit_regenerate_dialog_uses_english_title_intro_and_error(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    meeting_id, _revision, _snapshot, _segment_ids = _seed_saved_notes(live.settings.data_dir)

    def fail_notes(route) -> None:
        route.fulfill(
            status=503,
            json={"detail": {"code": "temporary_failure", "message": "Try again."}},
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", fail_notes)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    dialog = page.locator("#notes-regenerate-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#notes-regenerate-title")).to_have_text("Recreate notes")
    expect(page.locator("#notes-regenerate-intro")).to_have_text(
        "Choose the recipe and saved transcript for this run. "
        "This choice does not change the setting for the next run."
    )
    effort = page.locator("#notes-regenerate-effort")
    expect(effort.locator("option")).to_have_text(
        ["Default", "Low", "Medium", "High", "Very high", "Maximum"]
    )
    expect(effort).to_have_value("high")
    expect(effort.locator("option:checked")).to_have_text("High")
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")) as original:
        page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-error")).to_have_text(
        "The server could not complete the request. Try again later."
    )
    assert original.value.post_data_json["run"]["reasoning_effort"] == "high"
    page.locator("#notes-regenerate-cancel").click()
    verify = page.locator("#notes-start")
    expect(verify).to_have_text("Verify notes request")
    plan = "The request to verify uses ChatGPT · GPT 6.1 Sol · thinking level High."
    expect(page.locator("#notes-run-plan")).to_have_text(plan)
    expect(verify).to_have_accessible_description(re.compile(rf"^{re.escape(plan)}"))
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")) as retry:
        verify.click()
    assert retry.value.post_data == original.value.post_data
    expect(dialog).to_be_visible()
    expect(effort.locator("option:checked")).to_have_text("High")


def test_regenerate_native_close_and_descendant_cancel_keep_modal_state_in_sync(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    dialog = page.locator("#notes-regenerate-dialog")
    page.locator("#notes-regenerate-provider").evaluate(
        "el => el.dispatchEvent(new Event('cancel', {bubbles: true, cancelable: true}))"
    )
    expect(dialog).to_be_visible()
    # Force a repaint in the same JS turn, before native close is delivered.
    # The still-open reducer intent must not reopen the natively closed dialog.
    assert (
        page.evaluate(
            """async () => {
          const {providerChoice} = await import('/assets/channel.js');
          const dialog = document.querySelector('#notes-regenerate-dialog');
          dialog.close();
          providerChoice.dispatch({type: 'channel/provider-choice-changed'});
          return dialog.open;
        }"""
        )
        is False
    )
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(dialog).to_be_hidden()
    # Refresh may still be loading when deferred focus chooses the Notes tab.
    # Finishing that load must preserve the already viable focus destination.
    focus = page.locator("#notes-start:focus, #meeting-tab-notes:focus")
    expect(focus).to_have_count(1)
    expect(focus).to_be_enabled()
    page.locator("#notes-start").click()
    expect(page.locator("#notes-regenerate-provider")).to_be_focused()
    # A close event queued by the previous dismissal cannot cancel a newly
    # opened native dialog whose current logical intent is open again.
    dialog.dispatch_event("close")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.locator("#notes-start")).to_be_focused()


def test_regenerate_close_focus_falls_back_to_notes_tab_during_provider_refresh(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    held_providers = []
    page.route(f"{live.url}/api/providers", lambda route: held_providers.append(route))
    page.locator("#notes-start").click()
    page.evaluate(
        """async () => {
          const {providerChoice} = await import('/assets/channel.js');
          document.querySelector('#notes-regenerate-dialog').close();
          providerChoice.dispatch({type: 'channel/provider-choice-changed'});
        }"""
    )
    expect(page.locator("#notes-regenerate-dialog")).to_be_hidden()
    expect(page.locator("#notes-start")).to_be_disabled()
    expect(page.locator("#notes-cancel")).to_be_hidden()
    expect(page.locator("#meeting-tab-notes")).to_be_focused()


def test_regenerate_close_does_not_steal_focus_after_user_moves(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    page.evaluate(
        """() => {
          document.querySelector('#notes-regenerate-cancel').click();
          document.querySelector('#meeting-tab-transcript').focus();
        }"""
    )
    expect(page.locator("#notes-regenerate-dialog")).to_be_hidden()
    # Wait across the deferred focus callback without a wall-clock sleep.
    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
    expect(page.locator("#meeting-tab-transcript")).to_be_focused()


def test_regenerate_native_close_during_submit_preserves_exact_pending_retry(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    held_requests = []
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes-runs",
        lambda route: held_requests.append(route),
    )
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    page.locator("#notes-regenerate-language").select_option("vi")
    page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-submit")).to_be_disabled()
    expect(page.locator("#notes-regenerate-cancel")).to_be_focused()
    page.wait_for_function("document.querySelector('#notes-regenerate-dialog').open")
    assert len(held_requests) == 1
    first_body = held_requests[0].request.post_data
    storage_key = f"asr-studio:notes-request-v2:{meeting_id}"
    stored = page.evaluate("key => localStorage.getItem(key)", storage_key)
    assert json.loads(stored) == json.loads(first_body)
    page.locator("#notes-regenerate-dialog").evaluate("el => el.close()")
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator("#notes-start")).to_be_focused()
    assert page.evaluate("key => localStorage.getItem(key)", storage_key) == stored
    held_requests[0].fulfill(status=503, json={"detail": {"code": "temporary_failure"}})
    page.locator("#notes-start").click()
    expect(page.locator("#notes-start")).to_be_disabled()
    page.wait_for_function("document.querySelector('#notes-status').textContent.length > 0")
    assert len(held_requests) == 2
    assert held_requests[1].request.post_data == first_body
    held_requests[1].fulfill(status=503, json={"detail": {"code": "temporary_failure"}})
    expect(page.locator("#notes-regenerate-dialog")).to_be_visible()
    expect(page.locator("#notes-regenerate-provider")).to_be_disabled()
    expect(page.locator("#notes-regenerate-submit")).to_be_enabled()
    expect(page.locator("#notes-regenerate-submit")).to_be_focused()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    expect(page.locator("#notes-status")).not_to_be_empty()
    assert page.evaluate("key => localStorage.getItem(key)", storage_key) == stored


def test_regenerate_catalog_reload_recovers_empty_default_in_open_dialog(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.route(
        f"{live.url}/api/notes-preferences",
        lambda route: route.fulfill(json={"provider": "chatgpt", "models": {}}),
    )
    catalog_requests = []

    def catalog(route):
        catalog_requests.append(route)
        if len(catalog_requests) == 1:
            route.fulfill(status=503, json={"detail": {"code": "temporary_failure"}})
        else:
            route.fulfill(
                json={
                    "models": [
                        {
                            "id": "gpt-6.1-sol",
                            "display_name": "GPT 6.1 Sol",
                            "reasoning_efforts": ["low", "high"],
                        }
                    ]
                }
            )

    page.route(f"{live.url}/api/providers/chatgpt/models", catalog)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    expect(page.locator("#notes-regenerate-reload")).to_be_visible()
    page.locator("#notes-regenerate-reload").click()
    expect(page.locator("#notes-regenerate-model")).to_have_value("gpt-6.1-sol")
    expect(page.locator("#notes-regenerate-submit")).to_be_enabled()
    expect(page.locator("#notes-regenerate-error")).to_be_hidden()
    expect(page.locator("#notes-regenerate-dialog")).to_be_visible()


def test_regenerate_rejected_choice_clears_error_on_edit_and_reopen(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes-runs",
        lambda route: route.fulfill(
            status=422, json={"detail": {"code": "model_unavailable", "message": "Unavailable"}}
        ),
    )
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    expect(page.locator("#notes-regenerate-cancel")).to_be_focused()
    expect(page.locator("#notes-status")).not_to_be_empty()
    page.locator("#notes-regenerate-model").select_option("gpt-6-luna")
    expect(page.locator("#notes-regenerate-error")).to_be_hidden()
    expect(page.locator("#notes-regenerate-submit")).to_be_enabled()
    page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    page.locator("#notes-regenerate-cancel").click()
    page.locator("#notes-start").click()
    expect(page.locator("#notes-regenerate-error")).to_be_hidden()
    expect(page.locator("#notes-regenerate-provider")).to_be_focused()


@pytest.mark.parametrize(("width", "locale"), [(1440, "en"), (390, "en"), (390, "vi")])
def test_regenerate_adopts_updated_single_source_only_after_confirmation(
    live, signed_in, width, locale
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('asr-studio:lang', {json.dumps(locale)})")
    adopt_label, source_label = (
        ("Use updated transcript source (version 2)", "Current transcript · source")
        if locale == "en"
        else ("Dùng nguồn transcript đã cập nhật (phiên bản 2)", "Bản transcript hiện tại · nguồn")
    )
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    requests = []
    preference_writes = []
    page.on(
        "request",
        lambda request: (
            preference_writes.append(request.url)
            if request.method == "PUT" and request.url.endswith("/api/notes-preferences")
            else None
        ),
    )

    def reject_stale_source(route):
        requests.append(json.loads(route.request.post_data or "{}"))
        with sqlite3.connect(database_path(live.settings.data_dir)) as connection:
            connection.execute(
                "UPDATE transcript_generations SET source_version = 2 WHERE id = ?",
                (snapshot["generation_id"],),
            )
        route.fulfill(status=409, json={"detail": {"code": "source_version_changed"}})

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", reject_stale_source)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-start").click()
    adopt = page.locator("#notes-regenerate-source-adopt")
    selected_source = page.locator("#notes-regenerate-source option:checked")
    expect(adopt).to_be_hidden()
    page.locator("#notes-regenerate-submit").click()
    expect(adopt).to_be_visible()
    expect(adopt).to_have_text(adopt_label)
    expect(page.locator("#notes-regenerate-source option")).to_have_count(1)
    expect(selected_source).to_have_text(f"{source_label} 1")
    expect(page.locator("#notes-regenerate-submit")).to_be_disabled()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    expect(page.locator("#notes-status")).not_to_be_empty()
    adopt.click()
    expect(adopt).to_be_hidden()
    expect(selected_source).to_have_text(f"{source_label} 2")
    expect(page.locator("#notes-regenerate-error")).to_be_hidden()
    expect(page.locator("#notes-regenerate-submit")).to_be_enabled()
    assert len(requests) == 1  # Confirmation only adopts; it never submits.
    assert requests[0]["run"]["source_version"] == 1
    page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    assert len(requests) == 2
    assert requests[1]["run"]["source_version"] == 2
    assert requests[1]["expected_source_version"] == 2
    assert requests[1]["request_id"] != requests[0]["request_id"]
    assert preference_writes == []


@pytest.mark.parametrize(("width", "theme"), [(1440, "light"), (390, "dark")])
def test_saved_notes_history_keeps_exact_versions_and_drafts(
    live, signed_in, tmp_path, width, theme
):
    context, page = signed_in(viewport={"width": width, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.emulate_media(color_scheme=theme)
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    meeting_id, revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    note_id = revision["id"]
    data_dir = live.settings.data_dir
    version_two = "# Saved edit two\n\n**Two is immutable.**\n"
    version_three = "# Saved edit three\n\nThree is the latest save.\n"
    update_note(data_dir, note_id, expected_edit_version=1, markdown=version_two)
    update_note(data_dir, note_id, expected_edit_version=2, markdown=version_three)
    _open_saved_notes(live, page, meeting_id)

    page.locator("#notes-mode-write").click()
    unsaved = "# Keep this draft\n"
    page.locator("#notes-markdown").fill(unsaved)
    history = page.locator("#notes-history-selector")
    trigger = page.locator("#notes-history-open")
    trigger.press("Enter")
    expect(history).to_be_focused()
    expect(history.locator("option")).to_have_count(4)
    expect(history.locator('option[value="1"]')).to_contain_text("Bản AI gốc")
    history.select_option("2")
    expect(page.locator("#notes-preview")).to_contain_text("Two is immutable.")
    expect(page.locator("#notes-preview")).not_to_contain_text("Three is the latest save.")
    expect(page.locator("#notes-save")).to_be_hidden()
    expect(page.locator("#notes-markdown")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") == 0
    page.screenshot(
        path=str(tmp_path / f"notes-history-{theme}-{width}.png"),
        animations="disabled",
        full_page=True,
    )

    link_button = page.locator("#notes-history-copy-link")
    link_button.click()
    expect(page.locator("#notes-history-status")).to_contain_text("Đã sao chép liên kết")
    fixed_url = page.evaluate("navigator.clipboard.readText()")
    params = parse_qs(urlsplit(fixed_url).query)
    assert params == {"notes_revision": [note_id], "notes_edit": ["2"]}
    assert page.url == fixed_url

    page.locator("#notes-copy").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("edit 2")
    assert page.evaluate("navigator.clipboard.readText()") == version_two
    with page.expect_download() as download:
        page.locator("#notes-export").click()
    version_download = tmp_path / "fixed.md"
    download.value.save_as(version_download)
    assert version_download.read_text() == version_two

    for trigger_id in ("notes-copy-rich", "notes-copy-email"):
        with page.expect_response(f"**/api/notes/{note_id}/versions/2") as reply:
            page.locator(f"#{trigger_id}").click()
        assert reply.value.ok
        expect(page.locator("#notes-copy-dialog")).to_be_visible()
        expect(page.locator("#notes-copy-pin")).to_contain_text("edit 2")
        expect(page.locator("#notes-copy-document")).to_contain_text("Two is immutable.")
        page.keyboard.press("Escape")
        expect(page.locator("#notes-copy-dialog")).to_be_hidden()

    page.locator("#notes-history-close").press("Enter")
    expect(trigger).to_be_focused()
    expect(history).to_be_hidden()
    expect(page.locator("#notes-markdown")).to_have_value(unsaved)
    assert not urlsplit(page.url).query
    page.locator("#notes-edit-cancel").click()
    expect(page.locator("#notes-preview")).to_contain_text("Three is the latest save.")
    page.locator("#notes-mode-write").click()
    version_four = "# Saved edit four\n\nFour is the latest save.\n"
    page.locator("#notes-markdown").fill(version_four)
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("Markdown đã được lưu")
    assert get_note(data_dir, note_id)["source_snapshot_id"] == snapshot["id"]
    assert page.request.get(f"{live.url}/api/notes/{note_id}/export").text() == version_four

    page.goto(fixed_url)
    expect(page.locator("#notes-preview")).to_contain_text("Two is immutable.")
    expect(history.locator('option[value="4"]')).to_have_count(1)
    expect(page.locator("#notes-copy")).to_be_enabled()
    exact_export = page.request.get(f"{live.url}/api/notes/{note_id}/versions/2/export")
    assert exact_export.ok and exact_export.text() == version_two

    page.goto(fixed_url.replace("notes_edit=2", "notes_edit=99"))
    expect(page.locator("#notes-history-status")).to_contain_text("Không thể đọc bản Notes")
    expect(page.locator("#notes-copy")).to_be_disabled()
    expect(page.locator("#notes-preview")).not_to_contain_text("Four is the latest save.")
    page.locator("#notes-history-close").click()
    expect(page.locator("#notes-markdown")).to_have_value(version_four)
    assert page_errors == []


@pytest.mark.parametrize(
    ("control", "refuse", "width"), [("button", False, 1440), ("select", True, 390)]
)
def test_fixed_link_returns_to_the_linked_notes_latest_while_detail_is_unavailable(
    live, signed_in, control, refuse, width
):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, linked, snapshot, _segments = _seed_saved_notes(data_dir)
    linked_id = linked["id"]
    latest_text = "# Latest of the linked Notes revision\n"
    update_note(data_dir, linked_id, expected_edit_version=1, markdown=latest_text)
    regenerated = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id="unrelated-regenerated-notes"
    )
    newer_text = "# Newer generated Notes revision\n"
    newer = _complete_fake_task(
        data_dir, regenerated["id"], "unrelated-regenerated-notes", newer_text
    )
    assert newer["id"] != linked_id

    held = []
    page.route(f"**/api/notes/{linked_id}/versions/1", lambda route: held.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}?notes_revision={linked_id}&notes_edit=1#notes")
    work_selector = page.locator("#notes-revision-selector")
    expect(work_selector.locator(f'option[value="{newer["id"]}"]')).to_have_count(1)
    assert len(held) == 1
    if refuse:
        held.pop().fulfill(status=404, json={"detail": {"code": "notes_version_not_found"}})
        expect(page.locator("#notes-history-status")).to_have_text(
            "The requested saved Notes version could not be read."
        )
    expect(page.locator("#notes-save")).to_be_hidden()

    history = page.locator("#notes-history-selector")
    if control == "button":
        page.get_by_role("button", name="Return to latest").press("Enter")
        expect(history).to_be_hidden()
        expect(page.locator("#notes-history-open")).to_be_focused()
    else:
        history.select_option("")
        expect(history).to_be_visible()
    expect(work_selector).to_have_value(linked_id)
    expect(page.locator("#notes-preview")).to_contain_text("Latest of the linked Notes revision")
    expect(page.locator("#notes-save")).to_be_visible()
    assert not urlsplit(page.url).query


@pytest.mark.parametrize(
    ("index_seen", "hold_projection"),
    [(True, False), (False, False), (True, True)],
    ids=["index-v4", "index-pending", "projection-pending"],
)
@pytest.mark.parametrize(("control", "width"), [("button", 1440), ("select", 390)])
def test_return_to_latest_reconciles_a_newer_save_before_editing_or_copying(
    live, signed_in, tmp_path, control, width, index_seen, hold_projection
):
    context, page = signed_in(viewport={"width": width, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    fixed = "# Fixed save two\n"
    obsolete = "# Obsolete save three\n"
    latest = "# Reconciled save four\n"
    update_note(data_dir, note_id, expected_edit_version=1, markdown=fixed)
    update_note(data_dir, note_id, expected_edit_version=2, markdown=obsolete)
    index_reads = []
    latest_reads = []
    page.route(f"**/api/notes/{note_id}/versions", lambda route: index_reads.append(route))
    page.route(f"**/api/notes/{note_id}", lambda route: latest_reads.append(route))
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes"
    meeting_url = f"**/api/meetings/{meeting_id}/notes"
    meeting_reads = []
    if hold_projection:
        request_id = "unrelated-notes-in-delayed-projection"
        task = _accept(data_dir, meeting_id, snapshot["generation_id"], request_id=request_id)
        other = _complete_fake_task(data_dir, task["id"], request_id, "# Other saved Notes")
        page.route(meeting_url, lambda route: meeting_reads.append(route))
        with page.expect_request(meeting_url):
            page.goto(fixed_url)
    else:
        with page.expect_response(meeting_url) as meeting_read:
            page.goto(fixed_url)
    preview = page.locator("#notes-preview")
    chooser = page.locator("#notes-history-selector")
    close = page.locator("#notes-history-close")
    status = page.locator("#notes-history-status")
    save = page.locator("#notes-save")
    expect(preview).to_contain_text("Fixed save two")
    projection = meeting_reads[0].fetch() if hold_projection else meeting_read.value
    revision = next(item for item in projection.json()["revisions"] if item["id"] == note_id)
    assert (revision["edit_version"], revision["markdown"]) == (3, obsolete)
    assert len(index_reads) == 1
    update_note(data_dir, note_id, expected_edit_version=3, markdown=latest)
    index_route = index_reads.pop()
    if index_seen:
        index_reply = index_route.fetch()
        assert index_reply.json()["latest_edit_version"] == 4
        index_route.fulfill(response=index_reply)
        expect(chooser.locator('option[value="4"]')).to_have_count(1)
    else:
        expect(chooser.locator('option[value="4"]')).to_have_count(0)

    expect(close).to_have_text("Return to latest")
    expect(chooser.locator('option[value=""]')).to_be_enabled()
    if control == "button":
        close.press("Enter")
    else:
        chooser.focus()
        chooser.press("Home")
    expect(status).to_have_text("Reading the latest saved version…")
    expect(close if control == "button" else chooser).to_be_focused()
    expect(preview).to_contain_text("Fixed save two")
    expect(chooser).to_have_value("2")
    expect(save).to_be_hidden()
    assert page.url == fixed_url
    assert len(latest_reads) == 1
    assert latest_reads[0].request.method == "GET"
    latest_reads.pop().fulfill(status=503, json={})
    expect(status).to_have_text(
        "Could not load the latest Notes. The fixed saved version is still selected."
    )
    expect(close).to_have_text("Retry loading latest")
    expect(close).to_be_focused()
    expect(preview).to_contain_text("Fixed save two")
    expect(preview).not_to_contain_text("Obsolete save three")
    expect(page.locator("#notes-mode-write")).to_be_hidden()
    expect(save).to_be_hidden()
    assert page.url == fixed_url
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") == 0
    ordering = (
        "projection-pending" if hold_projection else ("index-v4" if index_seen else "index-pending")
    )
    page.screenshot(path=tmp_path / f"{control}-{width}-{ordering}-failed.png", full_page=True)

    copy_status = page.locator("#notes-copy-export-status")
    page.locator("#notes-copy").click()
    expect(copy_status).to_contain_text("edit 2")
    assert page.evaluate("navigator.clipboard.readText()") == fixed
    assert not latest_reads

    close.press("Enter")
    expect(status).to_have_text("Reading the latest saved version…")
    expect(close).to_be_focused()
    assert len(latest_reads) == 1
    latest_reads.pop().continue_()
    expect(preview).to_contain_text("Reconciled save four")
    expect(save).to_be_visible()
    assert not urlsplit(page.url).query
    if control == "button":
        expect(chooser).to_be_hidden()
        expect(page.locator("#notes-history-open")).to_be_focused()
    else:
        expect(chooser).to_be_visible()
        expect(chooser).to_have_value("")
        expect(chooser).to_be_focused()
        assert len(index_reads) == 1
        index_reads.pop().continue_()
        expect(chooser.locator('option[value="4"]')).to_have_count(1)
    page.screenshot(path=tmp_path / f"{control}-{width}-{ordering}-latest.png", full_page=True)

    if hold_projection:
        meeting_reads.pop().fulfill(response=projection)
        work_selector = page.locator("#notes-revision-selector")
        expect(work_selector.locator(f'option[value="{other["id"]}"]')).to_have_count(1)
        expect(work_selector).to_have_value(note_id)
        expect(preview).to_contain_text("Reconciled save four")
        expect(preview).not_to_contain_text("Obsolete save three")

    with page.expect_request(f"**/api/notes/{note_id}"):
        page.locator("#notes-copy").click()
    assert len(latest_reads) == 1
    assert latest_reads[0].request.method == "GET"
    latest_reads.pop().continue_()
    expect(copy_status).to_contain_text("edit 4")
    assert page.evaluate("navigator.clipboard.readText()") == latest


@pytest.mark.parametrize("kind", ["copy", "export", "rich", "email"])
def test_fixed_share_survives_the_delayed_meeting_list(live, signed_in, kind):
    context, page = signed_in()
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    asset = context.request.get(f"{live.url}/assets/notes-core.js")
    assert asset.ok and asset.body() == Path("src/asr_studio/web/notes-core.js").read_bytes()

    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    fixed = "# Fixed two\n\nKeep **these bytes**.\n"
    update_note(data_dir, note_id, expected_edit_version=1, markdown=fixed)
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# New three\n")
    meeting_reads = []
    meeting_endpoint = f"**/api/meetings/{meeting_id}/notes"
    page.route(meeting_endpoint, lambda route: meeting_reads.append(route))
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes"
    page.goto(fixed_url)
    expect(page.locator("#notes-preview")).to_contain_text("Fixed two")
    option = page.locator(f'#notes-revision-selector option[value="{note_id}"]')
    expect(option).to_have_text("Saved version 2")

    share_reads = []
    detail_endpoint = f"**/api/notes/{note_id}/versions/2"
    page.route(detail_endpoint, lambda route: share_reads.append(route))
    trigger_id = f"notes-copy-{kind}" if kind in ("rich", "email") else f"notes-{kind}"
    trigger = page.locator(f"#{trigger_id}")
    with page.expect_request(detail_endpoint):
        trigger.click()
    dialog = page.locator("#notes-copy-dialog")
    confirm = dialog.locator("#notes-copy-confirm")
    if kind == "email":
        assert len(share_reads) == 1
        share_reads.pop().continue_()
        expect(dialog).to_be_visible()
        confirm.focus()

    projection = meeting_reads[0].fetch()
    assert projection.ok and projection.json()["revisions"][0]["edit_version"] == 3
    meeting_reads.pop().fulfill(response=projection)
    expect(option).not_to_have_text("Saved version 2")
    expect(page.locator("#notes-preview")).to_contain_text("Fixed two")
    assert page.url == fixed_url

    status = page.locator("#notes-copy-export-status")
    if kind == "export":
        with page.expect_download() as download:
            share_reads.pop().continue_()
        assert download.value.path().read_bytes() == fixed.encode()
        expect(status).to_contain_text("edit 2")
        expect(trigger).to_be_focused()
    elif kind == "copy":
        share_reads.pop().continue_()
        expect(status).to_have_text("Copied saved version · edit 2.")
        assert page.evaluate("navigator.clipboard.readText()") == fixed
        expect(trigger).to_be_focused()
    else:
        # The preview read and the confirmation read are deliberately separate. The
        # production revalidation must remain in place so a version purged while the
        # dialog is open cannot be copied from cached preview bytes.
        if kind == "rich":
            assert len(share_reads) == 1
            share_reads.pop().continue_()
        expect(dialog).to_be_visible()
        expect(dialog.locator("#notes-copy-pin")).to_contain_text("edit 2")
        expect(dialog.locator("#notes-copy-document")).to_contain_text("Fixed two")
        if kind == "email":
            expect(confirm).to_be_focused()
        with page.expect_request(detail_endpoint) as revalidation:
            confirm.click()
        assert revalidation.value.url.endswith("/api/notes/" + note_id + "/versions/2")
        assert len(share_reads) == 1
        share_reads.pop().continue_()
        expect(dialog.locator("#notes-copy-result")).to_have_text("Formatted content copied.")
        assert page.evaluate("navigator.clipboard.readText()") == fixed
        html = page.evaluate(
            """async () => {
              const [item] = await navigator.clipboard.read();
              return (await item.getType('text/html')).text();
            }"""
        )
        assert "<strong>these bytes</strong>" in html
        assert "New three" not in html
        expect(confirm).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()


@pytest.mark.parametrize(("kind", "lifecycle"), [("rich", "trashed"), ("email", "purged")])
def test_fixed_formatted_share_revalidates_after_trash_or_purge(live, signed_in, kind, lifecycle):
    context, page = signed_in()
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")

    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    fixed = "# Fixed two\n\nKeep **these bytes**.\n"
    update_note(data_dir, note_id, expected_edit_version=1, markdown=fixed)
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# New three\n")
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes"
    page.goto(fixed_url)
    expect(page.locator("#notes-preview")).to_contain_text("Fixed two")
    page.evaluate("navigator.clipboard.writeText('clipboard sentinel')")

    trigger = page.locator("#notes-copy-rich" if kind == "rich" else "#notes-copy-email")
    trigger.click()
    dialog = page.locator("#notes-copy-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#notes-copy-document")).to_contain_text("Fixed two")

    moved = trash_version(
        data_dir,
        meeting_id,
        operation_id=f"browser-share-{kind}-trash",
        note_id=note_id,
        edit_version=2,
    )
    assert moved["historical_outcome"]["status"] == "succeeded"
    if lifecycle == "purged":
        current = list_trash(data_dir, meeting_id)
        purged = empty_trash(
            data_dir,
            meeting_id,
            operation_id=f"browser-share-{kind}-empty",
            confirmation_digest=current["digest"],
            items=[
                {
                    "note_id": item["note_id"],
                    "edit_version": item["edit_version"],
                    "transition_id": item["transition_id"],
                }
                for item in current["items"]
            ],
        )
        assert purged["historical_outcome"]["status"] == "succeeded"

    detail_url = f"**/api/notes/{note_id}/versions/2"
    with page.expect_response(detail_url) as revalidation:
        dialog.locator("#notes-copy-confirm").click()
    assert revalidation.value.ok
    expect(dialog).to_be_visible()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "requested saved Notes version could not be read"
    )
    assert page.evaluate("navigator.clipboard.readText()") == "clipboard sentinel"


@pytest.mark.parametrize(
    ("width", "theme", "locale"),
    [(1440, "light", "en"), (1440, "dark", "vi"), (390, "light", "vi"), (390, "dark", "en")],
)
def test_trash_lists_each_revision_with_exact_links_restore_and_confirmation_set(
    live, signed_in, tmp_path, width, theme, locale
):
    context, page = signed_in(viewport={"width": width, "height": 900}, color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('asr-studio:lang', {json.dumps(locale)})")

    data_dir = live.settings.data_dir
    meeting_id, note, snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    suffix = meeting_id[:8]
    update_note(data_dir, note_id, expected_edit_version=1, markdown="# First fixed two\n")
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# First latest three\n")

    second_task = _accept(
        data_dir, meeting_id, snapshot["generation_id"], request_id=f"second-{suffix}"
    )
    second_snapshot = get_notes_source(data_dir, _task_source_id(data_dir, second_task["id"]))
    other = _complete_fake_task(
        data_dir,
        second_task["id"],
        f"second-publish-{suffix}",
        "# Second revision fixed one\n",
    )
    other_id = other["id"]
    update_note(data_dir, other_id, expected_edit_version=1, markdown="# Second fixed two\n")
    update_note(data_dir, other_id, expected_edit_version=2, markdown="# Second latest three\n")
    assert second_snapshot["generation_id"] == snapshot["generation_id"]

    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request)
            if request.method == "POST" and "/notes-trash" in request.url
            else None
        ),
    )
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes"
    page.goto(fixed_url)
    expect(page.locator("#notes-preview")).to_contain_text("First fixed two")
    move = page.locator("#notes-trash-move")
    expect(move).to_be_visible()
    move.click()
    confirmation = page.locator("#notes-trash-confirmation")
    expect(confirmation).to_be_visible()
    expect(confirmation.locator("#notes-trash-confirmation-items .notes-trash-item")).to_have_count(
        1
    )
    revision_prefix = "Version" if locale == "en" else "Bản"
    save_prefix = "Save version" if locale == "en" else "Bản Save"
    picker_options = page.locator("#notes-revision-selector option").evaluate_all(
        "options => options.map(option => [option.value, option.textContent])"
    )
    picker_labels = dict(picker_options)
    picker_ordinals = {
        value: re.match(rf"({re.escape(revision_prefix)} [12])(?:\s|$)", label).group(1)
        for value, label in picker_options
        if value in {note_id, other_id}
    }
    assert set(picker_ordinals) == {note_id, other_id}
    assert set(picker_ordinals.values()) == {f"{revision_prefix} 1", f"{revision_prefix} 2"}
    confirmation_text = confirmation.inner_text()
    assert note_id not in confirmation_text
    assert re.findall(rf"{re.escape(revision_prefix)} [12]", confirmation_text)
    assert f"{save_prefix} 2" in confirmation_text
    assert requests == []
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-confirm"
    )
    page.locator("#notes-trash-cancel").click()
    expect(confirmation).to_be_hidden()
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-move"
    )

    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/notes-trash")
    ) as moved_response:
        move.click()
        page.locator("#notes-trash-confirm").click()
    assert moved_response.value.status == 200
    first_transition_id = moved_response.value.json()["current_state"]["transition_id"]
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-status"
    )
    assert page.locator("#notes-trash-status").get_attribute("tabindex") == "-1"

    second_move = trash_version(
        data_dir,
        meeting_id,
        operation_id=f"browser-rows-{suffix}",
        note_id=other_id,
        edit_version=2,
    )
    assert second_move["historical_outcome"]["status"] == "succeeded"
    page.goto(fixed_url)
    with page.expect_response(
        lambda response: response.request.method == "GET" and response.url.endswith("/notes-trash")
    ) as refreshed_trash:
        page.locator("#notes-trash-reload").click()
    assert refreshed_trash.value.status == 200
    rows = page.locator("#notes-trash-items .notes-trash-item")
    expect(rows).to_have_count(2)
    identities = rows.evaluate_all(
        "items => items.map(item => [item.dataset.noteId, item.dataset.editVersion])"
    )
    assert {tuple(identity) for identity in identities} == {(note_id, "2"), (other_id, "2")}
    hrefs = page.locator("#notes-trash-items a[data-trash-fixed-link]").evaluate_all(
        "links => links.map(link => link.getAttribute('href'))"
    )
    assert set(hrefs) == {
        f"/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes",
        f"/meetings/{meeting_id}?notes_revision={other_id}&notes_edit=2#notes",
    }
    row_data = rows.evaluate_all(
        """items => items.map(row => {
          const link = row.querySelector('a[data-trash-fixed-link]');
          const restore = row.querySelector('button[data-trash-restore]');
          const heading = row.querySelector('.notes-trash-item-heading');
          return {
            noteId: row.dataset.noteId,
            editVersion: row.dataset.editVersion,
            heading: heading?.textContent || '',
            href: link?.getAttribute('href') || '',
            restoreNoteId: restore?.dataset.noteId || '',
            restoreEditVersion: restore?.dataset.editVersion || '',
            restoreTransitionId: restore?.dataset.transitionId || '',
          };
        })"""
    )
    assert {
        (
            item["noteId"],
            item["editVersion"],
            item["heading"],
            item["href"],
            item["restoreNoteId"],
            item["restoreEditVersion"],
            item["restoreTransitionId"],
        )
        for item in row_data
    } == {
        (
            note_id,
            "2",
            picker_labels[note_id] + f" · {save_prefix} 2",
            f"/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes",
            note_id,
            "2",
            first_transition_id,
        ),
        (
            other_id,
            "2",
            picker_labels[other_id] + f" · {save_prefix} 2",
            f"/meetings/{meeting_id}?notes_revision={other_id}&notes_edit=2#notes",
            other_id,
            "2",
            second_move["current_state"]["transition_id"],
        ),
    }
    row_text = rows.all_text_contents()
    assert all(
        "First fixed two" not in text and "Second fixed two" not in text for text in row_text
    )
    assert all(note_id not in text and other_id not in text for text in row_text)
    assert all(re.search(rf"{re.escape(revision_prefix)} [12]", text) for text in row_text)
    assert all(f"{save_prefix} 2" in text for text in row_text)
    assert (
        page.evaluate(
            """() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        })"""
        )["scrollWidth"]
        <= page.evaluate("document.documentElement.clientWidth") + 1
    )
    for row in rows.all():
        detail = page.request.get(
            f"{live.url}/api/notes/{row.get_attribute('data-note-id')}/versions/2"
        )
        assert detail.ok
        payload = detail.json()
        assert payload.get("content_available") is False
        assert "markdown" not in payload

    page.screenshot(
        path=str(tmp_path / f"notes-trash-{locale}-{theme}-{width}.png"), full_page=True
    )
    page.locator("#notes-trash-empty").click()
    confirmation_items = page.locator("#notes-trash-confirmation-items .notes-trash-item")
    expect(confirmation_items).to_have_count(2)
    confirmation_data = confirmation_items.evaluate_all(
        """items => items.map(row => ({
          noteId: row.dataset.noteId,
          editVersion: row.dataset.editVersion,
          heading: row.querySelector('.notes-trash-item-heading')?.textContent || '',
          href: row.querySelector('a[data-trash-fixed-link]')?.getAttribute('href') || '',
        }))"""
    )
    assert {
        (item["noteId"], item["editVersion"], item["heading"], item["href"])
        for item in confirmation_data
    } == {
        (
            note_id,
            "2",
            picker_labels[note_id] + f" · {save_prefix} 2",
            f"/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes",
        ),
        (
            other_id,
            "2",
            picker_labels[other_id] + f" · {save_prefix} 2",
            f"/meetings/{meeting_id}?notes_revision={other_id}&notes_edit=2#notes",
        ),
    }
    empty_confirmation_text = page.locator("#notes-trash-confirmation").inner_text()
    assert note_id not in empty_confirmation_text
    assert other_id not in empty_confirmation_text
    assert set(re.findall(rf"{re.escape(revision_prefix)} [12]", empty_confirmation_text)) == set(
        picker_ordinals.values()
    )
    assert f"{save_prefix} 2" in empty_confirmation_text
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-confirmation-check"
    )
    assert page.locator("#notes-trash-confirmation-check").is_visible()
    for restore in page.locator("#notes-trash-items button[data-trash-restore]").all():
        expect(restore).to_be_disabled()
    page.locator("#notes-trash-cancel").click()

    other_restore = page.locator(
        f'#notes-trash-items button[data-trash-restore][data-note-id="{other_id}"]'
        '[data-edit-version="2"]'
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/notes-trash/{other_id}/2/restore")
        )
    ) as restored_response:
        other_restore.click()
    assert restored_response.value.status == 200
    expect(page.locator("#notes-trash-items .notes-trash-item")).to_have_count(1)
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-status"
    )


@pytest.mark.parametrize("focus_target", ["restore", "fixed-link", "reload"])
def test_trash_labels_do_not_leak_note_id_while_revision_metadata_is_pending(
    live, signed_in, focus_target
):
    _context, page = signed_in(viewport={"width": 390, "height": 900})
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    update_note(data_dir, note_id, expected_edit_version=1, markdown="# Fixed two\n")
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# Latest three\n")
    trash_version(
        data_dir,
        meeting_id,
        operation_id="browser-pending-label",
        note_id=note_id,
        edit_version=2,
    )

    held_notes = []
    notes_url = f"{live.url}/api/meetings/{meeting_id}/notes"
    page.route(notes_url, lambda route: held_notes.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes")

    row = page.locator("#notes-trash-items .notes-trash-item")
    expect(row).to_have_count(1)
    pending_text = row.inner_text()
    assert note_id not in pending_text
    assert "Loading" in pending_text

    if focus_target == "restore":
        control = row.locator("button[data-trash-restore]")
        row.locator("a[data-trash-fixed-link]").focus()
        page.keyboard.press("Tab")
    elif focus_target == "fixed-link":
        control = row.locator("a[data-trash-fixed-link]")
        control.focus()
    else:
        control = page.locator("#notes-trash-reload")
        control.focus()
    expect(control).to_be_focused()

    assert held_notes
    held_notes.pop().continue_()
    expect(row).not_to_contain_text(note_id)
    expect(row).to_contain_text(re.compile(r"Version [12]"))
    expect(control).to_be_focused()


def test_trash_lifecycle_failure_focuses_real_status_region(live, signed_in, monkeypatch):
    _context, page = signed_in(viewport={"width": 390, "height": 900}, color_scheme="dark")
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    update_note(data_dir, note_id, expected_edit_version=1, markdown="# Failure fixed two\n")
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# Failure latest three\n")
    page.goto(f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes")
    move = page.locator("#notes-trash-move")
    move.click()
    expect(page.locator("#notes-trash-confirm")).to_be_focused()
    monkeypatch.setenv("ASR_NOTES_LIFECYCLE_FENCE", "1")
    try:
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/notes-trash")
            )
        ) as failed_response:
            page.locator("#notes-trash-confirm").click()
        assert failed_response.value.status == 503
    finally:
        monkeypatch.delenv("ASR_NOTES_LIFECYCLE_FENCE", raising=False)
    status = page.locator("#notes-trash-status")
    expect(status).to_be_visible()
    assert status.get_attribute("tabindex") == "-1"
    assert page.evaluate("document.activeElement && document.activeElement.id") == (
        "notes-trash-status"
    )


def test_returning_to_notes_refreshes_canceled_trash_read(live, signed_in):
    _context, page = signed_in()
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir)
    for version in range(2, 5):
        update_note(
            data_dir, note["id"], expected_edit_version=version - 1, markdown=f"# Save {version}\n"
        )
    trash_version(
        data_dir, meeting_id, operation_id="tab-return-trash", note_id=note["id"], edit_version=3
    )
    trash_url = f"{live.url}/api/meetings/{meeting_id}/notes-trash"
    reads = []
    page.route(trash_url, lambda route: reads.append(route))
    page.goto(f"{live.url}/meetings/{meeting_id}?notes_revision={note['id']}&notes_edit=2#notes")
    expect(page.locator("#notes-preview")).to_contain_text("Save 2")
    assert len(reads) == 1
    page.locator("#meeting-tab-transcript").click()
    with page.expect_request(trash_url, timeout=2_000) as fresh:
        page.locator("#meeting-tab-notes").click()
    route = next(route for route in reads if route.request == fresh.value)
    route.fulfill(response=route.fetch(timeout=5_000))
    page.unroute(trash_url)
    expect(page.locator("#notes-trash-items .notes-trash-item")).to_have_count(1)
    expect(page.locator("#notes-trash-items")).to_contain_text("Save version 3")
    expect(page.locator("#notes-trash-reload")).to_be_enabled()


def test_trash_navigation_recovers_reads_and_lost_write_without_duplicate_post(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir, title="Navigation A")
    update_note(data_dir, note["id"], expected_edit_version=1, markdown="# A fixed two\n")
    update_note(data_dir, note["id"], expected_edit_version=2, markdown="# A latest three\n")
    other_id, other_note, _other_snapshot, _other_segments = _seed_saved_notes(
        data_dir, title="Navigation B"
    )
    update_note(data_dir, other_note["id"], expected_edit_version=1, markdown="# B fixed two\n")
    update_note(data_dir, other_note["id"], expected_edit_version=2, markdown="# B latest three\n")

    trash_url = f"{live.url}/api/meetings/{meeting_id}/notes-trash"
    held_reads = []
    page.route(trash_url, lambda route: held_reads.append(route))
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note['id']}&notes_edit=2#notes"
    page.goto(fixed_url)
    expect(page.locator("#notes-preview")).to_contain_text("A fixed two")
    assert len(held_reads) == 1

    page.locator("#recent-list a").filter(has_text="Navigation B").click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text("Navigation B")
    page.locator("#notes-mode-write").click()
    b_draft = "# B draft kept while A settles\n"
    page.locator("#notes-markdown").fill(b_draft)
    assert page.evaluate("document.activeElement && document.activeElement.id") == "notes-markdown"

    page.locator("#recent-list a").filter(has_text="Navigation A").click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_have_text("Navigation A")
    with page.expect_request(trash_url) as fresh_read:
        page.locator("#notes-history-open").click()
    assert len(held_reads) == 2
    fresh_route = next(route for route in held_reads if route.request == fresh_read.value)
    fresh_route.fulfill(response=fresh_route.fetch(timeout=5_000))
    page.unroute(trash_url)
    selector = page.locator("#notes-history-selector")
    expect(selector.locator('option[value="2"]')).to_have_count(1)
    selector.select_option("2")
    expect(page.locator("#notes-trash-move")).to_be_visible()

    held_posts = []
    post_url = trash_url

    def hold_committed_post(route):
        held_posts.append((route, route.fetch(timeout=5_000)))

    page.route(post_url, hold_committed_post)
    posts = []
    page.on(
        "request",
        lambda request: (
            posts.append(request)
            if request.method == "POST" and request.url.endswith("/notes-trash")
            else None
        ),
    )
    page.locator("#notes-trash-move").click()
    page.locator("#notes-trash-confirm").click()
    for _ in range(500):
        if held_posts:
            break
        page.wait_for_timeout(10)
    assert len(held_posts) == 1
    first_post_body = held_posts[0][0].request.post_data_json
    assert any(
        item["note_id"] == note["id"] and item["edit_version"] == 2
        for item in list_trash(data_dir, meeting_id)["items"]
    )

    page.locator("#recent-list a").filter(has_text="Navigation B").click()
    page.wait_for_url(f"{live.url}/meetings/{other_id}")
    expect(page.locator("#meeting-title")).to_have_text("Navigation B")
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(b_draft)
    # The committed A response is intentionally released only after B is active.
    with suppress(Exception):
        # Navigation may already have aborted the browser-side response; the
        # server commit remains observed above.
        held_posts[0][0].fulfill(response=held_posts[0][1])
    assert page.locator("#notes-markdown").input_value() == b_draft
    assert page.evaluate("document.activeElement && document.activeElement.id") == "notes-markdown"

    page.unroute(post_url)
    page.locator("#recent-list a").filter(has_text="Navigation A").click()
    page.wait_for_url(f"{live.url}/meetings/{meeting_id}")
    expect(page.locator("#meeting-title")).to_have_text("Navigation A")
    page.locator("#notes-history-open").click()
    expect(page.locator("#notes-trash-items .notes-trash-item")).to_have_count(1)
    status = page.locator("#notes-trash-status")
    expect(status).to_contain_text("Could not confirm the Trash action")
    expect(page.locator("#notes-trash-retry")).to_be_visible()
    expect(page.locator("#notes-trash-items button[data-trash-restore]")).to_be_disabled()
    assert len(posts) == 1
    with page.expect_request(
        lambda request: request.method == "POST" and request.url == post_url
    ) as retry_request:
        page.locator("#notes-trash-retry").click()
    assert retry_request.value.post_data_json == first_post_body
    expect(status).to_contain_text("Complete")
    assert len(posts) == 2


def test_fixed_return_to_latest_keeps_the_projected_floor_after_omission(live, signed_in):
    _context, page = signed_in()
    page.add_init_script("localStorage.setItem('asr-studio:lang', 'en')")
    data_dir = live.settings.data_dir
    meeting_id, note, _snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = note["id"]
    for previous, heading in enumerate(("Fixed two", "Save three", "Stale four"), start=1):
        update_note(data_dir, note_id, expected_edit_version=previous, markdown=f"# {heading}\n")

    meeting_endpoint = f"**/api/meetings/{meeting_id}/notes"
    current_endpoint = f"**/api/notes/{note_id}"
    meeting_reads = []
    current_reads = []
    page.route(meeting_endpoint, lambda route: meeting_reads.append(route))
    page.route(current_endpoint, lambda route: current_reads.append(route))
    fixed_url = f"{live.url}/meetings/{meeting_id}?notes_revision={note_id}&notes_edit=2#notes"
    page.goto(fixed_url)
    preview = page.locator("#notes-preview")
    chooser = page.locator("#notes-history-selector")
    status = page.locator("#notes-history-status")
    retry = page.locator("#notes-history-close")
    option = page.locator(f'#notes-revision-selector option[value="{note_id}"]')
    expect(preview).to_contain_text("Fixed two")
    expect(option).to_have_text("Saved version 2")
    expect(chooser.locator('option[value="4"]')).to_have_count(1)
    chooser.focus()
    chooser.press("Home")
    expect(status).to_have_text("Reading the latest saved version…")
    expect(chooser).to_be_focused()
    stale = current_reads[0].fetch()
    assert stale.ok and stale.json()["edit_version"] == 4

    update_note(data_dir, note_id, expected_edit_version=4, markdown="# Latest five\n")
    meeting_route = meeting_reads.pop()
    projection = meeting_route.fetch()
    body = projection.json()
    assert projection.ok and body["revisions"][0]["edit_version"] == 5
    meeting_route.fulfill(response=projection)
    expect(option).not_to_have_text("Saved version 2")
    with page.expect_request(meeting_endpoint):
        page.evaluate(
            "async id => (await import('/assets/history.js')).meetingHistory.refreshMeeting(id)",
            meeting_id,
        )
    meeting_reads.pop().fulfill(json={**body, "revisions": []})
    expect(option).to_have_text("Saved version 2")
    current_reads.pop().fulfill(response=stale)
    expect(status).to_have_text(
        "Could not load the latest Notes. The fixed saved version is still selected."
    )
    expect(retry).to_be_focused()
    expect(chooser).to_have_value("2")
    expect(preview).to_contain_text("Fixed two")
    expect(page.locator("#notes-save")).to_be_hidden()
    assert page.url == fixed_url

    with page.expect_request(current_endpoint):
        retry.press("Enter")
    current_reads.pop().continue_()
    expect(chooser).to_be_focused()
    expect(chooser).to_have_value("")
    expect(preview).to_contain_text("Latest five")
    expect(page.locator("#notes-save")).to_be_visible()
    assert not urlsplit(page.url).query


@pytest.mark.parametrize(("navigation", "locale"), [("back", "en"), ("citation", "vi")])
def test_fixed_history_reconciles_latest_on_navigation(live, signed_in, navigation, locale):
    _context, page = signed_in()
    page.add_init_script(f"localStorage.setItem('asr-studio:lang', {json.dumps(locale)})")
    data_dir = live.settings.data_dir
    meeting_id, generation_id, _segments = seed_completed_transcript(data_dir)
    task = _accept(data_dir, meeting_id, generation_id, request_id="seed-navigation")
    snapshot = get_notes_source(data_dir, _task_source_id(data_dir, task["id"]))
    original = "The team reviewed its launch plan."
    segment = snapshot["segments"][0]
    note = _complete_fake_task(
        data_dir,
        task["id"],
        "navigation",
        f"## Overview\n\n- {original}\n",
        citations=[
            {
                "section": "Overview",
                "text": original,
                "sources": [{"id": segment["id"], "start_seconds": segment["start_seconds"]}],
            }
        ],
    )
    note_id = note["id"]
    update_note(data_dir, note_id, expected_edit_version=1, markdown="# Cached save two\n")
    current_url = f"{live.url}/meetings/{meeting_id}#notes"
    page.goto(current_url)
    preview = page.locator("#notes-preview")
    expect(preview).to_contain_text("Cached save two")
    page.locator("#notes-mode-write").click()
    draft = "# Unsaved local draft"
    editor = page.locator("#notes-markdown")
    editor.fill(draft)

    # Preserve the cached meeting projection while the versions endpoint sees a newer save.
    page.route(f"**/api/meetings/{meeting_id}/notes", lambda route: route.abort())
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# Latest save three\n")
    if navigation == "back":
        # The chooser replaces its entry. Give it a separate same-document entry so
        # the real browser Back button can restore the ordinary Notes address.
        page.evaluate("history.pushState(history.state, '', location.href)")
    page.locator("#notes-history-open").press("Enter")
    chooser = page.locator("#notes-history-selector")
    chooser.select_option("1")
    expect(chooser.locator('option[value="3"]')).to_have_count(1)
    expect(preview).to_contain_text(original)
    fixed_url = page.url

    endpoint = f"**/api/notes/{note_id}"
    pending = []
    page.route(endpoint, lambda route: pending.append(route))
    if navigation == "back":
        chooser.focus()
        page.go_back()
        assert page.url == current_url
    else:
        preview.locator('a[href*="notes_source="]').first.click()
        expect(page.locator("#notes-source-view")).to_be_visible()
        assert urlsplit(page.url).fragment == "transcript"
        assert not pending
        # Back from the citation restores the exact fixed entry, then the Notes
        # tab from the citation address reconciles the current entry.
        page.go_back()
        assert page.url == fixed_url
        expect(preview).to_contain_text(original)
        assert not pending
        preview.locator('a[href*="notes_source="]').first.click()
        page.locator("#meeting-tab-notes").click()
        assert "notes_source=" in page.url and urlsplit(page.url).fragment == "notes"

    status = page.locator("#notes-history-status")
    waiting, failed = (
        (
            "Reading the latest saved version…",
            "Could not load the latest Notes. The fixed saved version is still selected.",
        )
        if locale == "en"
        else (
            "Đang đọc phiên bản đã lưu mới nhất…",
            "Không thể đọc bản Notes mới nhất. Vẫn đang chọn bản lưu cố định.",
        )
    )
    expect(status).to_have_text(waiting)
    focused = chooser if navigation == "back" else page.locator("#meeting-tab-notes")
    expect(focused).to_be_focused()
    expect(preview).to_contain_text(original)
    expect(editor).to_be_hidden()
    assert len(pending) == 1
    pending.pop().fulfill(status=503, json={"detail": "Temporary failure"})
    expect(status).to_have_text(failed)
    retry = page.locator("#notes-history-close")
    expect(retry if navigation == "back" else focused).to_be_focused()
    expect(editor).to_be_hidden()

    with page.expect_request(endpoint):
        retry.press("Enter")
    pending.pop().continue_()
    expect(editor).to_have_value(draft)
    expect(page.locator("#notes-history-open")).to_be_focused()
    assert get_note(data_dir, note_id)["markdown"] == "# Latest save three\n"
    expect(page.locator("#notes-conflict")).to_be_visible()
    page.locator("#notes-use-latest").click()
    expect(editor).to_have_value("# Latest save three\n")


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_notes_history_explains_which_legacy_versions_are_available(live, signed_in, locale):
    _context, page = signed_in(viewport={"width": 390, "height": 900})
    page.add_init_script(f"localStorage.setItem('asr-studio:lang', {json.dumps(locale)})")
    data_dir = live.settings.data_dir
    meeting_id, revision, _snapshot, _segments = _seed_saved_notes(data_dir)
    note_id = revision["id"]
    update_note(data_dir, note_id, expected_edit_version=1, markdown="# Retained legacy save\n")
    # Recreate the legacy baseline from the surviving projection in this test's database.
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("DELETE FROM notes_saved_versions WHERE note_id = ?", (note_id,))
        install_notes_history(connection)
    update_note(data_dir, note_id, expected_edit_version=2, markdown="# New save after upgrade\n")

    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-history-open").press("Enter")
    history = page.locator("#notes-history-selector")
    notice = page.locator("#notes-history-legacy")
    expected = (
        "Retained history starts at save 2. The AI original and earlier saves were overwritten "
        "before the upgrade."
        if locale == "en"
        else "Lịch sử còn lưu bắt đầu ở bản lưu 2. Bản AI gốc và các lần lưu trước đó "
        "đã bị ghi đè trước khi nâng cấp."
    )
    expect(notice).to_have_text(expected)
    expect(history.locator('option[value="1"]')).to_have_count(0)
    history.select_option("3")
    expect(page.locator("#notes-preview")).to_contain_text("New save after upgrade")
    expect(notice).to_have_text(expected)
    expect(history).to_have_accessible_description(re.compile(re.escape(expected)))


def _contrast(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(value) / 255 for value in re.findall(r"\d+", color)[:3]]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("width", [1440, 390])
def test_saved_notes_are_one_full_width_frame_with_a_view_write_group(live, signed_in, width):
    _context, page = signed_in(viewport={"width": width, "height": 900})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)

    expect(page.locator(".notes-frame")).to_have_count(1)
    expect(page.get_by_role("heading", name="Xem trước Markdown")).to_have_count(0)
    expect(page.get_by_role("heading", name="Markdown đã lưu")).to_have_count(0)
    group = page.get_by_role("group", name="Chế độ notes")
    expect(group).to_have_count(1)
    buttons = group.get_by_role("button")
    expect(buttons).to_have_count(3)
    expect(buttons.nth(0)).to_have_accessible_name("Xem")
    expect(buttons.nth(1)).to_have_accessible_name("Viết")
    expect(buttons.nth(2)).to_have_accessible_name("Markdown")
    for index in range(3):
        expect(buttons.nth(index)).to_have_attribute("aria-pressed", re.compile("^(true|false)$"))

    inner_width = page.evaluate(
        """() => {
          const panel = document.querySelector("#notes-panel");
          const style = getComputedStyle(panel);
          return panel.getBoundingClientRect().width
            - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
            - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
        }"""
    )
    frame = page.locator(".notes-frame").bounding_box()
    assert frame is not None and abs(frame["width"] - inner_width) <= 1
    for mode, shown, hidden in (
        ("view", "#notes-preview", "#notes-markdown"),
        ("write", "#notes-markdown", "#notes-preview"),
    ):
        page.locator(f"#notes-mode-{mode}").click()
        expect(page.locator(shown)).to_be_visible()
        expect(page.locator(hidden)).to_be_hidden()
        box = page.locator(shown).bounding_box()
        assert box is not None and abs(box["width"] - (frame["width"] - 2)) <= 1


def test_notes_open_in_view_mode_and_remember_the_last_choice(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    view, write = page.locator("#notes-mode-view"), page.locator("#notes-mode-write")
    stored = "localStorage.getItem('asr-studio:notes-view-mode')"

    expect(view).to_have_attribute("aria-pressed", "true")
    expect(write).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#notes-preview")).to_be_visible()
    expect(page.locator("#notes-markdown")).to_be_hidden()

    write.click()
    expect(write).to_have_attribute("aria-pressed", "true")
    assert page.evaluate(stored) == "write"
    page.reload()
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(write).to_have_attribute("aria-pressed", "true")
    expect(view).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#notes-markdown")).to_be_visible()
    expect(page.locator("#notes-preview")).to_be_hidden()

    view.click()
    assert page.evaluate(stored) == "view"
    page.reload()
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(view).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#notes-preview")).to_be_visible()


@pytest.mark.parametrize("storage", ["blocked", "corrupt"])
def test_notes_view_mode_falls_back_to_view_when_storage_fails(live, signed_in, storage):
    _context, page = signed_in()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    if storage == "blocked":
        page.add_init_script(
            "Object.defineProperty(window, 'localStorage', { configurable: true, "
            "get() { throw new Error('local storage unavailable'); } });"
        )
    else:
        page.evaluate("localStorage.setItem('asr-studio:notes-view-mode', 'sideways')")
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)

    expect(page.locator("#notes-mode-view")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#notes-preview")).to_be_visible()
    page.locator("#notes-mode-write").click()
    expect(page.locator("#notes-markdown")).to_be_visible()
    page.locator("#notes-markdown").fill("# Draft")
    page.locator("#notes-mode-view").click()
    expect(page.locator("#notes-preview")).to_be_visible()
    expect(page.get_by_text("Đang xem bản nháp chưa lưu", exact=True)).to_be_visible()
    assert page_errors == []


def test_view_mode_renders_the_current_draft_in_readable_type_below_the_panel_heading(
    live, signed_in
):
    _context, page = signed_in(viewport={"width": 1440, "height": 1000})
    meeting_id, revision, *_ = _seed_saved_notes(live.settings.data_dir)
    markdown = (
        "# Weekly sync\n\nA plain paragraph.\n\n## Decisions\n\n- Ship **now**\n\n"
        "### Detail\n\n- Check `logs`\n"
    )
    update_note(live.settings.data_dir, revision["id"], expected_edit_version=1, markdown=markdown)
    _open_saved_notes(live, page, meeting_id)
    preview = page.locator("#notes-preview")
    headings = (
        "root => [...root.querySelectorAll('h1, h2, h3, h4, h5, h6')].map(item => item.tagName)"
    )

    assert page.locator("#notes-heading").evaluate("element => element.tagName") == "H3"
    expect(preview).to_contain_text("A plain paragraph.")
    assert preview.evaluate(headings) == ["H4", "H5", "H6"]
    sizes = preview.evaluate(
        "root => [...root.querySelectorAll('p, li')].map(item => "
        "parseFloat(getComputedStyle(item).fontSize))"
    )
    assert sizes and min(sizes) >= 14

    # Xem shows the current draft, saved or not; the shallowest heading is always an h4.
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill("## Only a draft\n\n### Nested\n\nUnsaved words.\n")
    page.locator("#notes-mode-view").click()
    expect(preview).to_contain_text("Unsaved words.")
    assert preview.evaluate(headings) == ["H4", "H5"]
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == markdown


def test_write_mode_shows_a_full_width_readable_textarea(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 1000})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    page.locator("#notes-mode-write").click()

    textarea = page.locator("#notes-markdown")
    expect(textarea).to_be_visible()
    expect(page.locator("#notes-preview")).to_be_hidden()
    box, frame = textarea.bounding_box(), page.locator(".notes-frame").bounding_box()
    assert box is not None and frame is not None
    assert box["height"] >= 420
    assert abs(box["width"] - (frame["width"] - 2)) <= 1
    # The height comes from min-height, not from the number of rows.
    assert textarea.evaluate("area => { area.rows = 2; return area.offsetHeight; }") >= 420
    assert float(textarea.evaluate("element => getComputedStyle(element).fontSize")[:-2]) >= 13


def test_switching_modes_keeps_the_draft_and_the_caret_and_calls_no_api(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 1000})
    meeting_id, revision, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    textarea = page.locator("#notes-markdown")
    draft = "# Kept draft\n\nThe caret stays inside this sentence.\n"
    page.locator("#notes-mode-write").click()
    textarea.fill(draft)
    page.evaluate(
        "() => { const area = document.querySelector('#notes-markdown'); "
        "area.setSelectionRange(14, 22, 'backward'); }"
    )
    calls: list[str] = []
    page.on(
        "request", lambda request: calls.append(request.url) if "/api/" in request.url else None
    )

    for _ in range(3):
        page.locator("#notes-mode-view").click()
        expect(page.locator("#notes-preview")).to_contain_text("The caret stays inside")
        page.locator("#notes-mode-write").click()
        state = textarea.evaluate(
            "area => ({ value: area.value, start: area.selectionStart, end: area.selectionEnd, "
            "direction: area.selectionDirection, focused: document.activeElement === area })"
        )
        assert state == {
            "value": draft,
            "start": 14,
            "end": 22,
            "direction": "backward",
            "focused": True,
        }
    assert calls == []
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == revision["markdown"]


def test_unsaved_changes_mark_the_write_button_and_the_view(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    write, dot = page.locator("#notes-mode-rich"), page.locator("#notes-unsaved-dot")
    line = page.get_by_text("Đang xem bản nháp chưa lưu", exact=True)

    expect(write).to_have_attribute("aria-label", "Viết")
    expect(dot).to_be_hidden()
    expect(line).to_be_hidden()

    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill("# Changed\n\nNot saved yet.\n")
    expect(dot).to_be_visible()
    expect(write).to_have_attribute("aria-label", re.compile("chưa lưu"))
    expect(page.get_by_role("button", name=re.compile("Viết.*chưa lưu"))).to_have_count(1)
    expect(line).to_be_hidden()

    page.locator("#notes-mode-view").click()
    expect(line).to_be_visible()
    expect(page.locator("#notes-preview")).to_contain_text("Not saved yet.")
    expect(dot).to_be_visible()

    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )
    expect(line).to_be_hidden()
    expect(dot).to_be_hidden()
    expect(write).to_have_attribute("aria-label", "Viết")


def test_one_toolbar_serves_both_modes_and_keeps_the_enable_rules(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 1000})
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    toolbar = page.locator("#notes-toolbar")
    controls = ["#notes-revision-selector", "#notes-save", "#notes-copy", "#notes-export"]
    layout = """controls => {
      const toolbar = document.querySelector("#notes-toolbar").getBoundingClientRect();
      return controls.map(selector => {
        const box = document.querySelector(selector).getBoundingClientRect();
        return [box.x - toolbar.x, box.y - toolbar.y, box.width, box.height].map(Math.round);
      });
    }"""
    save, copy, export = (page.locator(f"#notes-{name}") for name in ("save", "copy", "export"))

    layouts = []
    for mode in ("view", "write"):
        page.locator(f"#notes-mode-{mode}").click()
        for selector in controls:
            expect(toolbar.locator(selector)).to_be_visible()
        layouts.append(page.evaluate(layout, controls))
        expect(save).to_be_disabled()
        expect(copy).to_be_enabled()
        expect(export).to_be_enabled()
    assert layouts[0] == layouts[1]
    # Empty action feedback uses no space; populated live regions sit by their controls.
    expect(page.locator("#notes-copy-export-status")).to_be_hidden()
    assert page.evaluate(
        """() => {
          const toolbar = document.querySelector("#notes-toolbar").getBoundingClientRect();
          const frame = document.querySelector(".notes-frame").getBoundingClientRect();
          return ["#notes-draft-status"].map(selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return box.top >= toolbar.bottom && box.bottom <= frame.top;
          });
        }"""
    ) == [True]
    expect(save).to_have_accessible_name("Lưu bản nháp")
    expect(copy).to_have_accessible_name("Sao chép bản đã lưu")
    expect(export).to_have_accessible_name("Tải Markdown đã lưu")

    page.locator("#notes-markdown").fill("# Draft")
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text("Markdown đã được lưu")
    assert (
        page.locator("#notes-copy-export-status").bounding_box()["y"]
        < page.locator(".notes-frame").bounding_box()["y"]
    )
    page.locator("#notes-markdown").fill("# Another draft")
    for mode in ("write", "view"):
        page.locator(f"#notes-mode-{mode}").click()
        expect(save).to_be_enabled()
        expect(copy).to_be_disabled()
        expect(export).to_be_disabled()


def test_conflict_block_and_its_two_actions_show_in_both_modes(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 1000})
    meeting_id, revision, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    draft = "# Local draft\n\nKept through the conflict.\n"
    remote = "# Remote edit\n\nSaved elsewhere first.\n"
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(draft)
    update_note(live.settings.data_dir, revision["id"], expected_edit_version=1, markdown=remote)
    with page.expect_response(lambda response: response.status == 409):
        page.locator("#notes-save").click()

    for mode in ("write", "view", "write", "view"):
        page.locator(f"#notes-mode-{mode}").click()
        expect(page.locator("#notes-conflict")).to_be_visible(timeout=10_000)
        expect(page.locator("#notes-conflict-message")).to_contain_text(
            "Bản nháp của bạn vẫn được giữ"
        )
        expect(page.locator("#notes-keep-draft")).to_be_visible()
        expect(page.locator("#notes-use-latest")).to_be_visible()
        expect(page.locator("#notes-copy")).to_be_disabled()
        expect(page.locator("#notes-export")).to_be_disabled()
        expect(page.locator("#notes-markdown")).to_have_value(draft)

    page.locator("#notes-use-latest").click()
    expect(page.locator("#notes-conflict")).to_be_hidden()
    expect(page.locator("#notes-preview")).to_contain_text("Saved elsewhere first.")
    expect(page.locator("#notes-markdown")).to_have_value(remote)


def test_ctrl_or_cmd_enter_saves_the_draft_like_the_save_button(live, signed_in):
    _context, page = signed_in()
    meeting_id, revision, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    saves: list[dict[str, object]] = []
    page.on(
        "request",
        lambda request: (
            saves.append(json.loads(request.post_data or "{}"))
            if request.method == "PATCH" and "/api/notes/" in request.url
            else None
        ),
    )
    status = page.locator("#notes-copy-export-status")
    textarea = page.locator("#notes-markdown")
    page.locator("#notes-mode-write").click()

    textarea.fill("# Saved by Control\n")
    textarea.press("Control+Enter")
    expect(status).to_contain_text("Markdown đã được lưu", timeout=10_000)
    assert saves == [{"expected_edit_version": 1, "markdown": "# Saved by Control\n"}]
    assert get_note(live.settings.data_dir, revision["id"])["markdown"] == "# Saved by Control\n"
    expect(textarea).to_have_value("# Saved by Control\n")
    expect(textarea).to_be_focused()

    textarea.press("Control+Enter")
    page.wait_for_timeout(200)
    assert len(saves) == 1

    textarea.fill("# Saved by Command\n")
    textarea.press("Meta+Enter")
    expect(page.locator("#notes-revision-meta")).to_contain_text("Edit 3", timeout=10_000)
    assert saves[1] == {"expected_edit_version": 2, "markdown": "# Saved by Command\n"}

    textarea.fill("# Saved by the button\n")
    page.locator("#notes-save").click()
    expect(page.locator("#notes-revision-meta")).to_contain_text("Edit 4", timeout=10_000)
    assert saves[2] == {"expected_edit_version": 3, "markdown": "# Saved by the button\n"}

    textarea.fill("one")
    textarea.press("Enter")
    expect(textarea).to_have_value("one\n")
    page.wait_for_timeout(200)
    assert len(saves) == 3


def test_beforeunload_warns_only_while_a_notes_or_transcript_draft_is_unsaved(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    warns = """() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }"""
    assert page.evaluate(warns) is False

    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill("# Unsaved notes draft\n")
    assert page.evaluate(warns) is True
    page.locator("#notes-mode-view").click()
    assert page.evaluate(warns) is True
    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )
    assert page.evaluate(warns) is False

    page.locator("#meeting-tab-transcript").click()
    first_segment = page.locator(".transcript-segment").nth(0)
    first_segment.locator(".segment-edit-button").click()
    editor = first_segment.locator(".segment-text-editor")
    saved_text = editor.input_value()
    editor.fill(f"{saved_text} and an unsaved edit")
    assert page.evaluate(warns) is True
    editor.fill(saved_text)
    assert page.evaluate(warns) is False


def test_closing_the_page_with_an_unsaved_draft_opens_the_browser_warning(live, signed_in):
    _context, page = signed_in()
    meeting_id, *_ = _seed_saved_notes(live.settings.data_dir)
    _open_saved_notes(live, page, meeting_id)
    dialogs: list[str] = []

    def stay(dialog) -> None:
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", stay)
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill("# Unsaved notes draft\n")
    with page.expect_event("dialog"):
        page.close(run_before_unload=True)
    assert dialogs == ["beforeunload"]
    assert not page.is_closed()

    page.locator("#notes-save").click()
    expect(page.locator("#notes-copy-export-status")).to_contain_text(
        "Markdown đã được lưu", timeout=10_000
    )
    with page.expect_event("close", timeout=10_000):
        page.close(run_before_unload=True)
    assert dialogs == ["beforeunload"]


def test_source_links_in_the_rendered_view_still_open_the_source_card(live, signed_in):
    _context, page = signed_in(viewport={"width": 1280, "height": 900})
    meeting_id, _revision, snapshot, segment_ids = _seed_saved_notes(live.settings.data_dir)
    source = f"/meetings/{meeting_id}?notes_source={snapshot['id']}&segment={segment_ids[0]}"
    _open_saved_notes(live, page, meeting_id)

    # Saved notes hide generated timestamp links, so a valid link reaches Xem through the draft.
    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(
        f"## Decisions\n\n- Keep the scope [00:00:01]({source})\n"
        "- A foreign link [elsewhere](https://attacker.invalid/x)\n"
    )
    page.locator("#notes-mode-view").click()
    source_link = page.locator("#notes-preview a[data-notes-source-link]")
    expect(source_link).to_have_count(1)
    expect(page.locator("#notes-preview a").filter(has_text="elsewhere")).to_have_attribute(
        "href", "https://attacker.invalid/x"
    )
    expect(page.locator("#notes-source-view")).to_be_hidden()
    source_link.click()
    expect(page.locator("#notes-source-view")).to_be_visible()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-time")).to_have_text("00:00:01")
    assert parse_qs(urlsplit(page.url).query) == {
        "notes_source": [snapshot["id"]],
        "segment": [segment_ids[0]],
    }


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_notes_frame_fits_360px_with_visible_focus_and_readable_colors(live, signed_in, scheme):
    _context, page = signed_in(viewport={"width": 360, "height": 800}, color_scheme=scheme)
    meeting_id, revision, *_ = _seed_saved_notes(live.settings.data_dir)
    unbroken = "x" * 400
    update_note(
        live.settings.data_dir,
        revision["id"],
        expected_edit_version=1,
        markdown=f"## Long\n\n- {unbroken}\n\n`{unbroken}`\n\n[{unbroken}](https://x.invalid/)\n",
    )
    _open_saved_notes(live, page, meeting_id)
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    overflow = "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    frame_overflow = (
        "() => { const frame = document.querySelector('.notes-frame'); "
        "return frame.scrollWidth - frame.clientWidth; }"
    )

    page.locator("#notes-mode-write").click()
    page.locator("#notes-markdown").fill(f"## Draft\n\n- {unbroken}\n")
    for mode in ("write", "view"):
        page.locator(f"#notes-mode-{mode}").click()
        assert page.evaluate(overflow) == 0
        assert page.evaluate(frame_overflow) == 0

    pairs = page.evaluate(
        """() => {
          const read = selector => {
            const style = getComputedStyle(document.querySelector(selector));
            return [style.color, style.backgroundColor];
          };
          return {
            pressed: read("#notes-mode-view"),
            idle: [getComputedStyle(document.querySelector("#notes-mode-write")).color,
                   getComputedStyle(document.querySelector(".notes-mode-toggle")).backgroundColor],
            banner: read("#notes-view-draft"),
            text: [getComputedStyle(document.querySelector("#notes-preview li")).color,
                   getComputedStyle(document.querySelector(".notes-frame")).backgroundColor],
          };
        }"""
    )
    for name, (foreground, background) in pairs.items():
        assert _contrast(foreground, background) >= 4.5, (name, foreground, background)

    # The document tools precede the mode switch; generation follows the document.
    page.locator("#notes-edit-cancel").focus()
    for button in ("view", "rich", "write"):
        page.keyboard.press("Tab")
        focused = page.locator(f"#notes-mode-{button}")
        expect(focused).to_be_focused()
        ring = focused.evaluate(
            "element => [getComputedStyle(element).outlineStyle, "
            "parseFloat(getComputedStyle(element).outlineWidth)]"
        )
        assert ring[0] == "solid" and ring[1] >= 2
    page.locator("#notes-mode-write").click()
    expect(page.locator("#notes-markdown")).to_be_focused()
    ring = page.locator("#notes-markdown").evaluate(
        "element => [getComputedStyle(element).outlineStyle, "
        "parseFloat(getComputedStyle(element).outlineWidth)]"
    )
    assert ring[0] == "solid" and ring[1] >= 2

    durations = (
        "() => ['#notes-mode-view', '#notes-mode-write', '#notes-save', '#notes-copy', "
        "'#notes-export'].map(selector => "
        "parseFloat(getComputedStyle(document.querySelector(selector)).transitionDuration))"
    )
    page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
    assert max(page.evaluate(durations)) <= 0.001
    page.emulate_media(color_scheme=scheme, reduced_motion="no-preference")
    normal = page.evaluate(durations)
    assert normal[:2] == [0, 0]  # Xem and Viết switch instantly whatever the preference
    assert min(normal[3:]) > 0.001  # the original copy and export icons still animate


@pytest.mark.parametrize(
    ("requested_effort", "effort_label", "admitted_effort"),
    [("medium", "Vừa", "medium"), ("default", "Mặc định", "high")],
)
@pytest.mark.usefixtures("fake_vendor_notes_provider")
def test_verify_uses_and_discloses_the_frozen_destination(
    live, signed_in, requested_effort, effort_label, admitted_effort
):
    _context, page = signed_in()
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    preferences_url = f"{live.url}/api/notes-preferences"
    preferences = page.request.get(preferences_url).json()
    requests = []

    def intercept(route):
        requests.append(route.request.post_data)
        if len(requests) == 1:
            route.fulfill(status=503, json={"detail": {"code": "temporary_failure"}})
        else:
            route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", intercept)
    _open_saved_notes(live, page, meeting_id)
    start = page.locator("#notes-start")
    start.click()
    page.locator("#notes-regenerate-provider").select_option("antigravity")
    page.locator("#notes-regenerate-model").select_option(ANTIGRAVITY_MODEL)
    page.locator("#notes-regenerate-effort").select_option(requested_effort)
    page.locator("#notes-regenerate-submit").click()
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    page.locator("#notes-regenerate-cancel").click()

    expect(start).to_have_text("Xác minh yêu cầu notes")
    expect(start).to_have_attribute("aria-describedby", "notes-run-plan notes-status")
    plan = page.locator("#notes-run-plan")
    expect(plan).to_be_visible()
    expect(plan).to_contain_text(
        f"Google Antigravity · Gemini 3 Pro High · mức suy nghĩ {effort_label}"
    )
    page.locator("#notes-change").click()
    provider = page.locator("#notes-provider")
    model = page.locator("#notes-model")
    expect(provider).to_have_value("antigravity")
    expect(provider).to_be_disabled()
    expect(model).to_have_value(ANTIGRAVITY_MODEL)
    expect(model).to_be_disabled()
    expect(page.locator("#notes-content-policy")).to_contain_text("gửi tới Google Antigravity")

    with page.expect_response(lambda response: response.url.endswith("/notes-runs")) as response:
        start.click()
    assert response.value.status == 202
    assert len(requests) == 2 and requests[0] == requests[1]
    body = json.loads(requests[1])
    assert body["run"]["provider"] == "antigravity"
    assert body["run"]["model"] == ANTIGRAVITY_MODEL
    assert body["run"]["reasoning_effort"] == requested_effort
    assert page.request.get(preferences_url).json() == preferences
    task = response.value.json()
    assert task["provider"] == "antigravity"
    assert task["reasoning_effort"] == admitted_effort
    _complete_fake_task(live.settings.data_dir, task["id"], body["request_id"], _markdown(snapshot))
    expect(start).to_be_enabled(timeout=10_000)
    start.click()
    expect(page.locator("#notes-regenerate-provider")).to_have_value("chatgpt")


@pytest.mark.parametrize("locale", ["vi", "en"])
def test_restored_run_dialog_shows_the_frozen_recipe_if_verify_cannot_confirm(
    live, signed_in, locale
):
    _context, page = signed_in(viewport={"width": 390, "height": 900})
    page.add_init_script(f"localStorage.setItem('asr-studio:lang', '{locale}')")
    verify_label = "Verify notes request" if locale == "en" else "Xác minh yêu cầu notes"
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    body = {
        "request_id": "fixture-restored-run",
        "expected_generation_id": snapshot["generation_id"],
        "expected_source_version": 1,
        "expected_metadata_version": 1,
        "output_language": "vi",
        "run": {
            "provider": "chatgpt",
            "model": "previous-model",
            "reasoning_effort": "high",
            "generation_id": snapshot["generation_id"],
            "source_version": 1,
        },
    }

    page.evaluate(
        """([id, body]) => sessionStorage.setItem(
            `asr-studio:notes-request-v2:${id}`,
            JSON.stringify(body)
        )""",
        [meeting_id, body],
    )
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes-runs",
        lambda route: route.fulfill(
            status=503, json={"detail": {"code": "temporary_failure", "message": "Try again."}}
        ),
    )
    _open_saved_notes(live, page, meeting_id)
    start = page.locator("#notes-start")
    expect(start).to_have_text(verify_label)
    with page.expect_request(lambda request: request.url.endswith("/notes-runs")) as sent:
        start.click()
    assert sent.value.post_data_json == body
    expect(page.locator("#notes-regenerate-error")).to_be_visible()
    for name, value in (
        ("provider", body["run"]["provider"]),
        ("model", body["run"]["model"]),
        ("effort", body["run"]["reasoning_effort"]),
        ("source", body["run"]["generation_id"]),
        ("language", body["output_language"]),
    ):
        field = page.locator(f"#notes-regenerate-{name}")
        expect(field).to_have_value(value)
        expect(field).to_be_disabled()
    expect(page.locator("#notes-regenerate-submit")).to_have_text(verify_label)
    effort_label = (
        "Reasoning level High — not available for this model"
        if locale == "en"
        else "Mức suy nghĩ Cao — không khả dụng với model này"
    )
    expect(page.locator("#notes-regenerate-effort option:checked")).to_have_text(effort_label)


@pytest.mark.usefixtures("fake_vendor_notes_provider")
@pytest.mark.parametrize("context_at_refusal", ["notes", "transcript", "transcript_disconnected"])
def test_restored_run_keeps_disconnected_destination_after_definitive_verify_rejection(
    live, signed_in, context_at_refusal
):
    _context, page = signed_in()
    mock_providers(page, live, antigravity="disconnected")
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(live.settings.data_dir)
    generation_id = snapshot["generation_id"]
    preferences_url = f"{live.url}/api/notes-preferences"
    preferences = page.request.get(preferences_url).json()
    assert preferences["provider"] == "chatgpt"
    body = {
        "request_id": "fixture-restored-antigravity-run",
        "expected_generation_id": generation_id,
        "expected_source_version": 1,
        "expected_metadata_version": 1,
        "output_language": "vi",
        "run": {
            "provider": "antigravity",
            "model": ANTIGRAVITY_MODEL,
            "reasoning_effort": "medium",
            "generation_id": generation_id,
            "source_version": 1,
        },
    }
    page.evaluate(
        """([id, body]) => sessionStorage.setItem(
            `asr-studio:notes-request-v2:${id}`, JSON.stringify(body)
        )""",
        [meeting_id, body],
    )
    runs_url = f"{live.url}/api/meetings/{meeting_id}/notes-runs"
    raw_requests = []
    preference_writes = []
    allow_submission = False
    held_refusals = []

    def intercept_run(route):
        raw_requests.append(route.request.post_data_buffer)
        if len(raw_requests) == 1:
            if context_at_refusal == "notes":
                route.fulfill(status=503, json={"detail": {"code": "temporary_failure"}})
            else:
                held_refusals.append(route)
        elif allow_submission:
            route.continue_()
        elif len(raw_requests) == 2 and context_at_refusal == "notes":
            route.fulfill(status=409, json={"detail": {"code": "provider_disconnected"}})
        else:
            route.abort()

    def track_preferences(request):
        if request.method == "PUT" and request.url == preferences_url:
            preference_writes.append(request.url)

    page.route(runs_url, intercept_run)
    page.on("request", track_preferences)
    _open_saved_notes(live, page, meeting_id)
    assert raw_requests == []
    start = page.locator("#notes-start")
    dialog = page.locator("#notes-regenerate-dialog")
    submit = page.locator("#notes-regenerate-submit")
    provider = page.locator("#notes-regenerate-provider")
    model = page.locator("#notes-regenerate-model")
    effort = page.locator("#notes-regenerate-effort")
    language = page.locator("#notes-regenerate-language")
    error = page.locator("#notes-regenerate-error")
    reload_choices = page.locator("#notes-regenerate-reload")
    notes_tab = page.locator("#meeting-tab-notes")
    transcript_tab = page.locator("#meeting-tab-transcript")
    verify = "Xác minh yêu cầu notes"
    expect(start).to_have_text(verify)
    if context_at_refusal == "notes":
        with page.expect_response(runs_url, timeout=5_000) as first:
            start.click()
        assert first.value.status == 503
        expect(dialog).to_be_visible()
        expect(error).to_be_visible()
        expect(page.locator("#notes-regenerate-status")).to_contain_text("đã được giữ nguyên")
        expect(submit).to_have_text(verify)
        expect(submit).to_be_focused()
        expect(provider).to_have_value("antigravity")
        expect(provider).to_be_disabled()
        expect(reload_choices).to_be_enabled()

        with (
            page.expect_response(runs_url, timeout=5_000) as refusal,
            page.expect_response(f"{live.url}/api/providers/chatgpt/models", timeout=5_000),
        ):
            submit.click()
        assert len(raw_requests) == 2
        assert raw_requests[0] == raw_requests[1]
        expect(page.locator("#notes-regenerate-cancel")).to_be_focused()
    else:
        with page.expect_request(runs_url, timeout=5_000):
            start.click()
        transcript_tab.click()
        expect(transcript_tab).to_be_focused()
        assert len(held_refusals) == 1
        all_disconnected = context_at_refusal == "transcript_disconnected"
        if all_disconnected:
            mock_providers(page, live, chatgpt="disconnected", antigravity="disconnected")
        with page.expect_response(runs_url, timeout=5_000) as refusal:
            held_refusals.pop().fulfill(
                status=409, json={"detail": {"code": "provider_disconnected"}}
            )
        expect(page.locator("#notes-error")).to_contain_text("Google Antigravity")
        expect(dialog).to_be_hidden()
        expect(transcript_tab).to_be_focused()
        notes_tab.click()
        expect(dialog).to_be_hidden()
        if all_disconnected:
            expect(start).to_have_text("Kết nối ChatGPT để tạo notes")
            review_correction = page.locator("#notes-review-correction")
            expect(review_correction).to_have_text("Xem lại lựa chọn bị từ chối")
            review_correction.click()
            expect(provider).to_have_value("antigravity")
            expect(provider.locator("option:checked")).to_be_disabled()
            expect(submit).to_be_disabled()
            assert len(raw_requests) == 1
            assert preference_writes == []
            expect(page.locator("#notes-regenerate-cancel")).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            expect(review_correction).to_be_hidden()
            expect(start).to_be_focused()
            expect(start).to_have_text("Kết nối ChatGPT để tạo notes")
            return
        start.click()
        expect(provider).to_be_focused()
    assert refusal.value.status == 409
    assert refusal.value.json()["detail"]["code"] == "provider_disconnected"
    assert json.loads(raw_requests[0]) == body
    requests_before_correction = len(raw_requests)
    expect(reload_choices).to_be_enabled()
    expect(dialog).to_be_visible()
    expect(error).to_contain_text("Google Antigravity")
    expect(provider).to_be_enabled()
    expect(provider).to_have_value("antigravity")
    expect(model).to_have_value(ANTIGRAVITY_MODEL)
    expect(effort).to_have_value("medium")
    expect(page.locator("#notes-regenerate-source")).to_have_value(generation_id)
    expect(language).to_have_value("vi")
    expect(submit).to_have_text("Tạo lại notes")
    expect(submit).to_be_disabled()

    provider.select_option("chatgpt")
    expect(submit).to_be_enabled()
    assert len(raw_requests) == requests_before_correction
    allow_submission = True
    with page.expect_response(runs_url, timeout=5_000) as admitted:
        submit.click()
    assert admitted.value.status == 202
    assert len(raw_requests) == requests_before_correction + 1
    corrected = admitted.value.request.post_data_json
    task = admitted.value.json()
    assert corrected["request_id"] != body["request_id"]
    assert corrected["run"]["provider"] == task["provider"] == "chatgpt"
    assert task["request_id"] == corrected["request_id"]
    assert task["output_language"] == corrected["output_language"] == "vi"
    expect(dialog).to_be_hidden()
    expect(page.locator("#notes-cancel")).to_be_focused()
    assert preference_writes == []
    assert page.request.get(preferences_url).json() == preferences


def test_completed_restored_recipe_keeps_the_meeting_language_for_the_next_run(live, signed_in):
    _context, page = signed_in(viewport={"width": 390, "height": 900})
    data_dir = live.settings.data_dir
    meeting_id, _revision, snapshot, _segments = _seed_saved_notes(data_dir)
    request_id = "fixture-restored-language"
    body = {
        "request_id": request_id,
        "expected_generation_id": snapshot["generation_id"],
        "expected_source_version": 1,
        "expected_metadata_version": 1,
        "output_language": "vi",
        "run": {
            "provider": "chatgpt",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
            "generation_id": snapshot["generation_id"],
            "source_version": 1,
        },
    }
    admitted = accept_notes_task(
        data_dir,
        provider=BrowserExplicitStub(),
        meeting_id=meeting_id,
        worker_ready=True,
        **body,
    )
    _complete_fake_task(data_dir, admitted["id"], request_id, _markdown(snapshot))

    page.evaluate(
        """([id, body]) => {
            sessionStorage.setItem(`asr-studio:notes-request-v2:${id}`, JSON.stringify(body));
            localStorage.setItem(`asr-studio:notes-output-language:${id}`, 'en');
        }""",
        [meeting_id, body],
    )
    _open_saved_notes(live, page, meeting_id)
    expect(page.locator("#notes-start")).to_contain_text("English")
    expect(page.locator("#notes-start")).not_to_contain_text("Xác minh yêu cầu notes")
    page.locator("#notes-start").click()
    expect(page.locator("#notes-regenerate-language")).to_have_value("en")
    expect(page.locator("#notes-regenerate-submit")).to_be_enabled()
