from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from media_tools import require_media_tools

from asr_studio import media


def _webm(tmp_path, *, streaming=True, duration=0.25, codec="libopus", video=False):
    require_media_tools()
    source = tmp_path / "source.webm"
    (tmp_path / ".tmp").mkdir(exist_ok=True)
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
    ]
    if video:
        command += ["-f", "lavfi", "-i", f"color=black:size=16x16:duration={duration}"]
    command += ["-map", "0:a", "-c:a", codec]
    if video:
        command += ["-map", "1:v", "-c:v", "libvpx", "-deadline", "realtime"]
    command += ["-f", "webm", "pipe:1" if streaming else str(source)]
    result = subprocess.run(command, capture_output=True, check=True, timeout=30)
    if streaming:
        source.write_bytes(result.stdout)
    return source


def test_streaming_webm_without_duration_becomes_playable_without_changing_raw(tmp_path):
    source = _webm(tmp_path)
    original = source.read_bytes()
    assert media._probe_extension_webm(source, timeout_seconds=5)[3] is None
    output = tmp_path / "audio.m4a"
    result = media.normalize_extension_media(
        source, output, max_duration_seconds=2, max_output_bytes=1024**2, timeout_seconds=10
    )
    assert 0 < result.duration_seconds <= 2
    assert media.probe_media(output, max_duration_seconds=2).codec_name == "aac"
    assert source.read_bytes() == original
    assert list((tmp_path / ".tmp").iterdir()) == []


def test_streaming_remux_temp_name_can_be_scoped_to_session(tmp_path, monkeypatch):
    source = tmp_path / "source.webm"
    source.write_bytes(b"raw")
    (tmp_path / ".tmp").mkdir()
    session_id = "a" * 32
    captured: dict[str, Path] = {}

    monkeypatch.setattr(
        media,
        "_probe_extension_webm",
        lambda *args, **kwargs: ("webm", "opus", 0, None),
    )

    def fail_remux(argv, **kwargs):
        captured["path"] = Path(argv[-1])
        raise media.MediaProcessingError("media_processing_failed")

    monkeypatch.setattr(media, "_run_bounded", fail_remux)
    with pytest.raises(media.MediaProcessingError):
        media.normalize_extension_media(
            source,
            tmp_path / "audio.m4a",
            max_duration_seconds=2,
            max_output_bytes=1024**2,
            timeout_seconds=10,
            remux_prefix=f".{session_id}-",
        )
    assert captured["path"].name.startswith(f".{session_id}-")
    assert list((tmp_path / ".tmp").iterdir()) == []


@pytest.mark.parametrize("streaming", [True, False])
def test_extension_media_rejects_over_budget_output_and_retains_raw(tmp_path, streaming):
    source = _webm(tmp_path, streaming=streaming)
    original = source.read_bytes()
    output = tmp_path / "audio.m4a"
    with pytest.raises(media.MediaProcessingError) as rejected:
        media.normalize_extension_media(
            source, output, max_duration_seconds=2, max_output_bytes=128, timeout_seconds=10
        )
    assert rejected.value.code == "media_output_limit"
    assert source.read_bytes() == original
    assert not output.exists() or output.stat().st_size <= 129
    assert list((tmp_path / ".tmp").iterdir()) == []


@pytest.mark.parametrize(
    ("codec", "video", "error_code"),
    [("libvorbis", False, "unsupported_codec"), ("libopus", True, "invalid_media")],
)
def test_extension_media_rejects_other_codecs_and_extra_streams(tmp_path, codec, video, error_code):
    source = _webm(tmp_path, codec=codec, video=video)
    original = source.read_bytes()
    with pytest.raises(media.MediaProcessingError) as rejected:
        media.normalize_extension_media(
            source,
            tmp_path / "audio.m4a",
            max_duration_seconds=2,
            max_output_bytes=1024**2,
            timeout_seconds=10,
        )
    assert rejected.value.code == error_code
    assert source.read_bytes() == original


