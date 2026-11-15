from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_recording_failure_and_success_preserve_keyboard_focus(
    live, signed_in, tmp_path, language, theme
):
    context, page = signed_in(color_scheme=theme, viewport={"width": 1280, "height": 900})
    context.add_init_script(f"localStorage.setItem('asr-studio:lang', '{language}')")
    meetings = {
        seed_meeting(live.settings.data_dir, "Retry recording", "2026-10-14"): "retry",
        seed_meeting(live.settings.data_dir, "Refresh recording", "2026-10-13"): "refresh",
    }
    reads = dict.fromkeys(meetings, 0)
    posts = dict.fromkeys(meetings, 0)

    def route_meeting(route):
        match = re.search(
            r"/api/meetings/([0-9a-f]{32})(?:/recording/retry)?/?$", route.request.url
        )
        assert match is not None
        meeting_id = match.group(1)
        if route.request.method == "POST":
            posts[meeting_id] += 1
            if posts[meeting_id] == 1:
                route.fulfill(status=503, json={"detail": "retry unavailable"})
            else:
                route.fulfill(
                    status=202,
                    json={
                        "meeting_id": meeting_id,
                        "recording_state": "finalizing",
                        "retry_requested": True,
                    },
                )
            return
        reads[meeting_id] += 1
        if meetings[meeting_id] == "refresh" and reads[meeting_id] == 2:
            route.fulfill(status=503, json={"detail": "refresh unavailable"})
            return
        response = route.fetch()
        payload = response.json()
        if "recording" not in payload:
            route.fulfill(response=response)
            return
        ready = posts[meeting_id] == 2 or reads[meeting_id] == 3
        state = "ready" if ready else "failed" if meetings[meeting_id] == "retry" else "receiving"
        payload["recording"].update(
            source="extension",
            state=state,
            duration_seconds=12 if ready else None,
            audio_url=f"/api/meetings/{meeting_id}/audio" if ready else None,
        )
        if state == "failed":
            payload["recording"]["error"] = {
                "code": "media_processing_failed",
                "message": "private",
            }
        else:
            payload["recording"].pop("error", None)
        route.fulfill(response=response, json=payload)

    page.route(re.compile(r"/api/meetings/[0-9a-f]{32}(?:/recording/retry)?/?$"), route_meeting)
    for meeting_id, action in meetings.items():
        page.goto(f"{live.url}/meetings/{meeting_id}#transcript", wait_until="domcontentloaded")
        button = page.locator("#recording-status-retry")
        expect(button).to_have_text(
            ("Retry processing" if action == "retry" else "Reload status")
            if language == "en"
            else ("Thử xử lý lại" if action == "retry" else "Tải lại trạng thái")
        )
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        button.focus()
        page.keyboard.press("Enter")
        error = (
            (
                "The server could not complete the request"
                if action == "retry"
                else "Could not read the recording status"
            )
            if language == "en"
            else (
                "Máy chủ chưa thể hoàn tất yêu cầu"
                if action == "retry"
                else "Không thể đọc trạng thái bản ghi"
            )
        )
        expect(page.locator("#recording-status")).to_contain_text(error)
        expect(button).to_be_enabled()
        assert page.evaluate("document.activeElement.id") == "recording-status-retry"
        page.screenshot(path=str(tmp_path / f"{language}-{theme}-{action}-failed.png"))
        page.keyboard.press("Enter")
        expect(page.locator("#status-recording")).to_contain_text(
            "Recording · Saved" if language == "en" else "Bản ghi · Đã lưu"
        )
        expect(page.locator("#recording-status")).to_be_hidden()
        assert page.evaluate("document.activeElement.id") == "meeting-title"
        assert page.locator("#meeting-audio").evaluate("audio => audio.controls") is True
