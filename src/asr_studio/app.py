"""FastAPI application factory and protected-by-default request policy."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from asr_studio import __version__
from asr_studio.antigravity import (
    AntigravityAdapter,
    AntigravityProvider,
    OmpRpcAdapter,
)
from asr_studio.auth import AuthStateError, AuthStore, session_cookie_name, session_is_valid
from asr_studio.chatgpt import ChatGPTAdapter, ChatGPTHTTPAdapter, ChatGPTProvider
from asr_studio.config import CHATGPT_CALLBACK_PATH, Settings
from asr_studio.extension_auth import EXTENSION_ID, ExtensionAuthService
from asr_studio.extension_ingest import ExtensionIngestWorker, recover_extension_ingest
from asr_studio.extension_routes import install_extension_routes
from asr_studio.meeting_deletion import recover_pending_meeting_deletions
from asr_studio.notes import assert_provider_mutation_allowed
from asr_studio.processing import AutomaticProcessingScheduler
from asr_studio.processing_routes import install_processing_routes
from asr_studio.recordings import recover_recordings
from asr_studio.routes import install_routes
from asr_studio.storage import initialize

PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/access"),
        ("GET", "/assets/access.css"),
        ("GET", "/assets/access.js"),
        ("GET", "/assets/design-system.css"),
        ("GET", "/assets/i18n.js"),
        ("GET", "/assets/theme.js"),
        ("GET", "/healthz"),
        ("HEAD", "/healthz"),
        ("POST", "/api/access/session"),
        ("GET", CHATGPT_CALLBACK_PATH),
    }
)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXTENSION_V1_PREFIX = "/api/extension/v1"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"


def _normalized_origin(scheme: str, authority: str) -> tuple[str, str, int] | None:
    if not isinstance(scheme, str) or not isinstance(authority, str):
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in authority):
        return None
    try:
        parsed = urlsplit(f"{scheme.lower()}://{authority}")
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port or (443 if scheme == "https" else 80),
    )


def _host_is_expected(request: Request, settings: Settings) -> bool:
    host_header = request.headers.get("host", "")
    request.state.web_origin = None
    if request.method.upper() == "GET" and request.url.path == CHATGPT_CALLBACK_PATH:
        return settings.callback_host_matches(host_header)
    selected_origin = settings.web_origin_for_host(host_header)
    if selected_origin is None:
        return False
    request.state.web_origin = selected_origin
    return True


def _origin_is_expected(origin: str | None, expected_origin: str | None) -> bool:
    if origin is None or expected_origin is None:
        return False
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(expected_origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return _normalized_origin(parsed.scheme, parsed.netloc) == _normalized_origin(
        expected.scheme, expected.netloc
    )


def _is_extension_v1_path(path: str) -> bool:
    return path == EXTENSION_V1_PREFIX or path.startswith(f"{EXTENSION_V1_PREFIX}/")


def _extension_origin_is_expected(origin: str | None) -> bool:
    return origin == EXTENSION_ORIGIN


def _extension_response(
    request: Request,
    response: Response,
) -> Response:
    response = _with_security_headers(response)
    if _extension_origin_is_expected(request.headers.get("origin")):
        response.headers["Access-Control-Allow-Origin"] = EXTENSION_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, X-Chunk-Offset, X-Chunk-SHA256, X-Writer-Epoch"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "300"
        response.headers.add_vary_header("Origin")
    return response


def _extension_error(request: Request, code: str, message: str, status_code: int) -> Response:
    return _extension_response(
        request,
        JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code),
    )


def _with_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; media-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
    return response


def create_app(
    settings: Settings | None = None,
    *,
    chatgpt_adapter: ChatGPTAdapter | None = None,
    antigravity_adapter: AntigravityAdapter | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    auth_store = AuthStore(runtime_settings.auth_dir)
    extension_auth = ExtensionAuthService(
        runtime_settings.data_dir,
        runtime_settings.extension_server_urls,
        auth_dir=runtime_settings.auth_dir,
    )
    extension_ingest_worker = ExtensionIngestWorker(runtime_settings)
    provider_dir = runtime_settings.provider_dir
    if provider_dir is None:
        raise ValueError("ASR_PROVIDER_DIR could not be resolved")
    chatgpt_provider = ChatGPTProvider(
        provider_dir,
        callback_url=runtime_settings.chatgpt_callback_url or "",
        adapter=(chatgpt_adapter if chatgpt_adapter is not None else ChatGPTHTTPAdapter()),
        active_task_guard=lambda: assert_provider_mutation_allowed(
            runtime_settings.data_dir, "chatgpt"
        ),
    )
    antigravity_provider = AntigravityProvider(
        provider_dir,
        adapter=(
            antigravity_adapter
            if antigravity_adapter is not None
            else OmpRpcAdapter(provider_dir / "omp")
        ),
        active_task_guard=lambda: assert_provider_mutation_allowed(
            runtime_settings.data_dir, "antigravity"
        ),
    )
    processing_scheduler = AutomaticProcessingScheduler(
        runtime_settings, chatgpt_provider, antigravity_provider
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        previous_umask = os.umask(0o077)
        try:
            runtime_settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            runtime_settings.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            provider_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            auth_store.ensure()
            chatgpt_provider.ensure()
            antigravity_provider.ensure()
            initialize(runtime_settings.data_dir)
            extension_auth.ensure()
            recover_extension_ingest(runtime_settings.data_dir)
            recover_recordings(runtime_settings.data_dir)
            recover_pending_meeting_deletions(runtime_settings.data_dir)
            extension_ingest_worker.start()
            processing_scheduler.start()
            try:
                yield
            finally:
                processing_scheduler.stop()
                extension_ingest_worker.stop()
        finally:
            os.umask(previous_umask)

    app = FastAPI(
        title="ASR Studio",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth_store = auth_store
    app.state.extension_auth = extension_auth
    app.state.extension_ingest_worker = extension_ingest_worker
    app.state.settings = runtime_settings
    app.state.chatgpt_provider = chatgpt_provider
    app.state.antigravity_provider = antigravity_provider
    app.state.processing_scheduler = processing_scheduler

    @app.middleware("http")
    async def protect_requests(request: Request, call_next) -> Response:
        method = request.method.upper()
        path = request.url.path
        is_extension = _is_extension_v1_path(path)
        if is_extension:
            endpoint = runtime_settings.extension_endpoint_for_host(request.headers.get("host"))
            request.state.extension_endpoint = endpoint
            if endpoint is None:
                return _extension_error(
                    request,
                    "invalid_endpoint",
                    "Use a configured server endpoint.",
                    400,
                )
            origin = request.headers.get("origin")
            if origin is not None and not _extension_origin_is_expected(origin):
                return _extension_error(
                    request,
                    "origin_not_allowed",
                    "The extension origin is not allowed.",
                    403,
                )
            if method == "OPTIONS":
                requested_method = request.headers.get("access-control-request-method", "").upper()
                requested_headers = {
                    header.strip().lower()
                    for header in request.headers.get("access-control-request-headers", "").split(
                        ","
                    )
                    if header.strip()
                }
                if (
                    not _extension_origin_is_expected(origin)
                    or requested_method not in {"GET", "POST", "PUT"}
                    or not requested_headers.issubset(
                        {
                            "authorization",
                            "content-type",
                            "x-chunk-offset",
                            "x-chunk-sha256",
                            "x-writer-epoch",
                        }
                    )
                ):
                    return _extension_error(
                        request,
                        "cors_not_allowed",
                        "The extension CORS request is not allowed.",
                        403,
                    )
                return _extension_response(request, Response(status_code=204))
            if method in UNSAFE_METHODS and not _extension_origin_is_expected(origin):
                return _extension_error(
                    request,
                    "origin_required",
                    "The extension origin is required.",
                    403,
                )
        elif not _host_is_expected(request, runtime_settings):
            return _with_security_headers(PlainTextResponse("Invalid host", status_code=400))

        selected_origin = getattr(request.state, "web_origin", None)
        is_public = (method, path) in PUBLIC_ROUTES or (
            method in {"GET", "HEAD"} and path.startswith("/assets/fonts/")
        )
        if not is_extension and method in UNSAFE_METHODS:
            if not _origin_is_expected(request.headers.get("origin"), selected_origin):
                return _with_security_headers(
                    JSONResponse({"detail": "Same-origin request required"}, status_code=403)
                )
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return _with_security_headers(
                    JSONResponse({"detail": "Same-origin request required"}, status_code=403)
                )

        if not is_extension and not is_public:
            try:
                state = auth_store.load()
            except AuthStateError:
                return _with_security_headers(
                    JSONResponse({"detail": "Authentication unavailable"}, status_code=503)
                )
            if selected_origin is None:
                return _with_security_headers(
                    JSONResponse({"detail": "Authentication required"}, status_code=401)
                )
            cookie_name = session_cookie_name(selected_origin, runtime_settings.public_url)
            cookie_value = request.cookies.get(cookie_name)
            if not session_is_valid(
                cookie_value,
                state,
                selected_origin,
                allow_legacy=selected_origin == runtime_settings.public_url,
            ):
                if method in {"GET", "HEAD"} and not path.startswith("/api/"):
                    return _with_security_headers(RedirectResponse("/access", status_code=303))
                return _with_security_headers(
                    JSONResponse({"detail": "Authentication required"}, status_code=401)
                )

        if not is_extension and is_public and path == "/api/access/session":
            content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
            if content_type.strip().lower() != "application/json":
                return _with_security_headers(
                    JSONResponse({"detail": "JSON body required"}, status_code=415)
                )

        response = await call_next(request)
        return (
            _extension_response(request, response)
            if is_extension
            else _with_security_headers(response)
        )

    install_routes(app, runtime_settings, chatgpt_provider, antigravity_provider)
    install_processing_routes(app, runtime_settings, chatgpt_provider, antigravity_provider)
    install_extension_routes(app, runtime_settings, extension_auth)
    return app


app = create_app()
