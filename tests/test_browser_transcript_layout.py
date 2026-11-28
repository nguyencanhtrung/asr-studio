from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from seeding import seed_meeting
from test_browser_history import (
    READING_SEGMENTS,
    READING_SPEAKERS,
    _publish_browser_generation,
)
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in
from test_browser_notes import _write_test_recording

pytestmark = pytest.mark.browser


def _open_transcript(live, page, tmp_path, *, title="Transcript layout", language="vi"):
    meeting_id = seed_meeting(
        live.settings.data_dir,
        title,
        "2026-10-14",
        duration_seconds=30,
    )
    _publish_browser_generation(
        live,
        meeting_id,
        segments=READING_SEGMENTS,
        speakers=READING_SPEAKERS,
    )
    _write_test_recording(live.settings.data_dir, meeting_id, tmp_path)
    page.evaluate("language => localStorage.setItem('asr-studio:lang', language)", language)
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript", wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    expect(page.locator("#transcript-generation")).to_be_visible(timeout=10_000)
    expect(page.locator("#transcript-search")).to_be_visible()
    page.wait_for_function(
        "() => document.querySelector('#meeting-audio').readyState >= 1",
        timeout=10_000,
    )
    return meeting_id


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("language", ["vi", "en"])
def test_transcript_layout_and_real_audio_transport(
    live, signed_in, tmp_path, scheme, width, language
):
    _context, page = signed_in(
        color_scheme=scheme,
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
    )
    meeting_id = _open_transcript(
        live,
        page,
        tmp_path,
        title=f"Transcript {scheme} {width} {language}",
        language=language,
    )

    expect(page.locator("#audio-transport")).to_be_visible()
    expect(page.locator("#audio-play-toggle")).to_be_visible()
    expect(page.locator("#audio-seek")).to_be_visible()
    expect(page.locator("#audio-rate")).to_be_visible()
    expect(page.locator("#audio-volume")).to_be_visible()
    expect(page.locator("#transcript-review-only")).to_be_visible()
    expect(page.locator("#transcript-review-prev")).to_be_visible()
    expect(page.locator("#transcript-review-next")).to_be_visible()
    accessible_names = {
        "#transcript-review-prev": "Đoạn cần kiểm tra trước"
        if language == "vi"
        else "Previous segment needing review",
        "#transcript-review-next": "Đoạn cần kiểm tra tiếp"
        if language == "vi"
        else "Next segment needing review",
    }
    for selector, name in accessible_names.items():
        control = page.locator(selector)
        expect(control).to_have_accessible_name(name)
        expect(control).to_have_attribute("title", name)
        expect(control.locator("svg")).to_have_attribute("aria-hidden", "true")
        compact = control.evaluate(
            """button => {
              const icon = button.querySelector('svg').getBoundingClientRect();
              const label = document.createRange();
              label.selectNodeContents(button.querySelector('span'));
              return {width: icon.width, height: icon.height,
                labelLines: label.getClientRects().length};
            }"""
        )
        assert 0 < compact["width"] <= 24, compact
        assert 0 < compact["height"] <= 24, compact
        assert compact["labelLines"] == 1, compact

    ownership = page.evaluate(
        """() => ({
          audioMeetingId: document.querySelector('#meeting-audio').dataset.meetingId,
          audioKey: document.querySelector('#meeting-audio').dataset.audioKey,
          generationId: document.querySelector(
            '#transcript-segments [data-generation-id]'
          )?.dataset.generationId,
          renderKey: document.querySelector('#transcript-generation').dataset.renderKey,
        })"""
    )
    assert ownership["audioMeetingId"] == meeting_id, ownership
    assert ownership["audioKey"].startswith(f"{meeting_id}:"), ownership
    assert ownership["generationId"], ownership
    assert ownership["renderKey"].startswith(f"{ownership['generationId']}:")

    geometry = page.evaluate(
        """() => {
          const box = selector => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
              right: rect.right, bottom: rect.bottom};
          };
          const panel = document.querySelector('#transcription-panel');
          const player = document.querySelector('#meeting-panel-transcript .player-bar');
          const audio = document.querySelector('#meeting-audio');
          return {
            reading: box('.transcript-reading-column'),
            context: box('#transcript-context'),
            player: box('#meeting-panel-transcript .player-bar'),
            playerPosition: getComputedStyle(player).position,
            panelBackground: getComputedStyle(panel).backgroundColor,
            panelBorder: getComputedStyle(panel).borderTopStyle,
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            autoplay: audio.autoplay,
            paused: audio.paused,
          };
        }"""
    )
    assert geometry["overflow"] <= 0, geometry
    assert geometry["playerPosition"] == "fixed", geometry
    assert geometry["panelBackground"] in {"rgba(0, 0, 0, 0)", "transparent"}, geometry
    assert geometry["panelBorder"] == "none", geometry
    assert geometry["autoplay"] is False and geometry["paused"] is True, geometry
    assert geometry["player"]["bottom"] <= page.viewport_size["height"] + 1, geometry
    if width >= 1100:
        toolbar = page.evaluate(
            """() => {
              const box = selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
              };
              const controls = [
                box('#transcript-search'),
                box('#transcript-filters .transcript-review-filter'),
                box('#transcript-review-prev'),
                box('#transcript-review-next'),
              ];
              return {
                controls,
                topDelta: Math.max(...controls.map(item => item.top))
                  - Math.min(...controls.map(item => item.top)),
                rowBottom: Math.max(...controls.map(item => item.bottom)),
                status: box('#transcript-filter-status'),
                overflow: document.documentElement.scrollWidth - innerWidth,
              };
            }"""
        )
        assert toolbar["topDelta"] <= 8, toolbar
        assert toolbar["status"]["top"] >= toolbar["rowBottom"] - 1, toolbar
        assert toolbar["overflow"] <= 0, toolbar
        expect(page.locator("#transcript-filter-status")).to_contain_text("3/3")

        # A real query reveals the clear action; it remains reachable and inside the reading
        # column rather than being clipped by the one-row default navigation treatment.
        page.locator("#transcript-search").fill("Costs look high")
        clear = page.locator("#transcript-clear-filters")
        expect(clear).to_be_visible()
        assert clear.evaluate(
            """element => {
              const box = element.getBoundingClientRect();
              const column = document.querySelector('.transcript-reading-column')
                .getBoundingClientRect();
              return box.left >= column.left && box.right <= column.right
                && box.top >= 0 && box.bottom <= innerHeight;
            }"""
        )
        page.locator("#transcript-clear-filters").click()
        assert geometry["context"]["x"] >= geometry["reading"]["right"] - 1, geometry
        assert geometry["reading"]["width"] / geometry["context"]["width"] >= 1.6, geometry
        assert page.locator("#transcript-context").evaluate("element => element.open")
    else:
        assert geometry["context"]["width"] <= width, geometry
        page.locator("#transcript-context > summary").click()
        expect(page.locator("#speaker-catalog-title")).to_be_visible()
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")

    audio = page.locator("#meeting-audio")
    duration = audio.evaluate("element => element.duration")
    assert duration > 0, duration

    page.locator("#audio-play-toggle").click()
    page.wait_for_function("() => !document.querySelector('#meeting-audio').paused")
    expect(page.locator("#audio-play-toggle")).to_have_attribute("aria-pressed", "true")
    page.locator("#audio-play-toggle").click()
    page.wait_for_function("() => document.querySelector('#meeting-audio').paused")
    expect(page.locator("#audio-play-toggle")).to_have_attribute("aria-pressed", "false")

    page.locator("#audio-seek").evaluate(
        """(element) => {
          element.value = '2';
          element.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    page.wait_for_function("() => document.querySelector('#meeting-audio').currentTime >= 1.8")
    page.locator("#audio-rate").select_option("1.5")
    page.locator("#audio-volume").evaluate(
        """(element) => {
          element.value = '0.35';
          element.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    assert audio.evaluate("element => element.playbackRate") == 1.5
    assert abs(audio.evaluate("element => element.volume") - 0.35) < 0.02
    assert audio.evaluate("element => element.dataset.meetingId") == meeting_id
    assert audio.evaluate("element => element.dataset.audioKey").startswith(f"{meeting_id}:")
    screenshot_dir = os.environ.get("ASR_TRANSCRIPT_SCREENSHOT_DIR")
    if screenshot_dir:
        destination = Path(screenshot_dir)
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(destination / f"transcript-{language}-{scheme}-{width}.png"),
            full_page=False,
        )


def test_transcript_edit_search_and_speaker_controls_remain_usable(live, signed_in, tmp_path):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    _open_transcript(live, page, tmp_path, title="Transcript editing")

    first = page.locator(".transcript-segment").first
    original = first.locator(".segment-text").inner_text()
    first.locator(".segment-edit-button").click()
    editor = first.locator(".segment-text-editor")
    expect(editor).to_be_focused()
    editor.fill("Draft that is cancelled")
    editor.press("Escape")
    expect(first.locator(".segment-text")).to_have_text(original)

    page.locator("#transcript-search").fill("Costs look high")
    expect(page.locator(".transcript-segment").nth(0)).to_be_hidden()
    expect(page.locator(".transcript-segment").nth(1)).to_be_visible()
    page.locator("#transcript-clear-filters").click()
    expect(page.locator(".transcript-segment").first).to_be_visible()

    speaker = page.locator("#speaker-catalog-list > li").first
    speaker.locator(".speaker-chip").click()
    name_input = speaker.locator("input")
    expect(name_input).to_be_focused()
    name_input.fill("Renamed then cancelled")
    name_input.press("Escape")
    expect(speaker.locator(".speaker-chip")).to_be_visible()


def test_unplayable_recording_keeps_explicit_error_and_no_fake_transport(live, signed_in):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    meeting_id = seed_meeting(
        live.settings.data_dir,
        "Transcript missing recording",
        "2026-10-14",
        duration_seconds=30,
    )
    _publish_browser_generation(
        live,
        meeting_id,
        segments=READING_SEGMENTS,
        speakers=READING_SPEAKERS,
    )

    def fail_recording(route):
        response = route.fetch()
        payload = response.json()
        payload["recording"] = {
            **payload["recording"],
            "state": "failed",
            "duration_seconds": None,
            "audio_url": None,
            "error": {"code": "media_processing_failed", "message": "private detail"},
        }
        route.fulfill(response=response, json=payload)

    page.route(re.compile(rf"/api/meetings/{meeting_id}/?$"), fail_recording)
    page.goto(f"{live.url}/meetings/{meeting_id}#transcript", wait_until="networkidle")
    expect(page.locator("#transcript-generation")).to_be_visible(timeout=10_000)
    expect(page.locator("#recording-status")).to_be_visible()
    expect(page.locator("#recording-status")).to_contain_text("media_processing_failed")
    expect(page.locator("#recording-status")).not_to_contain_text("private detail")
    expect(page.locator("#audio-transport")).to_be_hidden()
    expect(page.locator("#meeting-panel-transcript .player-bar")).to_be_hidden()
    assert page.locator("#meeting-audio").evaluate(
        """audio => ({
          controls: audio.controls,
          src: audio.currentSrc,
          autoplay: audio.autoplay,
          meetingId: audio.dataset.meetingId,
          audioKey: audio.dataset.audioKey,
        })"""
    ) == {
        "controls": False,
        "src": "",
        "autoplay": False,
        "meetingId": meeting_id,
        "audioKey": f"{meeting_id}:unavailable",
    }


@pytest.mark.parametrize("width", [320, 700, 800, 1440])
def test_audio_native_controls_and_muted_volume_keep_the_same_recording(
    live, signed_in, tmp_path, width
):
    _context, page = signed_in(viewport={"width": width, "height": 844})
    meeting_id = _open_transcript(live, page, tmp_path)
    audio = page.locator("#meeting-audio")
    key = audio.get_attribute("data-audio-key")
    audio.evaluate(
        "audio => { window.__originalAudio = audio; audio.currentTime = 2; audio.muted = true; }"
    )
    expect(page.locator("#audio-volume")).to_have_value("0")
    page.locator("#audio-volume").evaluate(
        "e => { e.value = '0.4'; e.dispatchEvent(new Event('input', {bubbles:true})); }"
    )
    assert audio.evaluate("audio => !audio.muted && Math.abs(audio.volume - 0.4) < 0.01")
    page.locator("#audio-native-options > summary").click()
    expect(audio).to_be_visible()
    expect(page.locator("#audio-transport")).to_be_hidden()
    assert audio.evaluate(
        "audio => audio === window.__originalAudio && audio.controls && !audio.autoplay"
    )
    assert audio.get_attribute("aria-hidden") is None
    assert audio.get_attribute("data-audio-key") == key
    assert audio.get_attribute("data-meeting-id") == meeting_id
    page.locator("#audio-native-options > summary").click()
    expect(page.locator("#audio-transport")).to_be_visible()
    assert audio.evaluate(
        "audio => audio === window.__originalAudio && audio.currentTime >= 1.9 && audio.paused"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for selector in ("#audio-play-toggle", "#audio-seek", "#audio-rate", "#audio-volume"):
        assert page.locator(selector).evaluate(
            """e => {
              const r = e.getBoundingClientRect();
              return r.left >= 0 && r.right <= innerWidth && r.width > 0;
            }"""
        )


def test_late_play_error_cannot_mark_a_different_meeting(live, signed_in, tmp_path):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    first = _open_transcript(live, page, tmp_path, title="First recording")
    page.locator("#meeting-audio").evaluate(
        """audio => {
          audio.play = () => new Promise((resolve, reject) => { window.__rejectOldPlay = reject; });
        }"""
    )
    page.locator("#audio-play-toggle").click()
    other = seed_meeting(live.settings.data_dir, "Other meeting", "2026-10-14")
    _write_test_recording(live.settings.data_dir, other, tmp_path)
    page.evaluate(
        """id => {
          history.pushState({}, '', '/meetings/' + id + '#transcript');
          dispatchEvent(new PopStateEvent('popstate'));
        }""",
        other,
    )
    expect(page.locator("#meeting-title")).to_have_text("Other meeting")
    assert page.locator("#meeting-audio").get_attribute("data-meeting-id") == other
    assert first != other
    page.wait_for_function("() => document.querySelector('#meeting-audio').readyState >= 1")
    expect(page.locator("#audio-transport-error")).to_be_hidden()
    page.evaluate(
        "window.__rejectOldPlay(new DOMException('old recording failed', 'NotSupportedError'))"
    )
    expect(page.locator("#audio-transport-error")).to_be_hidden()
    assert page.locator("#meeting-audio").get_attribute("data-audio-key").startswith(other + ":")


@pytest.mark.parametrize("name", ["AbortError", "NotSupportedError"])
def test_current_play_failure_distinguishes_user_abort_from_media_error(
    live, signed_in, tmp_path, name
):
    _context, page = signed_in()
    _open_transcript(live, page, tmp_path)
    page.locator("#meeting-audio").evaluate(
        """(audio, name) => {
          audio.play = () => Promise.reject(new DOMException('test playback failure', name));
        }""",
        name,
    )
    page.locator("#audio-play-toggle").click()
    expect(page.locator("#audio-play-toggle")).to_be_focused()
    error = page.locator("#audio-transport-error")
    if name == "AbortError":
        expect(error).to_be_hidden()
    else:
        expect(error).to_be_visible()
        expect(error).to_have_text("Không phát được bản ghi này.")
    assert page.locator("#meeting-audio").evaluate("audio => audio.paused && !audio.autoplay")


@pytest.mark.parametrize("play_result", ["reject", "resolve"])
@pytest.mark.parametrize("language", ["vi", "en"])
def test_audio_error_is_not_announced_as_playing(live, signed_in, tmp_path, language, play_result):
    _context, page = signed_in(viewport={"width": 1440, "height": 900})
    _open_transcript(live, page, tmp_path, language=language)
    source = page.locator("#meeting-audio").get_attribute("src")
    key = page.locator("#meeting-audio").get_attribute("data-audio-key")
    # Firefox can report a real output failure with paused=false. Reproduce those
    # observed media facts without playing sound or changing the saved recording.
    page.evaluate(
        """outcome => {
          const audio = document.querySelector('#meeting-audio');
          Object.defineProperty(audio, 'paused', {configurable: true, value: false});
          Object.defineProperty(audio, 'error', {configurable: true,
            value: {code: 3, message: 'Fixture audio output unavailable'}});
          window.audioRetryProbe = {play: 0, pause: 0};
          audio.play = () => {
            window.audioRetryProbe.play += 1;
            return outcome === 'reject'
              ? Promise.reject(new Error('Fixture output still unavailable'))
              : Promise.resolve();
          };
          audio.pause = () => { window.audioRetryProbe.pause += 1; };
          audio.dispatchEvent(new Event('error'));
        }"""
    )
    play = page.locator("#audio-play-toggle")
    expect(play).to_have_attribute("aria-pressed", "false")
    expect(play).to_have_accessible_name("Phát bản ghi" if language == "vi" else "Play recording")
    expect(page.locator("#audio-transport-error")).to_have_text(
        "Không phát được bản ghi này." if language == "vi" else "This recording cannot be played."
    )
    play.click()
    expect(play).to_be_focused()
    page.wait_for_function("() => window.audioRetryProbe.play === 1", timeout=5000)
    assert page.evaluate("window.audioRetryProbe") == {"play": 1, "pause": 0}
    expect(page.locator("#audio-transport-error")).to_be_visible()
    page.locator("#audio-native-options > summary").click()
    expect(page.locator("#meeting-audio")).to_be_visible()
    expect(page.locator("#meeting-audio")).to_have_attribute("src", source)
    expect(page.locator("#meeting-audio")).to_have_attribute("data-audio-key", key)
    expect(play).to_have_attribute("aria-pressed", "false")
