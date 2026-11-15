"""Operator and browser-extension routes for ``asr-extension-v1``."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from asr_studio.config import Settings
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
)

_JSON_BODY_BYTES = 4 * 1024


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
            "endpoint": "/api/extension/v1",
            "features": {"resumable_upload": False},
            "limits": {
                "pairing_code_seconds": 600,
                "session_seconds": 900,
                "device_id_bytes": MAX_DEVICE_ID_BYTES,
                "bearer_bytes": MAX_BEARER_BYTES,
                "pairing_code_bytes": MAX_CODE_BYTES,
                "pairing_attempts_per_window": MAX_PAIRING_ATTEMPTS,
                "max_pending_devices": MAX_PENDING_DEVICE_RECORDS,
                "max_active_sessions_per_device": MAX_ACTIVE_SESSIONS_PER_DEVICE,
            },
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