def test_streaming_extension_duration_limit_rejects_instead_of_truncating(tmp_path):
    source = _webm(tmp_path, duration=2.2)
    original = source.read_bytes()
    output = tmp_path / "audio.m4a"
    with pytest.raises(media.MediaProcessingError) as rejected:
        media.normalize_extension_media(
            source, output, max_duration_seconds=1, max_output_bytes=1024**2, timeout_seconds=10
        )
    assert rejected.value.code == "duration_limit"
    assert not output.exists()
    assert source.read_bytes() == original
    assert list((tmp_path / ".tmp").iterdir()) == []


def test_extension_media_shares_one_deadline_across_probe_remux_and_conversion(
    tmp_path, monkeypatch
):
    source = _webm(tmp_path)
    original = source.read_bytes()
    slow_tools = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = shutil.which(name)
        assert executable is not None
        wrapper = tmp_path / f"slow-{name}"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            "time.sleep(0.6)\n"
            f"os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        slow_tools[name] = str(wrapper)
    monkeypatch.setattr(media.shutil, "which", slow_tools.get)
    started = time.monotonic()
    with pytest.raises(media.MediaProcessingError) as rejected:
        media.normalize_extension_media(
            source,
            tmp_path / "audio.m4a",
            max_duration_seconds=2,
            max_output_bytes=1024**2,
            timeout_seconds=1,
        )
    assert rejected.value.code == "media_processing_timeout"
    assert time.monotonic() - started < 4
    assert source.read_bytes() == original
    assert list((tmp_path / ".tmp").iterdir()) == []


def test_command_file_limit_applies_during_writes(tmp_path):
    output = tmp_path / "oversized"
    result = media._run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'wb').write(b'x' * 1048576)",
            str(output),
        ],
        timeout_seconds=5,
        file_size_limit=4097,
    )
    assert result.returncode != 0
    assert 0 < output.stat().st_size <= 4097


def test_command_deadline_still_kills_and_reaps_after_output_pipes_close(tmp_path):
    pid_file = tmp_path / "pid"
    command = (
        "import os, sys, time; "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "os.close(1); os.close(2); time.sleep(5)"
    )
    with pytest.raises(media.MediaProcessingError) as rejected:
        media._run_bounded([sys.executable, "-c", command, str(pid_file)], timeout_seconds=1)
    assert rejected.value.code == "media_processing_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("streaming", [True, False])
def test_truncated_extension_webm_is_rejected_and_raw_retained(tmp_path, streaming):
    source = _webm(tmp_path, streaming=streaming)
    complete = source.read_bytes()
    truncated = complete[: len(complete) // 2]
    source.write_bytes(truncated)
    output = tmp_path / "audio.m4a"
    with pytest.raises(media.MediaProcessingError) as rejected:
        media.normalize_extension_media(
            source,
            output,
            max_duration_seconds=2,
            max_output_bytes=1024**2,
            timeout_seconds=10,
        )
    assert rejected.value.code in {"invalid_media", "conversion_failed"}
    assert not output.exists()
    assert source.read_bytes() == truncated
    assert list((tmp_path / ".tmp").iterdir()) == []


def test_extension_decoder_rejects_invalid_late_opus_packet(tmp_path):
    source = _webm(tmp_path, duration=6)
    packet_dump = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_packets",
            "-show_data",
            "-show_entries",
            "packet=data",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    packets = json.loads(packet_dump.stdout)["packets"]
    payload = bytes.fromhex(
        "".join(
            line.split(":", 1)[1].split("  ", 1)[0].strip().replace(" ", "")
            for line in packets[-1]["data"].splitlines()
            if ":" in line
        )
    )
    raw = source.read_bytes()
    offset = raw.rfind(payload)
    assert offset >= 0 and len(payload) >= 2
    # Invalid code-3 Opus frame count, inside an otherwise complete WebM block.
    source.write_bytes(raw[:offset] + b"\x03\x00" + raw[offset + 2 :])
    # Stream/header inspection succeeds; the full decode must still reject it.
    assert media._probe_extension_webm(source, timeout_seconds=5)[1] == "opus"
    with pytest.raises(media.MediaProcessingError):
        media.normalize_extension_media(
            source,
            tmp_path / "bad.m4a",
            max_duration_seconds=10,
            max_output_bytes=1024**2,
            timeout_seconds=10,
        )
    assert not (tmp_path / "bad.m4a").exists()
