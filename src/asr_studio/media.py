"""Probe uploaded media and convert its selected audio stream to managed M4A."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

ALLOWED_EXTENSIONS = (
    "wav",
    "mp3",
    "m4a",
    "mp4",
    "mov",
    "webm",
    "ogg",
    "opus",
    "flac",
)
OUTPUT_CONTENT_TYPE = "audio/mp4"
OUTPUT_CODEC = "aac"
OUTPUT_BITRATE = "160k"
MAX_STDERR_BYTES = 8192
MAX_PROBE_JSON_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 6 * 60 * 60
PROBE_TIMEOUT_SECONDS = 60
LOCAL_PROTOCOLS = "file,pipe,crypto,data"
DECODE_DURATION_TOLERANCE_SECONDS = 2.0
ESTIMATED_DURATION_FORMATS = frozenset({"aac", "adts", "mp3"})

# Apply the limit in a fresh interpreter, not preexec_fn: media runs in an app
# thread, where executing Python between fork and exec can deadlock.
_FILE_LIMIT_EXEC = """
import os, resource, sys
limit = int(sys.argv[1])
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
for inherited in (soft, hard):
    if inherited != resource.RLIM_INFINITY:
        limit = min(limit, inherited)
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
os.execv(sys.argv[2], sys.argv[2:])
"""


class MediaProcessingError(RuntimeError):
    """A safe, actionable error raised while validating or converting media."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration_seconds: float
    codec_name: str
    stream_index: int
    declared_duration_seconds: float | None = None
    duration_is_authoritative: bool = True


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stdout_overflow: bool
    stderr: bytes = b""


def _run_bounded(
    argv: list[str],
    *,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
    deadline: float | None = None,
    file_size_limit: int | None = None,
) -> _CommandResult:
    """Run a fixed argv without a shell while retaining bounded command output."""
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    command_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        command_deadline = min(command_deadline, deadline)
    if command_deadline <= time.monotonic():
        raise MediaProcessingError("media_processing_timeout")
    if file_size_limit is not None:
        if isinstance(file_size_limit, bool) or file_size_limit < 1:
            raise ValueError("file_size_limit must be positive")
        argv = [sys.executable, "-c", _FILE_LIMIT_EXEC, str(file_size_limit), *argv]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise MediaProcessingError("media_tool_unavailable") from exc
    except OSError as exc:
        raise MediaProcessingError("media_processing_failed") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = False
    for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)

    deadline = command_deadline
    timed_out = False
    cancelled = False
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not timed_out:
                timed_out = True
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            if should_cancel is not None and not cancelled and should_cancel():
                cancelled = True
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            events = selector.select(min(max(remaining, 0), 0.25))
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = stdout if key.data == "stdout" else stderr
                limit = MAX_PROBE_JSON_BYTES if key.data == "stdout" else MAX_STDERR_BYTES
                previous_length = len(target)
                if previous_length < limit:
                    target.extend(chunk[: limit - previous_length])
                if key.data == "stdout" and len(chunk) > limit - previous_length:
                    stdout_overflow = True
                # stderr is drained but only its bounded prefix is retained.
        returncode = process.wait()
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            if not pipe.closed:
                pipe.close()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    if timed_out:
        raise MediaProcessingError("media_processing_timeout")
    if cancelled:
        raise MediaProcessingError("media_processing_cancelled")
    return _CommandResult(returncode, bytes(stdout), stdout_overflow, bytes(stderr))


def _supported_codec(codec_name: str) -> bool:
    if codec_name == "mp3" or codec_name in {"aac", "flac", "opus", "vorbis"}:
        return True
    return codec_name.startswith("pcm_")


