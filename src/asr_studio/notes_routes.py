"""Authenticated notes API and assets owned by the notes integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from asr_studio.antigravity import AntigravityError, AntigravityProvider
from asr_studio.chatgpt import ChatGPTError, ChatGPTProvider
from asr_studio.config import Settings
from asr_studio.notes import (
    ABSENT,
    accept_notes_task,
    export_note,
    get_note,
    get_notes_source,
    notes_preferences,
    notes_state,
    save_notes_preference,
    update_note,
)
from asr_studio.notes_service import is_healthy as notes_service_is_healthy
from asr_studio.tasks import TaskError
from asr_studio.worker import is_healthy as worker_is_healthy
from asr_studio.worker import worker_role

WEB_DIR = Path(__file__).parent / "web"


def _notes_processing_ready(data_dir: Path) -> bool:
    if notes_service_is_healthy(data_dir):
        return True
    # A pre-upgrade combined worker remains a valid notes processor, but the
    # transcription-only worker is deliberately not treated as one.
    return worker_is_healthy(data_dir) and worker_role(data_dir) == "legacy"


async def _json_object(
    request: Request, *, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_json", "message": "Provide a valid JSON object."},
        ) from None
    allowed = required | (optional or set())
    if not isinstance(payload, dict) or not required <= set(payload) or not set(payload) <= allowed:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_request", "message": "The JSON fields are invalid."},
        )
    return payload


def _raise_task_error(error: TaskError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from None


def _raise_provider_error(error: ChatGPTError | AntigravityError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.public_message},
    ) from None


def install_notes_routes(
    app: FastAPI,
    settings: Settings,
    chatgpt_provider: ChatGPTProvider,
    antigravity_provider: AntigravityProvider,
) -> None:
    @app.get("/assets/notes.js", include_in_schema=False)
    async def notes_js() -> FileResponse:
        return FileResponse(WEB_DIR / "notes.js", media_type="text/javascript")

    @app.get("/assets/notes.css", include_in_schema=False)
    async def notes_css() -> FileResponse:
        return FileResponse(WEB_DIR / "notes.css", media_type="text/css")

    @app.post("/api/meetings/{meeting_id}/notes-runs", status_code=202)
    async def start_notes_run(meeting_id: str, request: Request) -> dict[str, object]:
        payload = await _json_object(
            request,
            required={
                "request_id",
                "expected_generation_id",
                "expected_source_version",
                "expected_metadata_version",
            },
            optional={"output_language", "provider", "expected_model", "expected_reasoning_effort"},
        )
        worker_ready = _notes_processing_ready(settings.data_dir)
        provider_id = payload.get("provider", "chatgpt")
        try:
            result = await run_in_threadpool(
                accept_notes_task,
                settings.data_dir,
                provider=antigravity_provider if provider_id == "antigravity" else chatgpt_provider,
                provider_id=provider_id,
                meeting_id=meeting_id,
                request_id=payload["request_id"],
                expected_generation_id=payload["expected_generation_id"],
                expected_source_version=payload["expected_source_version"],
                expected_metadata_version=payload["expected_metadata_version"],
                output_language=payload.get("output_language", "en"),
                expected_model=payload.get("expected_model", ABSENT),
                expected_reasoning_effort=payload.get("expected_reasoning_effort", ABSENT),
                worker_ready=worker_ready,
            )
            return result
        except TaskError as error:
            _raise_task_error(error)
        except (ChatGPTError, AntigravityError) as error:
            _raise_provider_error(error)

    @app.get("/api/notes-preferences")
    async def get_notes_preferences() -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                notes_preferences,
                settings.data_dir,
                chatgpt_provider=chatgpt_provider,
                antigravity_provider=antigravity_provider,
            )
        except (ChatGPTError, AntigravityError) as error:
            _raise_provider_error(error)

    @app.put("/api/notes-preferences")
    async def put_notes_preferences(request: Request) -> dict[str, Any]:
        payload = await _json_object(
            request, required={"provider", "model"}, optional={"reasoning_effort"}
        )
        try:
            return await run_in_threadpool(
                save_notes_preference,
                settings.data_dir,
                chatgpt_provider=chatgpt_provider,
                antigravity_provider=antigravity_provider,
                provider=payload["provider"],
                model=payload["model"],
                reasoning_effort=payload.get("reasoning_effort", "default"),
            )
        except TaskError as error:
            _raise_task_error(error)
        except (ChatGPTError, AntigravityError) as error:
            _raise_provider_error(error)

    @app.get("/api/meetings/{meeting_id}/notes")
    async def read_meeting_notes(meeting_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(notes_state, settings.data_dir, meeting_id)
        except TaskError as error:
            _raise_task_error(error)

    @app.get("/api/notes/{note_id}")
    async def read_note(note_id: str) -> dict[str, object]:
        try:
            return await run_in_threadpool(get_note, settings.data_dir, note_id)
        except TaskError as error:
            _raise_task_error(error)

    @app.patch("/api/notes/{note_id}")
    async def edit_note(note_id: str, request: Request) -> dict[str, object]:
        payload = await _json_object(request, required={"expected_edit_version", "markdown"})
        try:
            return await run_in_threadpool(
                update_note,
                settings.data_dir,
                note_id,
                expected_edit_version=payload["expected_edit_version"],
                markdown=payload["markdown"],
            )
        except TaskError as error:
            _raise_task_error(error)

    @app.get("/api/notes/{note_id}/export")
    async def export_saved_note(note_id: str) -> Response:
        try:
            markdown = await run_in_threadpool(export_note, settings.data_dir, note_id)
        except TaskError as error:
            _raise_task_error(error)
        return Response(
            content=markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="meeting-notes.md"'},
        )

    @app.get("/api/notes-sources/{source_id}")
    async def read_notes_source(source_id: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(get_notes_source, settings.data_dir, source_id)
        except TaskError as error:
            _raise_task_error(error)
