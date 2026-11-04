"""HTTP routes for access, uploads, meetings and the authenticated workspace."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from html import escape as escape_html
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from asr_studio import __version__
from asr_studio.antigravity import AntigravityError, AntigravityProvider
from asr_studio.asr_models import AUTO, model_choices, resolve_run, selectable_models
from asr_studio.auth import AuthStateError, create_session_cookie, token_matches
from asr_studio.chatgpt import ChatGPTError, ChatGPTProvider
from asr_studio.config import Settings
from asr_studio.media import ALLOWED_EXTENSIONS, OUTPUT_BITRATE, OUTPUT_CODEC
from asr_studio.meeting_deletion import MeetingDeletionError, delete_meeting
from asr_studio.notes import has_active_notes_task
from asr_studio.notes_routes import install_notes_routes
from asr_studio.recordings import (
    RecordingError,
    begin_upload,
    complete_upload,
    create_meeting,
    create_upload,
    delete_upload,
    fail_upload,
    finish_upload,
    get_audio_recording,
    get_meeting,
    get_upload,
    get_upload_received_bytes,
    list_meetings,
    open_audio,
    release_upload,
    update_meeting,
)
from asr_studio.runtime import read_runtime_profile
from asr_studio.tasks import (
    TaskError,
    accept_transcription_task,
    add_current_generation_speaker,
    cancel_task,
    get_task,
    reassign_current_generation_speaker,
    update_current_generation_segment_text,
    update_current_generation_speaker_name,
)
from asr_studio.worker import is_healthy as worker_is_healthy

SESSION_COOKIE = "asr_studio_session"
WEB_DIR = Path(__file__).parent / "web"
FONT_FILES = frozenset(
    {
        "inter-vietnamese.woff2",
        "inter-latin-ext.woff2",
        "inter-latin.woff2",
        "newsreader-vietnamese.woff2",
        "newsreader-latin-ext.woff2",
        "newsreader-latin.woff2",
        "jetbrains-mono-vietnamese.woff",
        "jetbrains-mono-latin-ext.woff2",
        "jetbrains-mono-latin.woff2",
    }
)
_AUDIO_CHUNK_SIZE = 1024 * 1024
_UPLOAD_IDLE_TIMEOUT_SECONDS = 60
_MEDIA_VALIDATION_CONCURRENCY = 2
_RangeResult = tuple[int, int] | Literal["ignore", "unsatisfiable"]


async def _await_owned(task: asyncio.Task[Any]) -> tuple[Any, bool]:
    """Drain an owned task before propagating cancellation to its caller."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    return result, cancelled


async def _owned_thread_call(
    function: Any, *args: Any, _capture_cancel: bool = False, **kwargs: Any
) -> Any:
    """Run a filesystem/DB operation without abandoning it on request cancel."""
    task = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))
    result, cancelled = await _await_owned(task)
    if cancelled and not _capture_cancel:
        raise asyncio.CancelledError
    return (result, cancelled) if _capture_cancel else result


