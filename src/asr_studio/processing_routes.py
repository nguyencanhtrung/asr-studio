"""Operator-facing automatic processing state and explicit actions."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from asr_studio.antigravity import AntigravityError, AntigravityProvider
from asr_studio.chatgpt import ChatGPTError, ChatGPTProvider
from asr_studio.config import Settings
from asr_studio.processing import (
    ProcessingError,
    apply_current_settings,
    cancel_workflow,
    processing_preferences,
    processing_state,
    retry_notes,
    save_processing_preferences,
    save_workflow_preferences,
    use_current_source,
    validate_processing_preferences,
)


async def _json(
    request: Request, required: set[str], optional: set[str] | None = None
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


def _raise(error: ProcessingError | ChatGPTError | AntigravityError) -> None:
    if isinstance(error, ProcessingError):
        raise HTTPException(
            error.status_code, detail={"code": error.code, "message": error.message}
        ) from None
    raise HTTPException(
        error.status_code, detail={"code": error.code, "message": error.public_message}
    ) from None


def install_processing_routes(
    app: FastAPI,
    settings: Settings,
    chatgpt_provider: ChatGPTProvider,
    antigravity_provider: AntigravityProvider,
) -> None:
    @app.get("/api/processing-preferences")
    async def get_processing_preferences() -> dict[str, object]:
        return await run_in_threadpool(processing_preferences, settings.data_dir)

    @app.put("/api/processing-preferences")
    async def put_processing_preferences(request: Request) -> dict[str, object]:
        payload = await _json(
            request,
            {
                "asr_choice",
                "asr_language",
                "notes_provider",
                "notes_model",
                "notes_reasoning_effort",
                "notes_output_language",
            },
        )
        try:
            provider = payload["notes_provider"]
            validated = validate_processing_preferences(
                asr_choice=payload["asr_choice"],
                asr_language=payload["asr_language"],
                notes_provider=provider,
                notes_model=payload["notes_model"],
                notes_reasoning_effort=payload["notes_reasoning_effort"],
                notes_output_language=payload["notes_output_language"],
            )
            if provider is not None:
                selected = chatgpt_provider if provider == "chatgpt" else antigravity_provider
                with selected.store.operation():
                    if provider == "chatgpt":
                        selected.save_notes_preference(
                            model=validated["notes_model"],
                            reasoning_effort=validated["notes_reasoning_effort"],
                        )
                    else:
                        selected.save_notes_preference(
                            validated["notes_model"], validated["notes_reasoning_effort"]
                        )
            return await run_in_threadpool(
                save_processing_preferences,
                settings.data_dir,
                **validated,
            )
        except (ProcessingError, ChatGPTError, AntigravityError) as error:
            _raise(error)

    @app.patch("/api/processing-preferences")
    async def patch_processing_preferences(request: Request) -> dict[str, object]:
        payload = await _json(
            request,
            {
                "expected_version",
                "asr_language",
                "notes_provider",
                "notes_output_language",
            },
        )
        try:

            def save_with_owner() -> dict[str, object]:
                provider = payload["notes_provider"]
                model = effort = None
                if provider == "chatgpt":
                    model, effort = chatgpt_provider.saved_notes_settings()
                elif provider == "antigravity":
                    model, effort = antigravity_provider.saved_notes_settings()
                return save_workflow_preferences(
                    settings.data_dir,
                    expected_version=payload["expected_version"],
                    asr_language=payload["asr_language"],
                    notes_provider=provider,
                    notes_output_language=payload["notes_output_language"],
                    notes_model=model,
                    notes_reasoning_effort=effort,
                )

            return await run_in_threadpool(save_with_owner)
        except (ProcessingError, ChatGPTError, AntigravityError) as error:
            _raise(error)

    @app.get("/api/meetings/{meeting_id}/processing")
    async def get_processing_state(meeting_id: str) -> dict[str, object]:
        state = await run_in_threadpool(processing_state, settings.data_dir, meeting_id)
        if state is None:
            raise HTTPException(
                404,
                detail={
                    "code": "processing_not_found",
                    "message": "Processing workflow not found.",
                },
            )
        return state

    @app.post("/api/meetings/{meeting_id}/processing/cancel")
    async def post_processing_cancel(meeting_id: str, request: Request) -> dict[str, object]:
        payload = await _json(request, {"request_id", "expected_pipeline_epoch"})
        try:
            return await run_in_threadpool(
                cancel_workflow,
                settings.data_dir,
                meeting_id,
                request_id=payload["request_id"],
                expected_epoch=payload["expected_pipeline_epoch"],
            )
        except ProcessingError as error:
            _raise(error)

    @app.post("/api/meetings/{meeting_id}/processing/retry-notes")
    async def post_processing_retry_notes(meeting_id: str, request: Request) -> dict[str, object]:
        payload = await _json(
            request,
            {
                "request_id",
                "expected_pipeline_epoch",
                "source_snapshot_id",
                "confirmed_recipe_version",
            },
        )
        try:
            return await run_in_threadpool(
                retry_notes,
                settings.data_dir,
                meeting_id,
                request_id=payload["request_id"],
                expected_epoch=payload["expected_pipeline_epoch"],
                source_snapshot_id=payload["source_snapshot_id"],
                confirmed_recipe_version=payload["confirmed_recipe_version"],
            )
        except ProcessingError as error:
            _raise(error)

    @app.post("/api/meetings/{meeting_id}/processing/apply-current-settings")
    async def post_processing_apply_settings(
        meeting_id: str, request: Request
    ) -> dict[str, object]:
        payload = await _json(request, {"expected_pipeline_epoch", "expected_preferences_version"})
        try:
            preferences = await run_in_threadpool(processing_preferences, settings.data_dir)
            provider_id = preferences["notes"]["provider"]
            selected = (
                (chatgpt_provider if provider_id == "chatgpt" else antigravity_provider)
                if provider_id is not None
                else None
            )
            if selected is None:
                return await run_in_threadpool(
                    apply_current_settings,
                    settings.data_dir,
                    meeting_id,
                    expected_epoch=payload["expected_pipeline_epoch"],
                    expected_preferences_version=payload["expected_preferences_version"],
                )
            with selected.store.operation():
                pin = selected.notes_task_pin()
                return await run_in_threadpool(
                    apply_current_settings,
                    settings.data_dir,
                    meeting_id,
                    expected_epoch=payload["expected_pipeline_epoch"],
                    expected_preferences_version=payload["expected_preferences_version"],
                    confirmed_pin=pin,
                )
        except ProcessingError as error:
            _raise(error)

    @app.post("/api/meetings/{meeting_id}/processing/use-current-source")
    async def post_processing_use_source(meeting_id: str, request: Request) -> dict[str, object]:
        payload = await _json(
            request,
            {
                "expected_pipeline_epoch",
                "expected_generation_id",
                "expected_source_version",
                "expected_metadata_version",
                "expected_notes_source_version",
            },
        )
        try:
            return await run_in_threadpool(
                use_current_source,
                settings.data_dir,
                meeting_id,
                expected_epoch=payload["expected_pipeline_epoch"],
                expected_generation_id=payload["expected_generation_id"],
                expected_source_version=payload["expected_source_version"],
                expected_metadata_version=payload["expected_metadata_version"],
                expected_notes_source_version=payload["expected_notes_source_version"],
            )
        except ProcessingError as error:
            _raise(error)
