"""Operator and browser-extension routes for ``asr-extension-v1``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from asr_studio.config import (
    EXTENSION_CHUNK_BODY_IDLE_SECONDS,
    EXTENSION_CHUNK_BODY_TOTAL_SECONDS,
    Settings,
)
from asr_studio.extension_auth import (
    EXTENSION_API_VERSION,
    EXTENSION_ID,
    EXTENSION_PROTOCOL,
    MAX_ACTIVE_SESSIONS_PER_DEVICE,
    MAX_BEARER_BYTES,
    MAX_CODE_BYTES,
    MAX_DEVICE_ID_BYTES,
    MAX_PAIRING_ATTEMPTS,
    MAX_PENDING_DEVICE_RECORDS,
    ExtensionAuthError,
    ExtensionAuthService,
    authorize_extension_session,
)
from asr_studio.extension_ingest import (
    ExtensionIngestError,
    begin_chunk,
    cancel_recording_session,
    create_recording_session,
    extension_limits,
    finalize_recording_session,
    get_recording_session,
)

_JSON_BODY_BYTES = 4 * 1024
_CREATE_BODY_BYTES = 64 * 1024
_CHUNK_ERROR_MESSAGE = "The chunk could not be stored safely. Retry the same chunk."


def _error(error: ExtensionAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message}},
        status_code=error.status_code,
    )


def _internal_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "authentication_unavailable",
                "message": "Extension authentication is unavailable.",
            }
        },
        status_code=503,
    )


def _ingest_error(error: ExtensionIngestError) -> JSONResponse:
    body: dict[str, object] = {"code": error.code, "message": error.message}
    if error.details:
        body["details"] = error.details
    return JSONResponse({"error": body}, status_code=error.status_code)


def _header_integer(request: Request, name: str, *, required: bool = True) -> int | None:
    raw = request.headers.get(name)
    if raw is None:
        if required:
            raise ExtensionIngestError(400, "invalid_request", f"{name} is required.")
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        raise ExtensionIngestError(400, "invalid_request", f"{name} is invalid.") from None
    if value < 0:
        raise ExtensionIngestError(400, "invalid_request", f"{name} is invalid.")
    return value


async def _bounded_chunk_body(
    request: Request,
    *,
    max_body_bytes: int,
    on_block: Callable[[bytes], None],
) -> None:
    """Read a chunk body with idle, total, and byte budgets."""
    iterator = request.stream().__aiter__()
    deadline = asyncio.get_running_loop().time() + EXTENSION_CHUNK_BODY_TOTAL_SECONDS
    received = 0
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        try:
            block = await asyncio.wait_for(
                iterator.__anext__(),
                timeout=min(EXTENSION_CHUNK_BODY_IDLE_SECONDS, remaining),
            )
        except StopAsyncIteration:
            return
        received += len(block)
        if received > max_body_bytes:
            raise ExtensionIngestError(
                413, "chunk_too_large", "The chunk exceeds its negotiated limit."
            )
        on_block(block)


def _request_endpoint(request: Request, settings: Settings) -> str:
    endpoint = _endpoint(request, settings)
    if endpoint is None:
        raise ExtensionAuthError("invalid_endpoint", "Use a configured server endpoint.", 400)
    return endpoint


async def _authorize_ingest(
    request: Request,
    settings: Settings,
    extension_auth: ExtensionAuthService,
):
    token = _authorization(request)
    endpoint = _request_endpoint(request, settings)
    if token is None:
        raise ExtensionAuthError(
            "invalid_session_credential",
            "The session credential is invalid or no longer active.",
            401,
        )
    identity = await run_in_threadpool(
        authorize_extension_session,
        settings.data_dir,
        token,
        endpoint,
        auth_dir=settings.auth_dir,
    )
    return identity, endpoint


async def _bounded_json(request: Request, *, max_bytes: int = _JSON_BODY_BYTES) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ExtensionAuthError(
                    "request_too_large",
                    "The request body is too large.",
                    413,
                )
        except ValueError:
            raise ExtensionAuthError("invalid_request", "The JSON body is invalid.", 400) from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise ExtensionAuthError(
                "request_too_large",
                "The request body is too large.",
                413,
            )
        body.extend(chunk)
    try:
        return json.loads(bytes(body))
    except (UnicodeDecodeError, ValueError):
        raise ExtensionAuthError("invalid_request", "The JSON body is invalid.", 400) from None


async def _bounded_empty(request: Request, *, max_bytes: int = 1024) -> None:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ExtensionAuthError(
                    "request_too_large",
                    "The request body is too large.",
                    413,
                )
        except ValueError:
            raise ExtensionAuthError(
                "invalid_request", "The request body is invalid.", 400
            ) from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise ExtensionAuthError(
                "request_too_large",
                "The request body is too large.",
                413,
            )
        body.extend(chunk)
    if body:
        raise ExtensionAuthError("invalid_request", "The request body is invalid.", 400)


def _endpoint(request: Request, settings: Settings) -> str | None:
    selected = getattr(request.state, "extension_endpoint", None)
    if isinstance(selected, str):
        return selected
    return settings.extension_endpoint_for_host(request.headers.get("host"))


def _authorization(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if (
        not isinstance(header, str)
        or len(header.encode("utf-8")) > MAX_BEARER_BYTES
        or len(header) < len("Bearer ") + 1
        or header[:7].lower() != "bearer "
    ):
        return None
    token = header[7:]
    return token if token and token == token.strip() else None


def install_extension_routes(
    app: FastAPI,
    settings: Settings,
    extension_auth: ExtensionAuthService,
) -> None:
    """Install the narrow extension namespace and the cookie-protected operator API."""

    @app.get("/api/extension/v1/capabilities", response_model=None)
    async def extension_capabilities(request: Request) -> dict[str, Any] | JSONResponse:
        endpoint = _endpoint(request, settings)
        if endpoint is None:
            return _error(
                ExtensionAuthError("invalid_endpoint", "Use a configured server endpoint.", 400)
            )
        try:
            backend_id = await run_in_threadpool(extension_auth.backend_id)
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {
            "protocol": EXTENSION_PROTOCOL,
            "api_version": EXTENSION_API_VERSION,
            "prototype": False,
            "backend_id": backend_id,
            "extension_id": EXTENSION_ID,
            "server_url": endpoint,
            "server_urls": list(settings.extension_server_urls),
            "web_url": settings.public_url,
            "endpoint": "/api/extension/v1",
            "features": {"resumable_upload": True, "raw_receipt": True},
            "limits": {
                "pairing_code_seconds": 600,
                "session_seconds": 900,
                "device_id_bytes": MAX_DEVICE_ID_BYTES,
                "bearer_bytes": MAX_BEARER_BYTES,
                "pairing_code_bytes": MAX_CODE_BYTES,
                "pairing_attempts_per_window": MAX_PAIRING_ATTEMPTS,
                "max_pending_devices": MAX_PENDING_DEVICE_RECORDS,
                "max_active_sessions_per_device": MAX_ACTIVE_SESSIONS_PER_DEVICE,
                **extension_limits(settings),
            },
            "mime_types": ["audio/webm;codecs=opus"],
        }

    @app.get("/api/extension/devices", response_model=None)
    async def list_extension_devices() -> dict[str, Any] | JSONResponse:
        try:
            backend_id, devices = await run_in_threadpool(
                lambda: (extension_auth.backend_id(), extension_auth.list_devices())
            )
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {
            "backend_id": backend_id,
            "extension_id": EXTENSION_ID,
            "server_urls": list(settings.extension_server_urls),
            "devices": devices,
        }

    @app.post("/api/extension/pairing-codes", response_model=None)
    async def create_extension_pairing_code(request: Request) -> dict[str, str] | JSONResponse:
        try:
            payload = await _bounded_json(request)
            if not isinstance(payload, dict) or set(payload) - {
                "device_lifetime",
                "device_record_id",
            }:
                raise ExtensionAuthError("invalid_request", "The JSON fields are invalid.", 400)
            lifetime = payload.get("device_lifetime", "30d")
            record_id = payload.get("device_record_id")
            if record_id is not None and not isinstance(record_id, str):
                raise ExtensionAuthError("invalid_request", "The JSON fields are invalid.", 400)
            code = await run_in_threadpool(
                extension_auth.create_pairing_code,
                device_lifetime=lifetime,
                device_record_id=record_id,
            )
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {
            "code": code.code,
            "expires_at": code.expires_at,
            "device_lifetime": code.device_lifetime,
        }

    @app.post("/api/extension/devices/{device_record_id}/revoke", response_model=None)
    async def revoke_extension_device(
        request: Request, device_record_id: str
    ) -> dict[str, Any] | JSONResponse:
        try:
            await _bounded_empty(request)
            await run_in_threadpool(extension_auth.revoke_device, device_record_id)
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {"id": device_record_id, "revoked": True}

    @app.post("/api/extension/v1/pair", response_model=None)
    async def pair_extension_device(request: Request) -> dict[str, Any] | JSONResponse:
        try:
            payload = await _bounded_json(request)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"code", "device_id"}
                or not isinstance(payload["code"], str)
                or not isinstance(payload["device_id"], str)
            ):
                raise ExtensionAuthError("invalid_request", "The JSON fields are invalid.", 400)
            endpoint = _endpoint(request, settings)
            if endpoint is None:
                raise ExtensionAuthError(
                    "invalid_endpoint", "Use a configured server endpoint.", 400
                )
            credential = await run_in_threadpool(
                extension_auth.pair,
                code=payload["code"],
                device_id=payload["device_id"],
                server_url=endpoint,
            )
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {
            "device_token": credential.token,
            "device_expires_at": credential.expires_at,
            "device_lifetime": credential.device_lifetime,
            "backend_id": credential.backend_id,
        }

    @app.post("/api/extension/v1/session", response_model=None)
    async def create_extension_session(request: Request) -> dict[str, Any] | JSONResponse:
        try:
            payload = await _bounded_json(request, max_bytes=1024)
            if payload != {}:
                raise ExtensionAuthError("invalid_request", "The JSON body is invalid.", 400)
            token = _authorization(request)
            endpoint = _endpoint(request, settings)
            if token is None or endpoint is None:
                raise ExtensionAuthError(
                    "invalid_device_credential",
                    "The device credential is invalid or no longer active.",
                    401,
                )
            credential = await run_in_threadpool(
                extension_auth.create_session,
                device_token=token,
                server_url=endpoint,
            )
        except ExtensionAuthError as error:
            return _error(error)
        except Exception:
            return _internal_error()
        return {
            "session_token": credential.token,
            "expires_at": credential.expires_at,
            "backend_id": credential.backend_id,
        }

    @app.post("/api/extension/v1/recording-sessions", response_model=None)
    async def create_recording_ingest_session(request: Request) -> JSONResponse:
        try:
            payload = await _bounded_json(request, max_bytes=_CREATE_BODY_BYTES)
            identity, _ = await _authorize_ingest(request, settings, extension_auth)
            backend_id = await run_in_threadpool(extension_auth.backend_id)
            summary, status_code = await run_in_threadpool(
                create_recording_session,
                settings.data_dir,
                identity=identity,
                payload=payload,
                backend_id=backend_id,
                settings=settings,
            )
            return JSONResponse(summary, status_code=status_code)
        except ExtensionAuthError as error:
            return _error(error)
        except ExtensionIngestError as error:
            return _ingest_error(error)
        except Exception:
            return _internal_error()

    @app.put(
        "/api/extension/v1/recording-sessions/{session_id}/chunks/{sequence}",
        response_model=None,
    )
    async def write_recording_chunk(
        request: Request, session_id: str, sequence: int
    ) -> dict[str, object] | JSONResponse:
        lease = None
        try:
            identity, _ = await _authorize_ingest(request, settings, extension_auth)
            offset = _header_integer(request, "x-chunk-offset")
            epoch = _header_integer(request, "x-writer-epoch")
            if offset is None or epoch is None:
                raise ExtensionIngestError(
                    400, "invalid_request", "Chunk position headers are required."
                )
            sha256 = request.headers.get("x-chunk-sha256")
            if sha256 is None:
                raise ExtensionIngestError(400, "invalid_request", "X-Chunk-SHA256 is required.")
            declared_length = _header_integer(request, "content-length", required=False)
            lease = await run_in_threadpool(
                begin_chunk,
                settings.data_dir,
                identity=identity,
                session_id=session_id,
                sequence=sequence,
                offset=offset,
                sha256=sha256,
                epoch=epoch,
                declared_length=declared_length,
                settings=settings,
            )
            if lease.replay_ack is None:
                await _bounded_chunk_body(
                    request,
                    max_body_bytes=lease.max_chunk_bytes,
                    on_block=lease.write,
                )
            else:
                # Drain a retry body without re-writing a committed chunk. The durable file
                # and its stored digest were verified before this ACK was prepared.
                committed_length = int(lease.replay_ack["next_offset"]) - lease.offset
                await _bounded_chunk_body(
                    request,
                    max_body_bytes=min(lease.max_chunk_bytes, committed_length),
                    on_block=lambda _block: None,
                )
            return await run_in_threadpool(lease.commit)
        except ClientDisconnect:
            if lease is not None:
                lease.abort()
            return JSONResponse(
                {"error": {"code": "chunk_interrupted", "message": _CHUNK_ERROR_MESSAGE}},
                status_code=400,
            )
        except TimeoutError:
            if lease is not None:
                lease.abort()
            return _ingest_error(ExtensionIngestError(408, "chunk_timeout", _CHUNK_ERROR_MESSAGE))
        except ExtensionAuthError as error:
            return _error(error)
        except ExtensionIngestError as error:
            if lease is not None:
                lease.abort()
            return _ingest_error(error)
        except Exception:
            if lease is not None:
                lease.abort()
            return _internal_error()
        finally:
            if lease is not None and not lease.closed:
                lease.abort()

    @app.get("/api/extension/v1/recording-sessions/{session_id}", response_model=None)
    async def read_recording_ingest_session(
        request: Request, session_id: str
    ) -> dict[str, object] | JSONResponse:
        try:
            identity, _ = await _authorize_ingest(request, settings, extension_auth)
            backend_id = await run_in_threadpool(extension_auth.backend_id)
            return await run_in_threadpool(
                get_recording_session,
                settings.data_dir,
                identity=identity,
                session_id=session_id,
                backend_id=backend_id,
                settings=settings,
            )
        except ExtensionAuthError as error:
            return _error(error)
        except ExtensionIngestError as error:
            return _ingest_error(error)
        except Exception:
            return _internal_error()

    @app.post("/api/extension/v1/recording-sessions/{session_id}/finalize", response_model=None)
    async def finalize_recording_ingest_session(
        request: Request, session_id: str
    ) -> dict[str, object] | JSONResponse:
        try:
            payload = await _bounded_json(
                request,
                max_bytes=extension_limits(settings)["max_finalize_body_bytes"],
            )
            identity, _ = await _authorize_ingest(request, settings, extension_auth)
            backend_id = await run_in_threadpool(extension_auth.backend_id)
            worker = getattr(request.app.state, "extension_ingest_worker", None)
            enqueue = worker.enqueue if worker is not None else None
            summary = await run_in_threadpool(
                finalize_recording_session,
                settings.data_dir,
                identity=identity,
                session_id=session_id,
                payload=payload,
                backend_id=backend_id,
                settings=settings,
                enqueue=enqueue,
            )
            return JSONResponse(summary, status_code=202)
        except ExtensionAuthError as error:
            return _error(error)
        except ExtensionIngestError as error:
            return _ingest_error(error)
        except Exception:
            return _internal_error()

    @app.post("/api/extension/v1/recording-sessions/{session_id}/cancel", response_model=None)
    async def cancel_recording_ingest_session(
        request: Request, session_id: str
    ) -> dict[str, object] | JSONResponse:
        try:
            payload = await _bounded_json(request, max_bytes=1024)
            if not isinstance(payload, dict) or set(payload) != {"epoch"}:
                raise ExtensionAuthError("invalid_request", "The JSON fields are invalid.", 400)
            identity, _ = await _authorize_ingest(request, settings, extension_auth)
            backend_id = await run_in_threadpool(extension_auth.backend_id)
            summary = await run_in_threadpool(
                cancel_recording_session,
                settings.data_dir,
                identity=identity,
                session_id=session_id,
                epoch=payload["epoch"],
                backend_id=backend_id,
                settings=settings,
            )
            return summary
        except ExtensionAuthError as error:
            return _error(error)
        except ExtensionIngestError as error:
            return _ingest_error(error)
        except Exception:
            return _internal_error()