async def _owned_cleanup_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Drain cleanup work even when another cancellation arrives during cleanup."""
    return await _owned_thread_call(function, *args, _capture_cancel=True, **kwargs)


async def _begin_upload_owned(data_dir: Path, upload_id: str, offset: int) -> tuple[Path, int, int]:
    """Acquire the upload writer without abandoning the guard on request cancel."""
    result, cancelled = await _owned_thread_call(
        begin_upload,
        data_dir,
        upload_id,
        offset,
        _capture_cancel=True,
    )
    if cancelled:
        _, release_cancelled = await _owned_cleanup_call(release_upload, data_dir, upload_id)
        if release_cancelled:
            raise asyncio.CancelledError
        raise asyncio.CancelledError
    return result


def _asset_response(filename: str, media_type: str) -> Response:
    if filename == "shell.html":
        content = (WEB_DIR / filename).read_text(encoding="utf-8")
        content = content.replace("{{ASR_STUDIO_VERSION}}", escape_html(__version__))
        return HTMLResponse(content, media_type=media_type)
    return FileResponse(WEB_DIR / filename, media_type=media_type)


def _raise_recording_error(error: RecordingError) -> None:
    detail = {"code": error.code, "message": error.message, **error.details}
    raise HTTPException(
        status_code=error.status_code,
        detail=detail,
    ) from None


def _raise_task_error(error: TaskError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from None


def _raise_meeting_deletion_error(error: MeetingDeletionError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from None


async def _json_object(
    request: Request,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_json", "message": "Provide a valid JSON object."},
        ) from None
    allowed = required | (optional or set())
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or not set(payload).issubset(allowed)
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_request", "message": "The JSON fields are invalid."},
        )
    return payload


def _parse_byte_range(value: str, size: int) -> _RangeResult:
    if "=" not in value:
        return "ignore"
    unit, specification = value.split("=", maxsplit=1)
    if unit.strip().lower() != "bytes" or "," in specification:
        return "ignore"
    specification = specification.strip()
    if "-" not in specification:
        return "ignore"
    first, last = specification.split("-", maxsplit=1)
    if not first:
        if not last.isascii() or not last.isdecimal() or len(last) > 20:
            return "ignore"
        suffix_length = int(last)
        if suffix_length < 1:
            return "unsatisfiable"
        if size < 1:
            return "unsatisfiable"
        return max(0, size - suffix_length), size - 1
    if (
        not first.isascii()
        or not first.isdecimal()
        or len(first) > 20
        or (last and (not last.isascii() or not last.isdecimal() or len(last) > 20))
    ):
        return "ignore"
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size:
        return "unsatisfiable"
    if end < start:
        return "ignore"
    return start, min(end, size - 1)


def install_routes(
    app: FastAPI,
    settings: Settings,
    chatgpt_provider: ChatGPTProvider,
    antigravity_provider: AntigravityProvider,
) -> None:
    validation_slots = asyncio.Semaphore(_MEDIA_VALIDATION_CONCURRENCY)

    async def provider_json(operation):
        try:
            return await run_in_threadpool(operation)
        except (ChatGPTError, AntigravityError) as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code, "message": error.public_message},
            ) from None

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/access", include_in_schema=False)
    async def access_page() -> FileResponse:
        return _asset_response("access.html", "text/html")

    @app.get("/assets/access.css", include_in_schema=False)
    async def access_css() -> FileResponse:
        return _asset_response("access.css", "text/css")

    @app.get("/assets/access.js", include_in_schema=False)
    async def access_js() -> FileResponse:
        return _asset_response("access.js", "text/javascript")

    @app.get("/assets/design-system.css", include_in_schema=False)
    async def design_system_css() -> FileResponse:
        return _asset_response("design-system.css", "text/css")

    @app.get("/assets/i18n.js", include_in_schema=False)
    async def i18n_js() -> FileResponse:
        return _asset_response("i18n.js", "text/javascript")

    @app.get("/assets/theme.js", include_in_schema=False)
    async def theme_js() -> FileResponse:
        return _asset_response("theme.js", "text/javascript")

    @app.get("/assets/fonts/{filename}", include_in_schema=False)
    async def font_asset(filename: str) -> FileResponse:
        if filename not in FONT_FILES:
            raise HTTPException(status_code=404, detail="Font asset not found")
        media_type = "font/woff2" if filename.endswith(".woff2") else "font/woff"
        return _asset_response(f"fonts/{filename}", media_type)

    @app.get("/", include_in_schema=False)
    async def workspace() -> HTMLResponse:
        return _asset_response("shell.html", "text/html")

    @app.get("/assets/shell.css", include_in_schema=False)
    async def shell_css() -> FileResponse:
        return _asset_response("shell.css", "text/css")

    @app.get("/assets/shell.js", include_in_schema=False)
    async def shell_js() -> Response:
        asset_path = WEB_DIR / "shell.js"
        if not asset_path.is_file():
            return Response(status_code=404)
        return FileResponse(asset_path, media_type="text/javascript")

    @app.get("/assets/audio-extract.js", include_in_schema=False)
    async def audio_extract_js() -> FileResponse:
        return _asset_response("audio-extract.js", "text/javascript")

    @app.get("/assets/vendor/mediabunny/mediabunny.min.mjs", include_in_schema=False)
    async def mediabunny_js() -> FileResponse:
        return _asset_response("vendor/mediabunny/mediabunny.min.mjs", "text/javascript")

    @app.get("/assets/shell-core.js", include_in_schema=False)
    async def shell_core_js() -> FileResponse:
        return _asset_response("shell-core.js", "text/javascript")

    @app.get("/assets/store.js", include_in_schema=False)
    async def store_js() -> FileResponse:
        return _asset_response("store.js", "text/javascript")

    @app.get("/assets/channel.js", include_in_schema=False)
    async def channel_js() -> FileResponse:
        return _asset_response("channel.js", "text/javascript")

    @app.get("/assets/notes-core.js", include_in_schema=False)
    async def notes_core_js() -> FileResponse:
        return _asset_response("notes-core.js", "text/javascript")

    @app.get("/assets/connections.css", include_in_schema=False)
    async def connections_css() -> Response:
        asset_path = WEB_DIR / "connections.css"
        if not asset_path.is_file():
            return Response(status_code=404)
        return FileResponse(asset_path, media_type="text/css")

    @app.get("/assets/provider-core.js", include_in_schema=False)
    async def provider_core_js() -> FileResponse:
        return _asset_response("provider-core.js", "text/javascript")

    @app.get("/assets/connections.js", include_in_schema=False)
    async def connections_js() -> Response:
        asset_path = WEB_DIR / "connections.js"
        if not asset_path.is_file():
            return Response(status_code=404)
        return FileResponse(asset_path, media_type="text/javascript")

    @app.get("/assets/antigravity.js", include_in_schema=False)
    async def antigravity_js() -> Response:
        asset_path = WEB_DIR / "antigravity.js"
        if not asset_path.is_file():
            return Response(status_code=404)
        return FileResponse(asset_path, media_type="text/javascript")

    @app.get("/assets/history.css", include_in_schema=False)
    async def history_css() -> FileResponse:
        return _asset_response("history.css", "text/css")

    @app.get("/assets/history.js", include_in_schema=False)
    async def history_js() -> FileResponse:
        return _asset_response("history.js", "text/javascript")

    @app.get("/assets/history-core.js", include_in_schema=False)
    async def history_core_js() -> FileResponse:
        return _asset_response("history-core.js", "text/javascript")

    @app.get("/assets/task-core.js", include_in_schema=False)
    async def task_core_js() -> FileResponse:
        return _asset_response("task-core.js", "text/javascript")

    @app.get("/assets/transcript-core.js", include_in_schema=False)
    async def transcript_core_js() -> FileResponse:
        return _asset_response("transcript-core.js", "text/javascript")

    @app.get("/assets/playback-core.js", include_in_schema=False)
    async def playback_core_js() -> FileResponse:
        return _asset_response("playback-core.js", "text/javascript")

    @app.get("/meetings/{meeting_id}", include_in_schema=False)
    async def meeting_page(meeting_id: str) -> HTMLResponse:
        return _asset_response("shell.html", "text/html")

    @app.get("/api/uploads/policy")
    async def upload_policy() -> dict[str, Any]:
        return {
            "extensions": list(ALLOWED_EXTENSIONS),
            "max_size_bytes": settings.max_upload_bytes,
            "max_duration_seconds": settings.max_recording_seconds,
            "chunk_bytes": settings.upload_chunk_bytes,
            "output": {
                "container": "m4a",
                "audio_codec": OUTPUT_CODEC,
                "bitrate_kbps": int(OUTPUT_BITRATE.removesuffix("k")),
                "faststart": True,
            },
        }

    @app.get("/api/providers")
    async def list_providers() -> dict[str, Any]:
        def load_providers() -> dict[str, Any]:
            chatgpt = chatgpt_provider.get_providers()
            providers = [*chatgpt["providers"], antigravity_provider.provider_view()]
            for provider in providers:
                in_use = has_active_notes_task(settings.data_dir, provider=provider["id"])
                provider["in_use"] = in_use
                provider["in_use_reason"] = "notes" if in_use else None
            return {"providers": providers}

        return await provider_json(load_providers)

    @app.post("/api/providers/antigravity/login-sessions", status_code=201)
    async def start_antigravity_login(request: Request) -> dict[str, Any]:
        await _json_object(request, required=set())
        return await provider_json(antigravity_provider.start_login)

    @app.get("/api/providers/antigravity/login-sessions/{session_id}")
    async def get_antigravity_login(session_id: str) -> dict[str, Any]:
        return await provider_json(lambda: antigravity_provider.login_session(session_id))

    @app.post("/api/providers/antigravity/login-sessions/{session_id}/input")
    async def answer_antigravity_login(session_id: str, request: Request) -> dict[str, Any]:
        payload = await _json_object(request, required={"value"})
        if not isinstance(payload["value"], str):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_login_input", "message": "Provide a string value."},
            )
        return await provider_json(
            lambda: antigravity_provider.submit_login_input(session_id, payload["value"])
        )

    @app.delete("/api/providers/antigravity/login-sessions/{session_id}")
    async def cancel_antigravity_login(session_id: str) -> dict[str, Any]:
        return await provider_json(lambda: antigravity_provider.cancel_login(session_id))

    @app.get("/api/providers/antigravity/models")
    async def list_antigravity_models() -> dict[str, Any]:
        return await provider_json(antigravity_provider.list_models)

    @app.put("/api/providers/antigravity/preference")
    async def put_antigravity_preference(request: Request) -> dict[str, Any]:
        payload = await _json_object(request, required={"model"}, optional={"reasoning_effort"})
        if not isinstance(payload["model"], str):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_model", "message": "Choose an Antigravity model."},
            )
        return await provider_json(
            lambda: antigravity_provider.set_preference(
                payload["model"], payload.get("reasoning_effort")
            )
        )

    @app.post("/api/providers/antigravity/verify")
    async def verify_antigravity(request: Request) -> dict[str, Any]:
        payload = await _json_object(request, required={"model"})
        if not isinstance(payload["model"], str):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_model", "message": "Choose an Antigravity model."},
            )
        return await provider_json(lambda: antigravity_provider.verify(payload["model"]))

    @app.delete("/api/providers/antigravity/connection")
    async def disconnect_antigravity() -> dict[str, Any]:
        return await provider_json(antigravity_provider.disconnect)

    @app.post("/api/providers/chatgpt/login-sessions", status_code=201)
    async def start_chatgpt_login(request: Request) -> dict[str, Any]:
        await _json_object(request, required=set())
        return await provider_json(chatgpt_provider.start_login)

    @app.get("/api/providers/chatgpt/login-sessions/{session_id}")
    async def get_chatgpt_login(session_id: str) -> dict[str, Any]:
        return await provider_json(lambda: chatgpt_provider.login_session(session_id))

    @app.delete("/api/providers/chatgpt/login-sessions/{session_id}")
    async def cancel_chatgpt_login(session_id: str) -> dict[str, Any]:
        return await provider_json(lambda: chatgpt_provider.cancel_login(session_id))

    @app.get("/api/providers/chatgpt/models")
    async def list_chatgpt_models() -> dict[str, Any]:
        return await provider_json(chatgpt_provider.list_models)

    @app.post("/api/providers/chatgpt/verify")
    async def verify_chatgpt(request: Request) -> dict[str, Any]:
        payload = await _json_object(request, required={"model"}, optional={"reasoning_effort"})
        if not isinstance(payload["model"], str) or not isinstance(
            payload.get("reasoning_effort", "default"), str
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_model",
                    "message": "Choose an available ChatGPT model to verify.",
                },
            )
        return await provider_json(
            lambda: chatgpt_provider.verify(
                model=payload["model"], reasoning_effort=payload.get("reasoning_effort")
            )
        )

    @app.delete("/api/providers/chatgpt/connection")
    async def disconnect_chatgpt() -> dict[str, Any]:
        return await provider_json(chatgpt_provider.disconnect)

    @app.get("/auth/chatgpt/callback", include_in_schema=False)
    async def chatgpt_callback(request: Request) -> HTMLResponse:
        try:
            await run_in_threadpool(
                chatgpt_provider.handle_callback,
                request.query_params.multi_items(),
            )
        except ChatGPTError as error:
            page = (
                '<!doctype html><html lang="vi"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                "<title>ChatGPT sign-in</title><p>Không thể hoàn tất đăng nhập ChatGPT. "
                "Trở về ASR Studio để xem chi tiết.</p>"
                '<p lang="en">Could not complete the ChatGPT sign-in. '
                "Return to ASR Studio for details.</p></html>"
            )
            return HTMLResponse(page, status_code=error.status_code)
        return HTMLResponse(
            '<!doctype html><html lang="vi"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>ChatGPT sign-in</title>"
            "<p>Đăng nhập ChatGPT đã hoàn tất. Bạn có thể đóng thẻ này.</p>"
            '<p lang="en">ChatGPT sign-in is complete. You can close this tab.</p></html>'
        )

    @app.post("/api/uploads", status_code=201)
    async def start_upload(request: Request) -> dict[str, object]:
        payload = await _json_object(request, required={"filename", "size_bytes"})
        try:
            return await run_in_threadpool(
                create_upload,
                settings.data_dir,
                filename=payload["filename"],
                size_bytes=payload["size_bytes"],
                max_upload_bytes=settings.max_upload_bytes,
            )
        except RecordingError as error:
            _raise_recording_error(error)

    @app.put("/api/uploads/{upload_id}/content", response_model=None)
    async def upload_content(
        upload_id: str,
        request: Request,
        offset: int | None = Query(default=None, ge=0),
    ) -> dict[str, object] | Response:
        requested_offset = 0 if offset is None else offset
        try:
            raw_path, declared_size, committed_size = await _begin_upload_owned(
                settings.data_dir, upload_id, requested_offset
            )
        except RecordingError as error:
            _raise_recording_error(error)

        file_handle = None
        writer_owned = True
        durable_committed = False
        received_size = committed_size
        cleanup_size = committed_size
        stream_interrupted = False
        cleanup_cancelled = False
        try:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(raw_path, flags)
            os.fchmod(descriptor, 0o600)
            file_handle = os.fdopen(descriptor, "r+b", buffering=0)
            # An interrupted previous request may have left uncommitted bytes behind.
            await _owned_thread_call(os.ftruncate, file_handle.fileno(), committed_size)
            await _owned_thread_call(file_handle.seek, committed_size)
            stream = request.stream().__aiter__()
            while True:
                try:
                    async with asyncio.timeout(_UPLOAD_IDLE_TIMEOUT_SECONDS):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise RecordingError(
                        408,
                        "upload_timeout",
                        "No upload bytes arrived in time. Resume from received_bytes.",
                        details={"received_bytes": committed_size},
                    ) from None
                except BaseException:
                    stream_interrupted = True
                    raise
                if not chunk:
                    continue
                received_size += len(chunk)
                if received_size > settings.max_upload_bytes:
                    raise RecordingError(
                        413,
                        "upload_too_large",
                        f"The file exceeds the {settings.max_upload_bytes}-byte upload limit.",
                    )
                if received_size > declared_size:
                    raise RecordingError(
                        400,
                        "size_mismatch",
                        "The received byte count does not match size_bytes.",
                    )
                written = await _owned_thread_call(file_handle.write, chunk)
                if written != len(chunk):
                    raise RecordingError(
                        500, "storage_failed", "The upload could not be written safely."
                    )
            if received_size == committed_size:
                raise RecordingError(
                    400,
                    "size_mismatch",
                    "The received byte count does not advance the upload.",
                )
            await _owned_thread_call(os.fsync, file_handle.fileno())
            status, finish_cancelled = await _owned_thread_call(
                finish_upload,
                settings.data_dir,
                upload_id,
                received_size,
                expected_bytes=committed_size,
                _capture_cancel=True,
            )
            durable_committed = True
            if finish_cancelled:
                raise asyncio.CancelledError
        except BaseException as error:
            interrupted = (
                stream_interrupted
                or isinstance(error, (asyncio.CancelledError, ClientDisconnect))
                or (isinstance(error, RecordingError) and error.code == "upload_timeout")
            )
            if file_handle is not None:
                if interrupted and not durable_committed:
                    try:
                        durable_offset, was_cancelled = await _owned_cleanup_call(
                            get_upload_received_bytes,
                            settings.data_dir,
                            upload_id,
                        )
                        cleanup_cancelled = cleanup_cancelled or was_cancelled
                        cleanup_size = max(
                            committed_size,
                            min(
                                declared_size,
                                durable_offset,
                            ),
                        )
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                        cleanup_size = committed_size
                    except BaseException:
                        cleanup_size = committed_size
                    try:
                        _, was_cancelled = await _owned_cleanup_call(
                            os.ftruncate, file_handle.fileno(), cleanup_size
                        )
                        cleanup_cancelled = cleanup_cancelled or was_cancelled
                        _, was_cancelled = await _owned_cleanup_call(os.fsync, file_handle.fileno())
                        cleanup_cancelled = cleanup_cancelled or was_cancelled
                    except OSError:
                        pass
                _, was_cancelled = await _owned_cleanup_call(file_handle.close)
                cleanup_cancelled = cleanup_cancelled or was_cancelled
                file_handle = None
            if cleanup_cancelled:
                raise asyncio.CancelledError from None
            if interrupted:
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, ClientDisconnect):
                    return Response(status_code=204)
                if isinstance(error, RecordingError):
                    _raise_recording_error(error)
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "upload_interrupted",
                        "message": "The upload was interrupted. Resume from received_bytes.",
                        "received_bytes": cleanup_size,
                    },
                ) from None
            if isinstance(error, RecordingError):
                fail_upload(settings.data_dir, upload_id, code=error.code)
                _raise_recording_error(error)
            await _owned_thread_call(
                fail_upload,
                settings.data_dir,
                upload_id,
                code="upload_interrupted",
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "upload_interrupted",
                    "message": "The upload was interrupted. Start a new upload.",
                },
            ) from None
        finally:
            try:
                if file_handle is not None:
                    _, was_cancelled = await _owned_cleanup_call(file_handle.close)
                    cleanup_cancelled = cleanup_cancelled or was_cancelled
            finally:
                if writer_owned:
                    _, was_cancelled = await _owned_cleanup_call(
                        release_upload, settings.data_dir, upload_id
                    )
                    cleanup_cancelled = cleanup_cancelled or was_cancelled
            if cleanup_cancelled:
                raise asyncio.CancelledError
        return {
            "id": upload_id,
            "status": status,
            "size_bytes": declared_size,
            "received_bytes": received_size,
        }

    @app.post("/api/uploads/{upload_id}/complete")
    async def complete_upload_route(upload_id: str) -> dict[str, object]:
        try:
            async with validation_slots:
                return await run_in_threadpool(
                    complete_upload,
                    settings.data_dir,
                    upload_id,
                    max_duration_seconds=settings.max_recording_seconds,
                )
        except RecordingError as error:
            _raise_recording_error(error)

    @app.get("/api/uploads/{upload_id}")
    async def read_upload(upload_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(get_upload, settings.data_dir, upload_id)
        except RecordingError as error:
            _raise_recording_error(error)

    @app.delete("/api/uploads/{upload_id}")
    async def remove_upload(upload_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(delete_upload, settings.data_dir, upload_id)
        except RecordingError as error:
            _raise_recording_error(error)

    @app.post("/api/meetings", status_code=201)
    async def make_meeting(request: Request) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"upload_id", "title", "language_hint"},
            optional={"meeting_date"},
        )
        try:
            return await run_in_threadpool(
                create_meeting,
                settings.data_dir,
                upload_id=payload["upload_id"],
                title=payload["title"],
                meeting_date=payload.get("meeting_date"),
                language_hint=payload["language_hint"],
            )
        except RecordingError as error:
            _raise_recording_error(error)

    @app.get("/api/meetings")
    async def read_meeting_history(q: str | None = Query(default=None, max_length=200)):
        return await run_in_threadpool(list_meetings, settings.data_dir, query=q)

    async def with_runtime(meeting: dict[str, object]) -> dict[str, object]:
        """What this machine can run now: the page replaces its meeting with these answers."""
        profile = (
            await run_in_threadpool(read_runtime_profile, settings.data_dir)
            if worker_is_healthy(settings.data_dir)
            else None
        )
        transcription = meeting["transcription"]
        assert isinstance(transcription, dict)
        transcription["runtime"] = profile.public_summary() if profile is not None else None
        transcription["asr_models"] = await run_in_threadpool(model_choices, settings, profile)
        return meeting

    @app.patch("/api/meetings/{meeting_id}")
    async def edit_meeting(meeting_id: str, request: Request) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"expected_metadata_version"},
            optional={"title", "meeting_date"},
        )
        try:
            meeting = await run_in_threadpool(
                update_meeting,
                settings.data_dir,
                meeting_id,
                expected_version=payload["expected_metadata_version"],
                fields={key: payload[key] for key in ("title", "meeting_date") if key in payload},
            )
            return await with_runtime(meeting)
        except RecordingError as error:
            _raise_recording_error(error)

    @app.delete("/api/meetings/{meeting_id}")
    async def remove_meeting(meeting_id: str) -> dict[str, str]:
        try:
            return await run_in_threadpool(delete_meeting, settings.data_dir, meeting_id)
        except MeetingDeletionError as error:
            _raise_meeting_deletion_error(error)

    @app.get("/api/meetings/{meeting_id}")
    async def read_meeting(meeting_id: str) -> dict[str, object]:
        try:
            meeting = await run_in_threadpool(get_meeting, settings.data_dir, meeting_id)
            return await with_runtime(meeting)
        except RecordingError as error:
            _raise_recording_error(error)

    @app.post("/api/tasks", status_code=202)
    async def start_transcription_task(request: Request) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"meeting_id", "request_id"},
            optional={"language_hint", "asr_model"},
        )
        worker_ready = worker_is_healthy(settings.data_dir)
        profile = read_runtime_profile(settings.data_dir) if worker_ready else None
        asr_model = payload.get("asr_model", AUTO)
        if asr_model not in selectable_models():
            _raise_task_error(
                TaskError(400, "invalid_asr_model", "Choose an ASR model from the list.")
            )
        task_options = {
            "meeting_id": payload["meeting_id"],
            "request_id": payload["request_id"],
            **resolve_run(settings, profile, asr_model),
            "diarization_model_path": settings.diarization_model_path,
            "worker_ready": worker_ready and profile is not None,
        }
        if "language_hint" in payload:
            task_options["language_hint"] = payload["language_hint"]
        try:
            return await run_in_threadpool(
                accept_transcription_task,
                settings.data_dir,
                **task_options,
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.get("/api/tasks/{task_id}")
    async def read_transcription_task(task_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(get_task, settings.data_dir, task_id)
        except TaskError as error:
            _raise_task_error(error)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_transcription_task(task_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(cancel_task, settings.data_dir, task_id)
        except TaskError as error:
            _raise_task_error(error)

    @app.patch("/api/meetings/{meeting_id}/transcript-segments/{segment_id}/speaker")
    async def reassign_transcript_speaker(
        meeting_id: str, segment_id: str, request: Request
    ) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={
                "expected_generation_id",
                "expected_source_version",
                "expected_assignment_version",
                "speaker_id",
            },
        )
        try:
            return await run_in_threadpool(
                reassign_current_generation_speaker,
                settings.data_dir,
                meeting_id,
                segment_id,
                expected_generation_id=payload["expected_generation_id"],
                expected_source_version=payload["expected_source_version"],
                expected_assignment_version=payload["expected_assignment_version"],
                speaker_id=payload["speaker_id"],
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.patch("/api/meetings/{meeting_id}/transcript-segments/{segment_id}/text")
    async def update_transcript_segment_text(
        meeting_id: str, segment_id: str, request: Request
    ) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"expected_generation_id", "expected_source_version", "text"},
        )
        try:
            return await run_in_threadpool(
                update_current_generation_segment_text,
                settings.data_dir,
                meeting_id,
                segment_id,
                expected_generation_id=payload["expected_generation_id"],
                expected_source_version=payload["expected_source_version"],
                text=payload["text"],
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.post("/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers")
    async def add_transcript_speaker(
        meeting_id: str, generation_id: str, request: Request
    ) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"expected_source_version"},
            optional={"display_name"},
        )
        try:
            return await run_in_threadpool(
                add_current_generation_speaker,
                settings.data_dir,
                meeting_id,
                expected_generation_id=generation_id,
                expected_source_version=payload["expected_source_version"],
                display_name=payload.get("display_name", ""),
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.patch(
        "/api/meetings/{meeting_id}/transcript-generations/{generation_id}/speakers/{speaker_id}"
    )
    async def update_transcript_speaker_name(
        meeting_id: str, generation_id: str, speaker_id: str, request: Request
    ) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={"expected_source_version", "display_name"},
        )
        try:
            return await run_in_threadpool(
                update_current_generation_speaker_name,
                settings.data_dir,
                meeting_id,
                speaker_id,
                expected_generation_id=generation_id,
                expected_source_version=payload["expected_source_version"],
                display_name=payload["display_name"],
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.api_route("/api/meetings/{meeting_id}/audio", methods=["GET", "HEAD"])
    async def meeting_audio(meeting_id: str, request: Request) -> Response:
        try:
            recording = await run_in_threadpool(get_audio_recording, settings.data_dir, meeting_id)
            size, audio_file = await run_in_threadpool(
                open_audio, settings.data_dir, recording["storage_key"]
            )
        except RecordingError as error:
            _raise_recording_error(error)

        requested_range = request.headers.get("range")
        byte_range = _parse_byte_range(requested_range, size) if requested_range else "ignore"
        if byte_range == "unsatisfiable":
            await run_in_threadpool(audio_file.close)
            return Response(
                status_code=416,
                headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
            )

        if not isinstance(byte_range, tuple):
            start, end, status_code = 0, max(size - 1, 0), 200
        else:
            start, end = byte_range
            status_code = 206
        content_length = max(0, end - start + 1)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD":
            await run_in_threadpool(audio_file.close)
            headers["Content-Type"] = recording["content_type"]
            return Response(status_code=status_code, headers=headers)

        await run_in_threadpool(audio_file.seek, start)

        async def stream_audio() -> AsyncIterator[bytes]:
            remaining = content_length
            try:
                while remaining > 0:
                    chunk = await run_in_threadpool(
                        audio_file.read,
                        min(_AUDIO_CHUNK_SIZE, remaining),
                    )
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                audio_file.close()

        return StreamingResponse(
            stream_audio(),
            status_code=status_code,
            media_type=recording["content_type"],
            headers=headers,
        )

    @app.post("/api/access/session", status_code=204, include_in_schema=False)
    async def create_access_session(
        request: Request,
        response: Response,
    ) -> Response:
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid access request") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"token"}
            or not isinstance(payload["token"], str)
            or not 40 <= len(payload["token"]) <= 512
        ):
            raise HTTPException(status_code=400, detail="Invalid access request")

        try:
            state = request.app.state.auth_store.load()
        except AuthStateError:
            raise HTTPException(status_code=503, detail="Authentication unavailable") from None
        if not token_matches(payload["token"], state.token):
            raise HTTPException(status_code=401, detail="Token không hợp lệ")

        response.set_cookie(
            key=SESSION_COOKIE,
            value=create_session_cookie(state),
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        response.status_code = 204
        return response

    install_notes_routes(app, settings, chatgpt_provider, antigravity_provider)
