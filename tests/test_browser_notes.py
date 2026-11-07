from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from notes_helpers import StubNotesProvider, seed_completed_transcript
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as browser_signed_in  # noqa: F401
from test_browser_notes_provider import mock_providers

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
from asr_studio.storage import database_path
from asr_studio.tasks import claim_next_task, transcription_state

pytestmark = pytest.mark.browser


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


def test_notes_recovers_when_transcription_submission_finishes_after_navigation(live, signed_in):
    _context, page = signed_in()
    first_id, _generation, _segments = seed_completed_transcript(live.settings.data_dir)
    other_id, _other_generation, _other_segments = seed_completed_transcript(
        live.settings.data_dir, title="Other meeting"
    )
    held_requests = []
    page.route(f"{live.url}/api/tasks", lambda route: held_requests.append(route))
    page.goto(f"{live.url}/meetings/{first_id}")
    expect(page.locator("#notes-start")).to_be_enabled()
    page.locator("#start-transcription").click()
    expect(page.locator("#notes-start")).to_be_disabled()
    page.locator(f'a[href="/meetings/{other_id}"]').first.click()
    expect(page.locator("#meeting-title")).to_have_text("Other meeting")
    expect(page.locator("#notes-start")).to_be_disabled()
    assert len(held_requests) == 1
    held_requests.pop().fulfill(
        status=409, json={"detail": {"code": "system_busy", "message": "Processing is busy."}}
    )
    expect(page.locator("#notes-start")).to_be_enabled()


@pytest.mark.parametrize("cached", [True, False])
def test_notes_waits_for_transcription_request_and_active_task(live, signed_in, cached):
    _context, page = signed_in()
    meeting_id, _generation_id, _segments = seed_completed_transcript(live.settings.data_dir)
    if not cached:
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
    else:
        expect(page.locator("#notes-status")).to_contain_text("Cần transcript hoàn chỉnh")
    page.locator("#start-transcription").click()
    expect(page.locator("#notes-start")).to_be_disabled()
    expect(page.locator("#notes-status")).to_contain_text("Chờ tác vụ kết thúc")
    assert len(held_requests) == 1
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
        return original_accept(data_dir, provider=StubNotesProvider(), **kwargs)

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


def _complete_fake_task(data_dir: Path, task_id: str, request_id: str, markdown: str):
    owner_id = f"notes-browser-worker-{request_id}"
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed is not None and claimed["id"] == task_id
    revision = publish_notes_revision(
        data_dir,
        task_id,
        owner_id=owner_id,
        markdown=markdown,
        elapsed_seconds=0.1,
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
        "[foreign](https://attacker.invalid/collect)\n"
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
    expect(buttons).to_have_count(2)
    expect(buttons.nth(0)).to_have_accessible_name("Xem")
    expect(buttons.nth(1)).to_have_accessible_name("Viết")
    for index in range(2):
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
    write, dot = page.locator("#notes-mode-write"), page.locator("#notes-unsaved-dot")
    line = page.get_by_text("Đang xem bản nháp chưa lưu", exact=True)

    expect(write).to_have_attribute("aria-label", "Viết")
    expect(dot).to_be_hidden()
    expect(line).to_be_hidden()

    write.click()
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
    # The status lines sit right under the toolbar, next to the buttons that cause them.
    assert page.evaluate(
        """() => {
          const toolbar = document.querySelector("#notes-toolbar").getBoundingClientRect();
          const frame = document.querySelector(".notes-frame").getBoundingClientRect();
          return ["#notes-draft-status", "#notes-copy-export-status"].map(selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return box.top >= toolbar.bottom && box.bottom <= frame.top;
          });
        }"""
    ) == [True, True]
    expect(save).to_have_accessible_name("Lưu bản nháp")
    expect(copy).to_have_accessible_name("Sao chép bản đã lưu")
    expect(export).to_have_accessible_name("Tải Markdown đã lưu")

    page.locator("#notes-markdown").fill("# Draft")
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
    links = page.locator("#notes-preview a")
    expect(links).to_have_count(1)
    expect(page.locator("#notes-source-view")).to_be_hidden()
    links.click()
    expect(page.locator("#notes-source-view")).to_be_visible()
    expect(page.locator("#notes-source-text")).to_have_text(snapshot["segments"][0]["text"])
    expect(page.locator("#notes-source-time")).to_have_text("00:00:01")
    assert parse_qs(urlsplit(page.url).query) == {
        "notes_source": [snapshot["id"]],
        "segment": [segment_ids[0]],
    }
    expect(page.locator("#notes-preview")).to_contain_text(
        "[elsewhere](https://attacker.invalid/x)"
    )


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

    # The switch sits above the frame, so the notes action's last control comes right before it.
    page.locator("#notes-change").focus()
    for button in ("view", "write"):
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
    assert min(normal[2:]) > 0.001  # the toolbar buttons do animate, so the check above is real