def _parse_duration(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if result <= 0 or not result < float("inf"):
        return None
    return result


def _has_estimated_duration_format(format_name: object) -> bool:
    if not isinstance(format_name, str):
        return False
    return any(
        name.strip().lower() in ESTIMATED_DURATION_FORMATS for name in format_name.split(",")
    )


def probe_media(
    path: Path,
    *,
    max_duration_seconds: int,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> MediaProbe:
    """Inspect one supported audio stream and enforce its codec and duration policy."""
    executable = shutil.which("ffprobe")
    if executable is None:
        raise MediaProcessingError("media_tool_unavailable")
    result = _run_bounded(
        [
            executable,
            "-v",
            "error",
            "-protocol_whitelist",
            LOCAL_PROTOCOLS,
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,duration:stream_disposition=default",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
        **({"deadline": deadline} if deadline is not None else {}),
        **({"should_cancel": should_cancel} if should_cancel is not None else {}),
    )
    if result.returncode != 0 or result.stdout_overflow:
        raise MediaProcessingError("invalid_media")
    try:
        document = json.loads(result.stdout)
        streams = document.get("streams", [])
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProcessingError("invalid_media") from exc
    if not audio_streams:
        raise MediaProcessingError("missing_audio_stream")

    selected = next(
        (
            stream
            for stream in audio_streams
            if isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("default") == 1
        ),
        audio_streams[0],
    )
    codec_name = selected.get("codec_name")
    stream_index = selected.get("index")
    if not isinstance(codec_name, str) or not isinstance(stream_index, int):
        raise MediaProcessingError("invalid_media")
    if not _supported_codec(codec_name.lower()):
        raise MediaProcessingError("unsupported_codec")

    format_info = document.get("format")
    format_name = format_info.get("format_name") if isinstance(format_info, dict) else None
    declared_duration = _parse_duration(selected.get("duration"))
    duration = declared_duration
    has_non_audio_stream = any(stream.get("codec_type") != "audio" for stream in streams)
    duration_is_authoritative = not _has_estimated_duration_format(format_name) and (
        declared_duration is not None or not has_non_audio_stream
    )
    if duration is None and isinstance(format_info, dict):
        duration = _parse_duration(format_info.get("duration"))
    if duration is None:
        raise MediaProcessingError("invalid_media")
    if duration_is_authoritative and duration > max_duration_seconds:
        raise MediaProcessingError("duration_limit")
    return MediaProbe(
        duration,
        codec_name.lower(),
        stream_index,
        declared_duration if duration_is_authoritative else None,
        duration_is_authoritative,
    )


def normalize_media(
    source_path: Path,
    output_path: Path,
    *,
    max_duration_seconds: int,
    should_cancel: Callable[[], bool] | None = None,
    command_timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    probe_timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    max_output_bytes: int | None = None,
    deadline: float | None = None,
    strict_input: bool = False,
) -> MediaProbe:
    """Convert the chosen input audio stream to AAC at 160 kbps in faststart M4A."""
    source = probe_media(
        source_path,
        max_duration_seconds=max_duration_seconds,
        timeout_seconds=probe_timeout_seconds,
        deadline=deadline,
        should_cancel=should_cancel,
    )
    if should_cancel is not None and should_cancel():
        raise MediaProcessingError("media_processing_cancelled")
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise MediaProcessingError("media_tool_unavailable")
    result = _run_bounded(
        [
            executable,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            LOCAL_PROTOCOLS,
            "-i",
            str(source_path),
            "-map",
            f"0:{source.stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-t",
            str(max_duration_seconds + 1),
            "-c:a",
            OUTPUT_CODEC,
            "-b:a",
            OUTPUT_BITRATE,
            "-movflags",
            "+faststart",
            "-f",
            "ipod",
            str(output_path),
        ],
        timeout_seconds=command_timeout_seconds,
        should_cancel=should_cancel,
        deadline=deadline,
        file_size_limit=max_output_bytes + 1 if max_output_bytes is not None else None,
    )
    if max_output_bytes is not None:
        try:
            output_size = output_path.stat().st_size
        except OSError as exc:
            raise MediaProcessingError("conversion_failed") from exc
        if output_size > max_output_bytes:
            raise MediaProcessingError("media_output_limit")
    if result.returncode != 0 or (strict_input and result.stderr):
        raise MediaProcessingError("conversion_failed")
    if should_cancel is not None and should_cancel():
        raise MediaProcessingError("media_processing_cancelled")
    normalized = probe_media(
        output_path,
        max_duration_seconds=max_duration_seconds,
        timeout_seconds=probe_timeout_seconds,
        deadline=deadline,
        should_cancel=should_cancel,
    )
    if normalized.codec_name != OUTPUT_CODEC:
        raise MediaProcessingError("conversion_failed")
    declared_duration = source.declared_duration_seconds
    if declared_duration is not None:
        tolerance = max(DECODE_DURATION_TOLERANCE_SECONDS, declared_duration * 0.05)
        if normalized.duration_seconds + tolerance < declared_duration:
            raise MediaProcessingError("duration_mismatch")
    return MediaProbe(normalized.duration_seconds, source.codec_name, source.stream_index)


def _probe_extension_webm(
    path: Path,
    *,
    timeout_seconds: int,
    deadline: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, str, int, float | None]:
    """Probe the strict WebM/Opus input shape without trusting duration metadata."""
    executable = shutil.which("ffprobe")
    if executable is None:
        raise MediaProcessingError("media_tool_unavailable")
    result = _run_bounded(
        [
            executable,
            "-v",
            "error",
            "-protocol_whitelist",
            LOCAL_PROTOCOLS,
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        should_cancel=should_cancel,
    )
    if result.returncode != 0 or result.stdout_overflow or result.stderr:
        raise MediaProcessingError("invalid_media")
    try:
        document = json.loads(result.stdout)
        streams = document.get("streams", [])
        format_info = document.get("format", {})
        format_name = format_info.get("format_name", "")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProcessingError("invalid_media") from exc
    if not isinstance(format_name, str) or "webm" not in format_name.lower():
        raise MediaProcessingError("invalid_media")
    if not isinstance(streams, list) or len(streams) != 1:
        raise MediaProcessingError("invalid_media")
    stream = streams[0]
    if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
        raise MediaProcessingError("invalid_media")
    codec_name = stream.get("codec_name")
    stream_index = stream.get("index")
    if codec_name != "opus":
        raise MediaProcessingError("unsupported_codec")
    if not isinstance(stream_index, int):
        raise MediaProcessingError("invalid_media")
    duration = _parse_duration(stream.get("duration"))
    if duration is None and isinstance(format_info, dict):
        duration = _parse_duration(format_info.get("duration"))
    return format_name.lower(), codec_name, stream_index, duration


def normalize_extension_media(
    source_path: Path,
    output_path: Path,
    *,
    max_duration_seconds: int,
    max_output_bytes: int,
    timeout_seconds: int,
    should_cancel: Callable[[], bool] | None = None,
) -> MediaProbe:
    """Validate bounded WebM/Opus and normalize it without altering the raw archive."""
    if timeout_seconds < 1 or max_output_bytes < 1:
        raise ValueError("extension media limits must be positive")
    deadline = time.monotonic() + timeout_seconds
    try:
        source_size = source_path.stat().st_size
    except OSError as exc:
        raise MediaProcessingError("invalid_media") from exc
    if source_size < 1:
        raise MediaProcessingError("invalid_media")
    _, _, _, duration = _probe_extension_webm(
        source_path,
        timeout_seconds=min(timeout_seconds, PROBE_TIMEOUT_SECONDS),
        deadline=deadline,
        should_cancel=should_cancel,
    )
    remux_path: Path | None = None
    normalized_source = source_path
    try:
        if duration is None:
            if should_cancel is not None and should_cancel():
                raise MediaProcessingError("media_processing_cancelled")
            try:
                remux_fd, remux_name = tempfile.mkstemp(
                    prefix=".extension-remux-", suffix=".webm", dir=source_path.parent / ".tmp"
                )
                os.fchmod(remux_fd, 0o600)
                os.close(remux_fd)
            except OSError as exc:
                raise MediaProcessingError("storage_failed") from exc
            remux_path = Path(remux_name)
            executable = shutil.which("ffmpeg")
            if executable is None:
                raise MediaProcessingError("media_tool_unavailable")
            remux_result = _run_bounded(
                [
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    LOCAL_PROTOCOLS,
                    "-i",
                    str(source_path),
                    "-map",
                    "0:0",
                    "-c",
                    "copy",
                    "-f",
                    "webm",
                    str(remux_path),
                ],
                timeout_seconds=timeout_seconds,
                should_cancel=should_cancel,
                deadline=deadline,
                file_size_limit=max_output_bytes + 1,
            )
            try:
                if remux_path.stat().st_size > max_output_bytes:
                    raise MediaProcessingError("media_output_limit")
            except OSError as exc:
                raise MediaProcessingError("invalid_media") from exc
            if remux_result.returncode != 0 or remux_result.stderr:
                raise MediaProcessingError("invalid_media")
            normalized_source = remux_path
            _, _, _, duration = _probe_extension_webm(
                normalized_source,
                timeout_seconds=min(timeout_seconds, PROBE_TIMEOUT_SECONDS),
                deadline=deadline,
                should_cancel=should_cancel,
            )
        if duration is None:
            raise MediaProcessingError("invalid_media")
        if duration > max_duration_seconds:
            raise MediaProcessingError("duration_limit")
        return normalize_media(
            normalized_source,
            output_path,
            max_duration_seconds=max_duration_seconds,
            should_cancel=should_cancel,
            command_timeout_seconds=timeout_seconds,
            probe_timeout_seconds=min(timeout_seconds, PROBE_TIMEOUT_SECONDS),
            max_output_bytes=max_output_bytes,
            deadline=deadline,
            strict_input=True,
        )
    except MediaProcessingError:
        with suppress(FileNotFoundError):
            output_path.unlink()
        raise
    finally:
        if remux_path is not None:
            with suppress(FileNotFoundError):
                remux_path.unlink()
