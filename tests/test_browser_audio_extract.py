"""Real-browser tests: a video is cut down to its audio in the browser before upload (#117)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from media_tools import require_media_tools
from playwright.sync_api import expect
from test_browser_create_flow import _meeting_id, _open_dialog
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio.media import normalize_media

pytestmark = pytest.mark.browser

SECONDS = 6
TOLERANCE_SECONDS = 0.15


@pytest.fixture(autouse=True)
def media_tools():
    require_media_tools()


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=120
    )


def make_video(path: Path, *, audio: str | None = "aac", audio_delay: float = 0) -> Path:
    """A small generated video: moving test pattern plus a tone in the named audio codec."""
    video = ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={SECONDS}"]
    # -itsoffset shifts where the audio starts on the timeline (a delayed-start track).
    tone = [
        "-itsoffset",
        str(audio_delay),
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={SECONDS}",
    ]
    codec = {
        "aac": ["-c:a", "aac", "-b:a", "96k"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "96k"],
        "opus": ["-c:a", "libopus", "-b:a", "64k"],
    }
    if audio is None:
        _ffmpeg(*video, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    else:
        video_codec = (
            ["-c:v", "libvpx", "-b:v", "200k"]
            if audio == "opus"
            else ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        )
        _ffmpeg(*video, *tone, *video_codec, *codec[audio], str(path))
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return json.loads(out)


def audio_duration(path: Path) -> float:
    info = probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
    return float(stream.get("duration") or info["format"]["duration"])


def pcm_landmarks(path: Path) -> tuple[float, float]:
    """(first loud sample, total length) in seconds of the decoded audio of `path`."""
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    samples = memoryview(raw).cast("h")
    onset = next(i for i, v in enumerate(samples) if abs(v) > 2000)
    return onset / 8000, len(samples) / 8000


def _create(page, video: Path, *, title="Họp video"):
    """Select the video, create the meeting; return the upload requests the page made."""
    seen = {"post": [], "put": []}

    def on_request(request):
        path = urlsplit(request.url).path
        if request.method == "POST" and path == "/api/uploads":
            seen["post"].append(request.post_data_json)

    page.on("request", on_request)
    # Playwright cannot read a Blob request body; hand the XHR body over from the page.
    page.expose_function("recordPut", lambda data: seen["put"].append(base64.b64decode(data)))
    hook = """
        const send = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function (body) {
          if (body instanceof Blob) {
            const reader = new FileReader();
            reader.onload = () => window.recordPut(String(reader.result).split(",")[1]);
            reader.readAsDataURL(body);
          }
          return send.call(this, body);
        };
        """
    page.add_init_script(hook)  # later documents
    page.evaluate(f"(() => {{{hook}}})()")  # the one already open
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(str(video))
    page.locator("#meeting-name").fill(title)
    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#auto-transcribe").set_checked(False)
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    return seen


@pytest.mark.parametrize(
    ("audio", "container", "expected_codec", "suffix"),
    [
        ("aac", "mp4", "aac", ".m4a"),
        ("aac", "mov", "aac", ".m4a"),
        ("opus", "webm", "opus", ".webm"),
    ],
)
def test_video_is_sent_as_audio_only_and_the_meeting_is_created(
    live, signed_in, tmp_path, audio, container, expected_codec, suffix
):
    video = make_video(tmp_path / f"clip.{container}", audio=audio)
    context, page = signed_in()
    seen = _create(page, video)
    meeting_id = _meeting_id(page)

    assert len(seen["post"]) == 1
    assert seen["post"][0]["filename"] == f"clip{suffix}"
    assert len(seen["put"]) == 1
    sent = tmp_path / f"sent{suffix}"
    sent.write_bytes(seen["put"][0])
    assert seen["post"][0]["size_bytes"] == sent.stat().st_size
    assert sent.stat().st_size < video.stat().st_size
    streams = probe(sent)["streams"]
    assert [s["codec_type"] for s in streams] == ["audio"]  # no video stream was sent
    assert streams[0]["codec_name"] == expected_codec

    meeting = context.request.get(f"{live.url}/api/meetings/{meeting_id}")
    assert meeting.status == 200


@pytest.mark.parametrize("delay", [0, 1])
def test_timestamps_match_after_server_normalization(live, signed_in, tmp_path, delay):
    video = make_video(tmp_path / "clip.mp4", audio_delay=delay)
    context, page = signed_in()
    seen = _create(page, video)
    meeting_id = _meeting_id(page)
    assert seen["put"]
    stored = tmp_path / "stored.m4a"
    response = context.request.get(f"{live.url}/api/meetings/{meeting_id}/audio")
    assert response.status == 200
    stored.write_bytes(response.body())
    # What the server would have stored had the whole video been uploaded: the extracted audio must
    # land on the same timeline (same PCM landmarks), delayed-start track included.
    reference = tmp_path / "reference.m4a"
    normalize_media(video, reference, max_duration_seconds=3600)
    want_onset, want_length = pcm_landmarks(reference)
    got_onset, got_length = pcm_landmarks(stored)
    assert abs(got_onset - want_onset) < TOLERANCE_SECONDS
    assert abs(got_length - want_length) < TOLERANCE_SECONDS


@pytest.mark.parametrize(
    ("container", "metadata", "timezone", "expected"),
    [
        (
            "mp4",
            ["-metadata", "creation_time=2026-10-14T23:30:00Z"],
            "Asia/Ho_Chi_Minh",
            "2026-10-15",
        ),
        (
            "mov",
            [
                "-metadata",
                "creation_time=2026-10-13T12:00:00Z",
                "-metadata",
                "com.apple.quicktime.creationdate=2026-10-15T00:30:00+0700",
                "-movflags",
                "use_metadata_tags",
            ],
            "America/Los_Angeles",
            "2026-10-15",
        ),
        ("webm", ["-metadata", "DATE_RECORDED=2026-10-15T00:30:00+07:00"], "UTC", "2026-10-15"),
    ],
)
def test_recording_date_is_read_locally_before_upload_and_only_audio_is_sent(
    live, signed_in, tmp_path, container, metadata, timezone, expected
):
    codec = "opus" if container == "webm" else "aac"
    source = make_video(tmp_path / f"source.{container}", audio=codec)
    video = tmp_path / f"dated.{container}"
    _ffmpeg("-i", str(source), "-c", "copy", *metadata, str(video))
    context, page = signed_in(timezone=timezone)
    requests = []
    page.on(
        "request", lambda request: requests.append((request.method, urlsplit(request.url).path))
    )
    dialog = _open_dialog(page)
    page.locator("#recording-file").set_input_files(str(video))
    expect(page.locator("#meeting-date-input")).to_have_value(expected)
    expect(page.locator("#meeting-date-help")).to_have_text(
        "Ngày từ metadata file. Bạn có thể sửa."
    )
    assert not [method for method, _path in requests if method in {"POST", "PUT"}]
    assert page.evaluate("document.activeElement.id") == "meeting-name"

    page.locator('input[name="language_hint"][value="en"]').check()
    page.locator("#auto-transcribe").set_checked(False)
    uploads = []
    page.on(
        "request",
        lambda request: (
            uploads.append(request.post_data_json)
            if request.method == "POST" and urlsplit(request.url).path == "/api/uploads"
            else None
        ),
    )
    dialog.get_by_role("button", name="Tạo cuộc họp").click()
    meeting_id = _meeting_id(page)
    meeting = context.request.get(f"{live.url}/api/meetings/{meeting_id}").json()
    assert meeting["meeting_date"] == expected
    assert uploads[0]["filename"].endswith(".webm" if container == "webm" else ".m4a")
    assert uploads[0]["size_bytes"] < video.stat().st_size
    stored = tmp_path / "stored-date.m4a"
    stored.write_bytes(context.request.get(f"{live.url}/api/meetings/{meeting_id}/audio").body())
    assert [stream["codec_type"] for stream in probe(stored)["streams"]] == ["audio"]


def test_date_metadata_is_optional_and_a_manual_date_is_preserved(live, signed_in, tmp_path):
    source = make_video(tmp_path / "undated.mp4")
    dated = tmp_path / "dated.mp4"
    _ffmpeg(
        "-i",
        str(source),
        "-c",
        "copy",
        "-metadata",
        "creation_time=2026-10-14T12:00:00Z",
        str(dated),
    )
    _context, page = signed_in()
    _open_dialog(page)
    page.locator("#recording-file").set_input_files(str(source))
    expect(page.locator("#meeting-date-help")).to_have_text(
        "Tự điền từ metadata nếu có; bạn có thể nhập hoặc sửa ngày."
    )
    expect(page.locator("#meeting-date-input")).to_have_value("")
    page.locator("#meeting-date-input").fill("2026-10-16")
    page.locator("#recording-file").set_input_files(str(dated))
    expect(page.locator("#meeting-date-help")).to_have_text(
        "Tự điền từ metadata nếu có; bạn có thể nhập hoặc sửa ngày."
    )
    expect(page.locator("#meeting-date-input")).to_have_value("2026-10-16")


def test_uncopyable_codec_sends_the_original_file(live, signed_in, tmp_path):
    video = make_video(tmp_path / "clip.mp4", audio="mp3")  # mp3 is not copied into an m4a
    context, page = signed_in()
    seen = _create(page, video)
    meeting_id = _meeting_id(page)

    assert seen["post"][0]["filename"] == "clip.mp4"
    assert seen["post"][0]["size_bytes"] == video.stat().st_size
    assert seen["put"][0] == video.read_bytes()  # the original, byte for byte
    assert context.request.get(f"{live.url}/api/meetings/{meeting_id}").status == 200


def test_fallback_notice_is_visible_while_the_original_uploads(live, signed_in, tmp_path):
    video = make_video(tmp_path / "clip.mp4", audio=None)
    _context, page = signed_in()
    # Hold the upload so the dialog stays on screen with the notice.
    release = []
    page.route(
        re.compile(r".*/api/uploads/[0-9a-f]+/content(?:\?.*)?$"),
        lambda route: release.append(route),
    )
    seen = _create(page, video)
    notice = page.locator("#upload-notice")
    expect(notice).to_contain_text("video không có luồng audio", timeout=15_000)
    expect(notice).to_contain_text("gửi nguyên file video")
    expect(page.locator("#state-label")).to_have_text("Đang tải lên")
    assert seen["post"][0] == {"filename": "clip.mp4", "size_bytes": video.stat().st_size}
    page.locator("#cancel-upload").click()
    expect(page.locator("#state-label")).to_have_text("Đã hủy")


def test_cancel_during_extraction_leaves_no_upload(live, signed_in, tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    _context, page = signed_in()
    # Hold the module that does the extraction, so the dialog stays in the extraction step.
    held = []
    page.route(re.compile(r".*/assets/audio-extract\.js$"), lambda route: held.append(route))
    seen = _create(page, video)
    expect(page.locator("#state-label")).to_have_text("Đang tách audio", timeout=10_000)
    expect(page.locator("#cancel-upload")).to_be_visible()
    page.locator("#cancel-upload").click()
    expect(page.locator("#state-label")).to_have_text("Đã hủy")
    for route in held:
        route.continue_()
    page.wait_for_timeout(500)
    assert seen["post"] == [] and seen["put"] == []  # no upload slot was ever created
    expect(page.locator("#create-meeting")).to_be_enabled()
