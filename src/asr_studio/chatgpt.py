"""Direct Sign in with ChatGPT, protected credentials and controlled verification."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import jwt

from asr_studio.notes_document import (
    MAX_OUTPUT_BYTES,
    MAX_PROMPT_BYTES,
    MAX_PROVIDER_CALLS,
    NOTES_ACTION_FIELD_NAMES,
    NOTES_MAX_OUTPUT_TOKENS,
    NOTES_VALIDATION_DETAIL_CODES,
    NOTES_VALIDATION_REASON_CODES,
    NotesBudget,
    NotesDocumentError,
    build_chatgpt_notes_request_body,
    estimate_notes_tokens,
    notes_budget,
    notes_provider_input_bytes,
)
from asr_studio.notes_pipeline import (
    generate_notes_document as generate_notes_pipeline,
)
from asr_studio.notes_pipeline import (
    log_notes_provider_call,
    log_notes_validation_failure,
)

ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
API_BASE_URL = "https://api.openai.com/v1"
RESOURCE = API_BASE_URL
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
REQUESTED_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
DEFAULT_REGISTRATION_NAME = "ASR Studio"
SESSION_TTL_SECONDS = 10 * 60
MAX_CALLBACK_URL_BYTES = 16 * 1024
MAX_REGISTRATION_NAME_BYTES = 200
ACCESS_REFRESH_MARGIN_SECONDS = 60
MAX_PROVIDER_STATE_BYTES = 1024 * 1024
MAX_HTTP_BODY_BYTES = 1024 * 1024
MAX_SSE_BODY_BYTES = 512 * 1024
MAX_SSE_LINE_BYTES = 64 * 1024
HTTP_CONNECT_TIMEOUT_SECONDS = 5.0
HTTP_READ_TIMEOUT_SECONDS = 15.0
HTTP_WRITE_TIMEOUT_SECONDS = 5.0
HTTP_POOL_TIMEOUT_SECONDS = 5.0
HTTP_TOTAL_TIMEOUT_SECONDS = 30.0
# HTTPX applies this to inactivity while waiting for the next response chunk. The
# per-call total budget remains 900 seconds; a silent connection must fail sooner.
NOTES_HTTP_READ_TIMEOUT_SECONDS = 60.0
NOTES_HTTP_TOTAL_TIMEOUT_SECONDS = 900.0
OPERATION_LOCK_TIMEOUT_SECONDS = 5.0
# Wire framing repeats IDs and metadata per delta; it is separate from text size.
NOTES_MAX_SSE_BODY_BYTES = NOTES_MAX_OUTPUT_TOKENS * 512 + 1024 * 1024
NOTES_MAX_SSE_LINE_BYTES = 512 * 1024
NOTES_CONTEXT_WINDOW_TOKENS = 64_000
NOTES_CONTEXT_HEADROOM_TOKENS = 4_000
NOTES_CONTEXT_INPUT_BUDGET_BYTES = (
    NOTES_CONTEXT_WINDOW_TOKENS - NOTES_MAX_OUTPUT_TOKENS - NOTES_CONTEXT_HEADROOM_TOKENS
)
NOTES_MAX_REQUEST_BODY_BYTES = 128 * 1024
_RECONNECT_ERROR_CODES = frozenset(
    {
        "provider_auth_error",
        "subscription_sharing_invalid_user",
        "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
    }
)
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{5,256}\Z")
_SAFE_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_ALL_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_POSITIVE_REASONING_EFFORTS = _ALL_REASONING_EFFORTS[1:]
_NOTES_VALIDATION_STAGES = frozenset({"single", "chunk", "reduce", "final", "audit", "reconcile"})
_NOTES_VALIDATION_ATTEMPTS = frozenset({"primary", "repair", "recovery"})
_NOTES_REPAIR_STATES = frozenset({"unavailable", "already_consumed"})


def _notes_sse_body_limit(budget: NotesBudget) -> int:
    if budget.uses_token_estimate:
        return budget.output_reserve_tokens * 512 + 1024 * 1024
    return NOTES_MAX_SSE_BODY_BYTES


def _notes_usage_counts(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    observed: dict[str, int] = {}
    for source_name, log_name in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
    ):
        value = usage.get(source_name)
        if type(value) is int and value >= 0:
            observed[log_name] = value
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        value = output_details.get("reasoning_tokens")
        if type(value) is int and value >= 0:
            observed["reasoning_tokens"] = value
    return observed


def _bounded_diagnostic_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if type(value) is int and minimum <= value <= maximum:
        return value
    return None


def _safe_notes_call_diagnostics(
    diagnostics: object, *, budget: NotesBudget | None = None
) -> dict[str, Any]:
    if not isinstance(diagnostics, dict):
        return {}
    selected_budget = notes_budget() if budget is None else budget
    prompt_limit = (
        selected_budget.request_guard_bytes
        if selected_budget.uses_token_estimate
        else MAX_PROMPT_BYTES
    )
    sse_limit = _notes_sse_body_limit(selected_budget)
    result: dict[str, Any] = {}
    stage = diagnostics.get("stage")
    attempt = diagnostics.get("attempt")
    repair_state = diagnostics.get("repair_state")
    if isinstance(stage, str) and stage in _NOTES_VALIDATION_STAGES:
        result["stage"] = stage
    if isinstance(attempt, str) and attempt in _NOTES_VALIDATION_ATTEMPTS:
        result["attempt"] = attempt
    bounded_fields = (
        ("provider_call_index", 1, MAX_PROVIDER_CALLS),
        ("prompt_bytes", 0, prompt_limit),
        ("response_limit", 1, MAX_OUTPUT_BYTES),
        ("output_bytes", 0, sse_limit),
    )
    for field, minimum, maximum in bounded_fields:
        value = _bounded_diagnostic_int(diagnostics.get(field), minimum=minimum, maximum=maximum)
        if value is not None:
            result[field] = value
    if isinstance(repair_state, str) and repair_state in _NOTES_REPAIR_STATES:
        result["repair_state"] = repair_state
    body_shape = diagnostics.get("body_shape")
    if isinstance(body_shape, str) and body_shape in {"non_json", "detail", "error"}:
        result["body_shape"] = body_shape
    http_status = _bounded_diagnostic_int(diagnostics.get("http_status"), minimum=100, maximum=599)
    if http_status is not None:
        result["http_status"] = http_status
    request_id = diagnostics.get("request_id")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
        result["request_id"] = request_id
    param = diagnostics.get("param")
    if isinstance(param, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,200}", param):
        result["param"] = param
    return result


def _safe_notes_validation_diagnostics(
    diagnostics: object, *, budget: NotesBudget | None = None
) -> dict[str, Any]:
    if not isinstance(diagnostics, dict):
        return {}
    result = _safe_notes_call_diagnostics(diagnostics, budget=budget)
    reason = diagnostics.get("validation_reason")
    detail = diagnostics.get("validation_detail")
    field_name = diagnostics.get("validation_field")
    if isinstance(reason, str) and reason in NOTES_VALIDATION_REASON_CODES:
        result["validation_reason"] = reason
    if (
        reason == "source_ids"
        and isinstance(detail, str)
        and detail in NOTES_VALIDATION_DETAIL_CODES
    ):
        result["validation_detail"] = detail
    if (
        reason == "action_fields"
        and isinstance(field_name, str)
        and field_name in NOTES_ACTION_FIELD_NAMES
    ):
        result["validation_field"] = field_name
    return result


def reasoning_efforts_for_model(model: str) -> tuple[str, ...]:
    """Expose only effort values documented for known reasoning model families."""
    if model == "gpt-5.6" or model.startswith("gpt-5.6-"):
        return _ALL_REASONING_EFFORTS
    if model in {"gpt-6-luna", "gpt-6-sol"} or model.startswith(("gpt-6-luna-", "gpt-6-sol-")):
        return _ALL_REASONING_EFFORTS
    if model in {"gpt-6.1-sol", "gpt-6-astra"} or model.startswith(
        ("gpt-6.1-sol-", "gpt-6-astra-")
    ):
        return _POSITIVE_REASONING_EFFORTS
    return ()


def validate_reasoning_effort(model: str, effort: str) -> None:
    if effort != "default" and effort not in reasoning_efforts_for_model(model):
        raise ChatGPTError(
            400, "invalid_reasoning_effort", "Choose an effort supported by this model."
        )


_TERMINAL_REFRESH_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


class ChatGPTError(RuntimeError):
    """A safe provider failure that can be returned to the local UI."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = 409 if status_code == 401 else status_code
        self.code = code if _SAFE_ERROR_CODE_RE.fullmatch(code) else "provider_error"
        self.message = message
        self.diagnostics: dict[str, Any] = {}

    @property
    def public_message(self) -> str:
        return ChatGPTHTTPAdapter._message_for(self.status_code, self.code, self.diagnostics)


class ProviderStateError(ChatGPTError):
    """The dedicated provider credential store cannot be used safely."""

    def __init__(self, diagnostic: str = "Provider credential storage is unavailable") -> None:
        self.diagnostic = diagnostic
        super().__init__(
            503, "provider_storage_unavailable", "ChatGPT connection storage is unavailable."
        )


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    subject: str
    email: str | None
    email_verified: bool


class ChatGPTAdapter(Protocol):
    """Network boundary for the provider service; tests inject a deterministic adapter."""

    def exchange_code(
        self, *, code: str, verifier: str, redirect_uri: str, client_id: str
    ) -> dict[str, Any]: ...

    def validate_identity(
        self, id_token: str, *, client_id: str, nonce: str
    ) -> VerifiedIdentity: ...

    def refresh(self, *, client_id: str, refresh_token: str) -> dict[str, Any]: ...

    def list_models(self, *, access_token: str) -> list[dict[str, Any]]: ...

    def verify(
        self, *, access_token: str, model: str, reasoning_effort: str = "default"
    ) -> None: ...

    def generate_notes(
        self,
        *,
        access_token: str,
        model: str,
        reasoning_effort: str,
        prompt: str,
        allowed_source_ids: Sequence[str],
        max_output_bytes: int | None = None,
        budget: NotesBudget | None = None,
        max_unit_size: int = 1,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str: ...

    def revoke(self, *, client_id: str, refresh_token: str) -> bool: ...


class ProviderStore:
    """Owner-only atomic state, with separate state and cross-process use locks."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.state_path = self.directory / "chatgpt.json"
        self.state_lock_path = self.directory / ".chatgpt-state.lock"
        self.operation_lock_path = self.directory / ".chatgpt-operation.lock"

    def _prepare_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.directory.is_symlink():
                raise ProviderStateError("Provider storage directory must not be a symbolic link")
            os.chmod(self.directory, 0o700)
        except (OSError, ProviderStateError) as exc:
            raise ProviderStateError("Provider credential storage is unavailable") from exc

    @contextmanager
    def _file_lock(self, path: Path, *, timeout: float | None = None) -> Iterator[None]:
        self._prepare_directory()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ProviderStateError("Provider credential lock is unavailable") from exc
        acquired = False
        try:
            try:
                os.fchmod(descriptor, 0o600)
                if timeout is None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                else:
                    deadline = time.monotonic() + max(0.0, timeout)
                    while True:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ChatGPTError(
                                    503,
                                    "provider_busy",
                                    "Another ChatGPT operation is still running. "
                                    "Try again shortly.",
                                ) from None
                            time.sleep(min(0.01, remaining))
                acquired = True
            except OSError as exc:
                raise ProviderStateError("Provider credential lock is unavailable") from exc
            yield
        finally:
            if acquired:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": 1,
            "host_id": f"urn:uuid:{uuid.uuid4()}",
            "connection_status": "disconnected",
            "connection_version": 0,
            "active_client_id": None,
            "registrations": {},
            "sessions": {},
            "notes_provider": None,
            "selected_model": None,
            "selected_reasoning_effort": "default",
            "verification": None,
        }

    def _read(self) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.state_path, flags)
        except OSError as exc:
            raise ProviderStateError("Provider credential state is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProviderStateError("Provider credential state is not a regular file")
            if metadata.st_mode & 0o077:
                raise ProviderStateError("Provider credential state permissions are too broad")
            if metadata.st_size > MAX_PROVIDER_STATE_BYTES:
                raise ProviderStateError("Provider credential state is too large")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(descriptor, 64 * 1024):
                total += len(chunk)
                if total > MAX_PROVIDER_STATE_BYTES:
                    raise ProviderStateError("Provider credential state is too large")
                chunks.append(chunk)
            document = json.loads(b"".join(chunks))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderStateError("Provider credential state is invalid") from exc
        finally:
            os.close(descriptor)
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("host_id"), str)
            or not document["host_id"].startswith("urn:uuid:")
            or document.get("connection_status")
            not in {"disconnected", "connected", "reconnect_required"}
            or not isinstance(document.get("connection_version"), int)
            or not isinstance(document.get("registrations"), dict)
            or not isinstance(document.get("sessions"), dict)
        ):
            raise ProviderStateError("Provider credential state is invalid")
        return document

    def _write(self, state: dict[str, Any]) -> None:
        try:
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_PROVIDER_STATE_BYTES:
                raise ProviderStateError("Provider credential state is too large")
            descriptor, temporary_name = tempfile.mkstemp(prefix=".chatgpt-", dir=self.directory)
            temporary_path = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as file_handle:
                    file_handle.write(payload)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                os.replace(temporary_path, self.state_path)
                directory_descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                temporary_path.unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError) as exc:
            raise ProviderStateError("Provider credential state could not be saved") from exc

    @contextmanager
    def locked(self, *, write: bool = False) -> Iterator[dict[str, Any]]:
        with self._file_lock(self.state_lock_path):
            state = self._read()
            yield state
            if write:
                self._write(state)

    @contextmanager
    def operation(self, *, timeout: float | None = None) -> Iterator[None]:
        wait_seconds = OPERATION_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        with self._file_lock(self.operation_lock_path, timeout=wait_seconds):
            yield

    def ensure(self) -> None:
        with self._file_lock(self.state_lock_path):
            if self.state_path.exists() or self.state_path.is_symlink():
                self._read()
                return
            self._write(self._empty_state())


class ChatGPTHTTPAdapter:
    """Fixed-endpoint HTTP implementation for Sign in with ChatGPT."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._discovery_document: dict[str, Any] | None = None
        self._discovery_lock = threading.Lock()
        self._notes_usage_local = threading.local()

    @property
    def last_notes_usage(self) -> dict[str, int]:
        """Return usage isolated to the current notes transport thread."""
        if not hasattr(self, "_notes_usage_local"):
            self._notes_usage_local = threading.local()
        usage = getattr(self._notes_usage_local, "value", None)
        if not isinstance(usage, dict):
            usage = {}
            self._notes_usage_local.value = usage
        return usage

    @last_notes_usage.setter
    def last_notes_usage(self, value: object) -> None:
        if not hasattr(self, "_notes_usage_local"):
            self._notes_usage_local = threading.local()
        self._notes_usage_local.value = value if isinstance(value, dict) else {}

    @contextmanager
    def _client(self, *, read_timeout: float | None = None) -> Iterator[httpx.Client]:
        timeout = httpx.Timeout(
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
            read=HTTP_READ_TIMEOUT_SECONDS if read_timeout is None else read_timeout,
            write=HTTP_WRITE_TIMEOUT_SECONDS,
            pool=HTTP_POOL_TIMEOUT_SECONDS,
        )
        with httpx.Client(
            transport=self._transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield client

    @staticmethod
    def _read_limited(
        response: httpx.Response,
        *,
        limit: int = MAX_HTTP_BODY_BYTES,
        deadline: float | None = None,
    ) -> bytes:
        ChatGPTHTTPAdapter._check_deadline(deadline)
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > limit:
                    raise ChatGPTError(
                        502,
                        "provider_response_too_large",
                        "ChatGPT returned an oversized response.",
                    )
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes():
            ChatGPTHTTPAdapter._check_deadline(deadline)
            if len(body) + len(chunk) > limit:
                raise ChatGPTError(
                    502, "provider_response_too_large", "ChatGPT returned an oversized response."
                )
            body.extend(chunk)
        ChatGPTHTTPAdapter._check_deadline(deadline)
        return bytes(body)

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ChatGPTError(503, "provider_timeout", "ChatGPT did not respond in time.")

    @staticmethod
    def _json_document(raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatGPTError(
                502, "provider_invalid_response", "ChatGPT returned an invalid response."
            ) from None
        if not isinstance(value, dict):
            raise ChatGPTError(
                502, "provider_invalid_response", "ChatGPT returned an invalid response."
            )
        return value

    @staticmethod
    def _message_for(status_code: int, code: str, diagnostics: dict[str, Any] | None = None) -> str:
        if code in {"subscription_sharing_usage_limit_exceeded"} or status_code == 429:
            return "ChatGPT plan usage is limited. Review ChatGPT Settings → Usage."
        if code in {
            "subscription_sharing_user_not_eligible",
            "subscription_sharing_route_not_supported",
        }:
            return "ChatGPT plan use is unavailable for this account, workspace, or request."
        if code in {
            "chatpass_v2_scope_not_authorized",
            "chatpass_v2_invalid_authorization_context",
        }:
            return "ChatGPT did not grant permission for plan use. Sign in again to review access."
        if code == "provider_busy":
            return "Another ChatGPT operation is still running. Try again shortly."
        if code == "provider_output_too_large":
            return "ChatGPT's response exceeded the allowed output size."
        if code == "provider_stream_too_large":
            return "ChatGPT's response exceeded the allowed transfer size."
        if code == "provider_stream_line_too_large":
            return "ChatGPT sent a response line that exceeded the allowed size."
        if code == "provider_invalid_stream":
            return "ChatGPT returned a response in an invalid format."
        if code == "provider_timeout":
            return "ChatGPT did not finish responding in time."
        if code in {"notes_request_too_large", "notes_context_budget_exceeded"}:
            return "This transcript exceeds the safe ChatGPT notes request limit."
        if code == "notes_token_budget_exceeded":
            return (
                "ChatGPT exhausted the token budget before completing notes. "
                "No new notes were saved."
            )
        if code == "notes_content_filtered":
            return (
                "ChatGPT stopped notes generation because of a content filter. "
                "No new notes were saved."
            )
        if code == "notes_incomplete":
            return "ChatGPT did not complete the notes response. No new notes were saved."
        if code == "notes_invalid_output":
            reason = diagnostics.get("validation_reason") if isinstance(diagnostics, dict) else None
            if not isinstance(reason, str) or reason not in NOTES_VALIDATION_REASON_CODES:
                return "ChatGPT returned invalid notes. No new notes were saved."
            if reason == "json":
                return "ChatGPT returned notes with invalid JSON. No new notes were saved."
            if reason in {"fields", "version", "items", "text", "action_fields"}:
                return "ChatGPT returned notes with an invalid structure. No new notes were saved."
            if reason == "source_ids":
                return (
                    "ChatGPT returned notes with invalid source references. "
                    "No new notes were saved."
                )
            if reason == "size":
                return (
                    "ChatGPT returned notes that exceed the output budget. No new notes were saved."
                )
            return "ChatGPT returned invalid notes. No new notes were saved."
        if code == "subscription_sharing_unsupported_capability":
            return "ChatGPT does not support this request for the selected model."
        if status_code in {400, 422}:
            return "ChatGPT rejected this request. Check the selected model or permission."
        if status_code == 403:
            return "ChatGPT denied this request. Check account or workspace permissions."
        if status_code == 503:
            return "ChatGPT plan use is temporarily unavailable."
        if status_code >= 500:
            return "ChatGPT is temporarily unavailable."
        if status_code == 409:
            return (
                "ChatGPT could not complete this request. Sign in again if the problem continues."
            )
        return "ChatGPT could not complete this request."

    @classmethod
    def _http_error(cls, response: httpx.Response, body: bytes) -> ChatGPTError:
        code = "provider_http_error"
        document: object = None
        try:
            document = json.loads(body)
            if isinstance(document, dict):
                error = document.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
                elif isinstance(error, str):
                    code = error
                elif isinstance(document.get("detail"), str):
                    code = "provider_admission_denied"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not _SAFE_ERROR_CODE_RE.fullmatch(code):
            code = "provider_http_error"
        if response.status_code == 401 and code not in _TERMINAL_REFRESH_CODES | {
            "invalid_client",
            "subscription_sharing_invalid_user",
        }:
            code = "provider_auth_error"
        status = 409 if response.status_code == 401 else response.status_code
        if not 400 <= status <= 599:
            status = 502
        failure = ChatGPTError(status, code, cls._message_for(response.status_code, code))
        failure.diagnostics = cls._diagnostics(response, document)
        return failure

    @staticmethod
    def _diagnostics(response: httpx.Response | None, document: object) -> dict[str, Any]:
        # Preserve correlation and field names, never provider prose or raw payloads.
        result: dict[str, Any] = {"body_shape": "non_json"}
        if isinstance(document, dict):
            result["body_shape"] = "detail" if "detail" in document else "error"
            error = document.get("error", document)
            param = error.get("param") if isinstance(error, dict) else None
            if isinstance(param, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,200}", param):
                result["param"] = param
        if response is not None:
            result["http_status"] = response.status_code
            request_id = response.headers.get("x-request-id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
                result["request_id"] = request_id
        return result

    @classmethod
    def _json_request(
        cls,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + HTTP_TOTAL_TIMEOUT_SECONDS
        try:
            with client.stream(method, url, headers=headers, data=data, json=json_body) as response:
                body = cls._read_limited(response, deadline=deadline)
        except httpx.TimeoutException:
            raise ChatGPTError(
                503, "provider_timeout", "ChatGPT did not respond in time."
            ) from None
        except httpx.HTTPError:
            raise ChatGPTError(
                503, "provider_unavailable", "ChatGPT could not be reached."
            ) from None
        if not 200 <= response.status_code < 300:
            raise cls._http_error(response, body)
        return cls._json_document(body)

    def _discovery(self, client: httpx.Client) -> dict[str, Any]:
        with self._discovery_lock:
            if self._discovery_document is not None:
                return self._discovery_document
            document = self._json_request(client, "GET", DISCOVERY_URL)
            if (
                document.get("issuer") != ISSUER
                or document.get("authorization_endpoint") != AUTHORIZATION_URL
                or document.get("token_endpoint") != TOKEN_URL
                or not isinstance(document.get("id_token_signing_alg_values_supported"), list)
                or "RS256" not in document["id_token_signing_alg_values_supported"]
            ):
                raise ChatGPTError(
                    502, "provider_discovery_invalid", "ChatGPT sign-in configuration is invalid."
                )
            for name in ("jwks_uri", "revocation_endpoint"):
                endpoint = document.get(name)
                try:
                    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
                    port = parsed.port if parsed else None
                except ValueError:
                    parsed = None
                    port = None
                if (
                    parsed is None
                    or parsed.scheme != "https"
                    or parsed.hostname != "auth.openai.com"
                    or parsed.username is not None
                    or parsed.password is not None
                    or port not in {None, 443}
                    or parsed.fragment
                ):
                    raise ChatGPTError(
                        502,
                        "provider_discovery_invalid",
                        "ChatGPT sign-in configuration is invalid.",
                    )
            self._discovery_document = document
            return document

    def exchange_code(
        self, *, code: str, verifier: str, redirect_uri: str, client_id: str
    ) -> dict[str, Any]:
        with self._client() as client:
            return self._json_request(
                client,
                "POST",
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                    "resource": RESOURCE,
                },
            )

    def validate_identity(self, id_token: str, *, client_id: str, nonce: str) -> VerifiedIdentity:
        if len(id_token) > 64 * 1024:
            raise ChatGPTError(
                502, "invalid_identity_token", "ChatGPT returned an invalid identity token."
            )
        try:
            header = jwt.get_unverified_header(id_token)
            if (
                header.get("alg") != "RS256"
                or "jku" in header
                or "x5u" in header
                or "crit" in header
                or not isinstance(header.get("kid"), str)
                or len(header["kid"]) > 256
            ):
                raise jwt.InvalidTokenError("Unsupported identity token header")
            with self._client() as client:
                discovery = self._discovery(client)
                jwks = self._json_request(client, "GET", discovery["jwks_uri"])
            keys = jwks.get("keys")
            if not isinstance(keys, list):
                raise jwt.InvalidTokenError("Invalid JWKS")
            jwk_document = next(
                (
                    item
                    for item in keys
                    if isinstance(item, dict)
                    and item.get("kid") == header["kid"]
                    and item.get("kty") == "RSA"
                    and item.get("use", "sig") == "sig"
                    and item.get("alg", "RS256") == "RS256"
                ),
                None,
            )
            if jwk_document is None:
                raise jwt.InvalidTokenError("Signing key is unavailable")
            signing_key = jwt.PyJWK.from_dict(jwk_document, algorithm="RS256").key
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "sub", "nonce"]},
            )
            if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
                claims["nonce"], nonce
            ):
                raise jwt.InvalidTokenError("Nonce mismatch")
            subject = claims.get("sub")
            email = claims.get("email")
            if not isinstance(subject, str) or not subject or len(subject) > 2048:
                raise jwt.InvalidTokenError("Subject is invalid")
            if email is not None and (not isinstance(email, str) or len(email) > 320):
                email = None
            return VerifiedIdentity(subject, email, claims.get("email_verified") is True)
        except ChatGPTError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise ChatGPTError(
                502, "invalid_identity_token", "ChatGPT returned an invalid identity token."
            ) from None

    def refresh(self, *, client_id: str, refresh_token: str) -> dict[str, Any]:
        with self._client() as client:
            return self._json_request(
                client,
                "POST",
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "resource": RESOURCE,
                },
            )

    def list_models(self, *, access_token: str) -> list[dict[str, Any]]:
        with self._client() as client:
            document = self._json_request(
                client,
                "GET",
                f"{API_BASE_URL}/models",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        models = document.get("models")
        if not isinstance(models, list):
            raise ChatGPTError(
                502, "provider_invalid_model_catalog", "ChatGPT returned an invalid model catalog."
            )
        return models

    def verify(self, *, access_token: str, model: str, reasoning_effort: str = "default") -> None:
        body: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": 'Return exactly {"ok":true}.'}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "connection_check",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                }
            },
            "store": False,
            "stream": True,
        }
        if reasoning_effort != "default":
            body["reasoning"] = {"effort": reasoning_effort}
        completed = False
        delta_text: list[str] = []
        total_text = 0
        with self._client() as client:
            deadline = time.monotonic() + HTTP_TOTAL_TIMEOUT_SECONDS
            try:
                with client.stream(
                    "POST",
                    f"{API_BASE_URL}/responses",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        error_body = self._read_limited(response, deadline=deadline)
                        raise self._http_error(response, error_body)
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", maxsplit=1)[0]
                        .strip()
                        .lower()
                    )
                    if "content-type" in response.headers and content_type != "text/event-stream":
                        raise ChatGPTError(
                            502,
                            "provider_invalid_stream",
                            "ChatGPT returned an invalid verification stream.",
                        )
                    for event in self._sse_events(response, deadline=deadline):
                        event_type = event.get("type")
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise ChatGPTError(
                                    502,
                                    "provider_invalid_stream",
                                    "ChatGPT returned an invalid verification stream.",
                                )
                            total_text += len(delta.encode("utf-8"))
                            if total_text > 16 * 1024:
                                raise ChatGPTError(
                                    502,
                                    "provider_output_too_large",
                                    "ChatGPT returned an oversized verification result.",
                                )
                            delta_text.append(delta)
                        elif event_type == "response.failed":
                            error = (
                                event.get("response", {}).get("error")
                                if isinstance(event.get("response"), dict)
                                else None
                            )
                            raise self._sse_error(error)
                        elif event_type == "error":
                            raise self._sse_error(event.get("error", event))
                        elif event_type == "response.incomplete":
                            raise ChatGPTError(
                                502,
                                "verification_incomplete",
                                "ChatGPT did not complete the verification request.",
                            )
                        elif event_type == "response.completed":
                            if completed:
                                raise ChatGPTError(
                                    502,
                                    "provider_invalid_stream",
                                    "ChatGPT returned an invalid verification stream.",
                                )
                            completed = True
                            if not delta_text:
                                output_text = self._completed_output_text(event)
                                delta_text.extend(output_text)
                            break
            except ChatGPTError as error:
                if "response" in locals():
                    correlation = self._diagnostics(response, None)
                    correlation.update(error.diagnostics)
                    error.diagnostics = correlation
                raise
            except httpx.TimeoutException:
                raise ChatGPTError(
                    503, "provider_timeout", "ChatGPT did not complete the verification request."
                ) from None
            except httpx.HTTPError:
                raise ChatGPTError(
                    503,
                    "provider_unavailable",
                    "ChatGPT could not complete the verification request.",
                ) from None
        if not completed:
            raise ChatGPTError(
                502, "verification_incomplete", "ChatGPT did not complete the verification request."
            )
        try:
            result = json.loads("".join(delta_text))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatGPTError(
                502,
                "verification_invalid_output",
                "ChatGPT returned an invalid verification result.",
            ) from None
        if not isinstance(result, dict) or set(result) != {"ok"} or result["ok"] is not True:
            raise ChatGPTError(
                502,
                "verification_invalid_output",
                "ChatGPT returned an invalid verification result.",
            )

    def generate_notes(
        self,
        *,
        access_token: str,
        model: str,
        reasoning_effort: str,
        prompt: str,
        allowed_source_ids: Sequence[str],
        max_output_bytes: int | None = None,
        budget: NotesBudget | None = None,
        max_unit_size: int = 1,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str:
        """Request one strict JSON notes document within its per-stage output budget.

        `response_schema` replaces notes schema 1 for an internal pipeline stage; every size
        guard below measures the schema actually sent.
        """
        from asr_studio.notes_document import (
            MAX_OUTPUT_BYTES,
            MAX_PROMPT_BYTES,
            NOTES_CONTEXT_HEADROOM_TOKENS,
            NotesDocumentError,
            build_notes_schema,
            estimate_notes_tokens,
            notes_budget,
        )

        selected_budget = notes_budget("chatgpt", model) if budget is None else budget
        self.last_notes_usage = {}
        response_limit = MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
        if (
            isinstance(response_limit, bool)
            or not isinstance(response_limit, int)
            or not 1 <= response_limit <= MAX_OUTPUT_BYTES
        ):
            raise ChatGPTError(
                413,
                "notes_request_too_large",
                "The notes request exceeds the configured size limit.",
            )
        try:
            schema = (
                dict(response_schema)
                if response_schema is not None
                else build_notes_schema(allowed_source_ids, max_unit_size=max_unit_size)
            )
            prompt_bytes = prompt.encode("utf-8")
            schema_bytes = json.dumps(
                schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (NotesDocumentError, UnicodeEncodeError, TypeError, ValueError):
            raise ChatGPTError(
                413,
                "notes_request_too_large",
                "The notes request exceeds the configured size limit.",
            ) from None
        prompt_limit = (
            selected_budget.request_guard_bytes
            if selected_budget.uses_token_estimate
            else MAX_PROMPT_BYTES
        )
        if len(prompt_bytes) > prompt_limit:
            raise ChatGPTError(
                413,
                "notes_request_too_large",
                "The notes request exceeds the configured size limit.",
            )

        body = build_chatgpt_notes_request_body(
            model=model,
            prompt=prompt,
            schema=schema,
            reasoning_effort=reasoning_effort,
            budget=selected_budget,
        )

        if selected_budget.uses_token_estimate:
            context_fits = (
                estimate_notes_tokens(len(prompt_bytes) + len(schema_bytes), selected_budget)
                + selected_budget.output_reserve_tokens
                + NOTES_CONTEXT_HEADROOM_TOKENS
                <= selected_budget.usable_context_tokens
            )
        else:
            # Preserve the conservative byte/token convention for every other model.
            context_bytes = len(prompt_bytes) + len(schema_bytes) + 1024
            context_fits = context_bytes <= NOTES_CONTEXT_INPUT_BUDGET_BYTES
        if not context_fits:
            raise ChatGPTError(
                413,
                "notes_context_budget_exceeded",
                "The notes request exceeds the safe context budget.",
            )
        try:
            request_body = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (UnicodeEncodeError, TypeError, ValueError):
            raise ChatGPTError(
                413,
                "notes_request_too_large",
                "The notes request exceeds the configured size limit.",
            ) from None
        request_body_limit = (
            selected_budget.request_guard_bytes
            if selected_budget.uses_token_estimate
            else NOTES_MAX_REQUEST_BODY_BYTES
        )
        if len(request_body) > request_body_limit:
            raise ChatGPTError(
                413,
                "notes_request_too_large",
                "The notes request exceeds the configured size limit.",
            )

        completed = False
        delta_text: list[str] = []
        total_text = 0
        response: httpx.Response | None = None

        def with_observed_output_bytes(error: ChatGPTError) -> ChatGPTError:
            diagnostics = error.diagnostics if isinstance(error.diagnostics, dict) else {}
            error.diagnostics = {**diagnostics, "output_bytes": total_text}
            return error

        with self._client(read_timeout=NOTES_HTTP_READ_TIMEOUT_SECONDS) as client:
            deadline = time.monotonic() + NOTES_HTTP_TOTAL_TIMEOUT_SECONDS
            try:
                with client.stream(
                    "POST",
                    f"{API_BASE_URL}/responses",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                    },
                    content=request_body,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        error_body = self._read_limited(response, deadline=deadline)
                        raise self._http_error(response, error_body)
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", maxsplit=1)[0]
                        .strip()
                        .lower()
                    )
                    if "content-type" in response.headers and content_type != "text/event-stream":
                        raise ChatGPTError(
                            502,
                            "provider_invalid_stream",
                            "ChatGPT returned an invalid notes stream.",
                        )
                    for event in self._sse_events(
                        response,
                        deadline=deadline,
                        max_body_bytes=_notes_sse_body_limit(selected_budget),
                        max_line_bytes=NOTES_MAX_SSE_LINE_BYTES,
                    ):
                        event_type = event.get("type")
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise ChatGPTError(
                                    502,
                                    "provider_invalid_stream",
                                    "ChatGPT returned an invalid notes stream.",
                                )
                            total_text += len(delta.encode("utf-8"))
                            if total_text > response_limit:
                                raise with_observed_output_bytes(
                                    ChatGPTError(
                                        502,
                                        "provider_output_too_large",
                                        "ChatGPT returned an oversized notes document.",
                                    )
                                )
                            delta_text.append(delta)
                        elif event_type == "response.failed":
                            error = (
                                event.get("response", {}).get("error")
                                if isinstance(event.get("response"), dict)
                                else None
                            )
                            raise self._sse_error(error)
                        elif event_type == "error":
                            raise self._sse_error(event.get("error", event))
                        elif event_type == "response.incomplete":
                            result = event.get("response")
                            usage = result.get("usage") if isinstance(result, dict) else None
                            self.last_notes_usage = _notes_usage_counts(usage)
                            details = (
                                result.get("incomplete_details")
                                if isinstance(result, dict)
                                else None
                            )
                            reason = details.get("reason") if isinstance(details, dict) else None
                            code = (
                                {
                                    "max_output_tokens": "notes_token_budget_exceeded",
                                    "content_filter": "notes_content_filtered",
                                }.get(reason, "notes_incomplete")
                                if isinstance(reason, str)
                                else "notes_incomplete"
                            )
                            raise ChatGPTError(
                                502,
                                code,
                                "ChatGPT did not complete the notes request.",
                            )
                        elif event_type == "response.completed":
                            if completed:
                                raise ChatGPTError(
                                    502,
                                    "provider_invalid_stream",
                                    "ChatGPT returned an invalid notes stream.",
                                )
                            completed = True
                            response_data = event.get("response")
                            usage = (
                                response_data.get("usage")
                                if isinstance(response_data, dict)
                                else None
                            )
                            self.last_notes_usage = _notes_usage_counts(usage)
                            if not delta_text:
                                completed_text = self._completed_output_text(event)
                                delta_text.extend(completed_text)
                                total_text = sum(
                                    len(part.encode("utf-8")) for part in completed_text
                                )
                            break
            except ChatGPTError as error:
                with_observed_output_bytes(error)
                if response is not None:
                    correlation = self._diagnostics(response, None)
                    correlation.update(error.diagnostics)
                    error.diagnostics = correlation
                raise
            except httpx.TimeoutException:
                raise with_observed_output_bytes(
                    ChatGPTError(
                        503, "provider_timeout", "ChatGPT did not complete the notes request."
                    )
                ) from None
            except httpx.HTTPError:
                raise with_observed_output_bytes(
                    ChatGPTError(
                        503, "provider_unavailable", "ChatGPT could not complete the notes request."
                    )
                ) from None
        if not completed:
            raise with_observed_output_bytes(
                ChatGPTError(502, "notes_incomplete", "ChatGPT did not complete the notes request.")
            )
        result = "".join(delta_text)
        if len(result.encode("utf-8")) > response_limit:
            raise with_observed_output_bytes(
                ChatGPTError(
                    502,
                    "provider_output_too_large",
                    "ChatGPT returned an oversized notes document.",
                )
            )
        return result

    @staticmethod
    def _completed_output_text(event: dict[str, Any]) -> list[str]:
        response = event.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("output"), list):
            return []
        text_parts = []
        for item in response["output"]:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    text_parts.append(part["text"])
        return text_parts

    @staticmethod
    def _sse_error(error: object) -> ChatGPTError:
        code = "provider_inference_error"
        status = 502
        if isinstance(error, dict):
            if isinstance(error.get("code"), str) and _SAFE_ERROR_CODE_RE.fullmatch(error["code"]):
                code = error["code"]
            candidate_status = error.get("status")
            if isinstance(candidate_status, int) and 400 <= candidate_status <= 599:
                status = candidate_status
        status = {
            "subscription_sharing_user_not_eligible": 403,
            "subscription_sharing_usage_limit_exceeded": 429,
            "subscription_sharing_usage_unavailable": 503,
            "subscription_sharing_unsupported_capability": 400,
            "subscription_sharing_route_not_supported": 403,
            "subscription_sharing_invalid_user": 409,
            "chatpass_v2_scope_not_authorized": 403,
            "chatpass_v2_invalid_authorization_context": 403,
            "subscription_sharing_user_unavailable": 503,
        }.get(code, status)
        if status == 401:
            code = "provider_auth_error"
        failure = ChatGPTError(status, code, ChatGPTHTTPAdapter._message_for(status, code))
        failure.diagnostics = ChatGPTHTTPAdapter._diagnostics(None, error)
        return failure

    @classmethod
    def _sse_events(
        cls,
        response: httpx.Response,
        *,
        deadline: float | None = None,
        max_body_bytes: int | None = None,
        max_line_bytes: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        body_limit = MAX_SSE_BODY_BYTES if max_body_bytes is None else max_body_bytes
        line_limit = MAX_SSE_LINE_BYTES if max_line_bytes is None else max_line_bytes
        buffer = bytearray()
        data_lines: list[bytes] = []
        total = 0

        def parse_event() -> dict[str, Any] | None:
            if not data_lines:
                return None
            payload = b"\n".join(data_lines)
            data_lines.clear()
            if payload == b"[DONE]":
                return None
            try:
                event = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ChatGPTError(
                    502,
                    "provider_invalid_stream",
                    "ChatGPT returned an invalid verification stream.",
                ) from None
            if not isinstance(event, dict):
                raise ChatGPTError(
                    502,
                    "provider_invalid_stream",
                    "ChatGPT returned an invalid verification stream.",
                )
            return event

        try:
            cls._check_deadline(deadline)
            for chunk in response.iter_bytes():
                cls._check_deadline(deadline)
                total += len(chunk)
                if total > body_limit:
                    raise ChatGPTError(
                        502,
                        "provider_stream_too_large",
                        "ChatGPT returned an oversized verification stream.",
                    )
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        if len(buffer) > line_limit:
                            raise ChatGPTError(
                                502,
                                "provider_stream_line_too_large",
                                "ChatGPT returned an oversized verification stream.",
                            )
                        break
                    if newline > line_limit:
                        raise ChatGPTError(
                            502,
                            "provider_stream_line_too_large",
                            "ChatGPT returned an oversized stream line.",
                        )
                    line = bytes(buffer[:newline]).rstrip(b"\r")
                    del buffer[: newline + 1]
                    if not line:
                        event = parse_event()
                        if event is not None:
                            yield event
                    elif not line.startswith(b":"):
                        field, separator, value = line.partition(b":")
                        if field == b"data":
                            if separator and value.startswith(b" "):
                                value = value[1:]
                            data_lines.append(value)
            cls._check_deadline(deadline)
            if buffer:
                line = bytes(buffer).rstrip(b"\r")
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip(b" "))
            event = parse_event()
            if event is not None:
                yield event
        except httpx.HTTPError:
            raise

    def revoke(self, *, client_id: str, refresh_token: str) -> bool:
        with self._client() as client:
            discovery = self._discovery(client)
            deadline = time.monotonic() + HTTP_TOTAL_TIMEOUT_SECONDS
            try:
                with client.stream(
                    "POST",
                    discovery["revocation_endpoint"],
                    headers={"Accept": "application/json"},
                    data={
                        "token": refresh_token,
                        "token_type_hint": "refresh_token",
                        "client_id": client_id,
                    },
                ) as response:
                    self._read_limited(response, deadline=deadline)
            except httpx.TimeoutException:
                raise ChatGPTError(
                    503, "provider_timeout", "ChatGPT session revocation was not confirmed."
                ) from None
            except httpx.HTTPError:
                raise ChatGPTError(
                    503, "provider_unavailable", "ChatGPT session revocation was not confirmed."
                ) from None
        if response.status_code == 200:
            return True
        if response.status_code >= 500:
            raise ChatGPTError(
                response.status_code,
                "provider_revocation_unavailable",
                "ChatGPT session revocation was not confirmed.",
            )
        return False


class ChatGPTProvider:
    """Provider lifecycle and preferences, independent from HTTP route definitions."""

    def __init__(
        self,
        directory: Path,
        *,
        callback_url: str,
        adapter: ChatGPTAdapter | None = None,
        clock=time.time,
        sleeper=time.sleep,
        active_task_guard: Callable[[], None] | None = None,
    ) -> None:
        self.store = ProviderStore(directory)
        self.callback_url = callback_url
        self.adapter: ChatGPTAdapter = adapter or ChatGPTHTTPAdapter()
        self._token_refresh_lock = threading.Lock()
        self._clock = clock
        self._sleep = sleeper
        self._active_task_guard = active_task_guard

    def _assert_provider_mutation_allowed(self) -> None:
        if self._active_task_guard is not None:
            self._active_task_guard()

    def ensure(self) -> None:
        self.store.ensure()
        with self.store.locked() as state:
            needs_disconnected_migration = state["connection_status"] == "disconnected" and (
                state.get("active_client_id") is not None
                or bool(state["registrations"])
                or any("expected_subject" in session for session in state["sessions"].values())
            )
        if not needs_disconnected_migration:
            return
        with self.store.locked(write=True) as state:
            if state["connection_status"] != "disconnected":
                return
            client_id = state.get("active_client_id")
            if self._issued_client_id(client_id):
                state["provisional_client_id"] = client_id
            state["active_client_id"] = None
            state["registrations"].clear()
            for session in state["sessions"].values():
                session.pop("expected_subject", None)

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _iso_timestamp(value: float) -> str:
        return (
            datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )

    @staticmethod
    def _clear_session_secrets(session: dict[str, Any]) -> None:
        for key in ("state", "nonce", "verifier", "expected_subject"):
            session.pop(key, None)
        session["claimed"] = False

    @staticmethod
    def _expire_sessions(state: dict[str, Any], now: float) -> None:
        for session in state["sessions"].values():
            if session.get("status") == "pending" and session.get("expires_at", 0) <= now:
                session["status"] = "expired"
                ChatGPTProvider._clear_session_secrets(session)
        cutoff = now - 24 * 60 * 60
        for session_id in list(state["sessions"]):
            session = state["sessions"][session_id]
            if session.get("status") != "pending" and session.get("expires_at", now) < cutoff:
                del state["sessions"][session_id]

    @staticmethod
    def _session_view(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session["id"],
            "status": session["status"],
            "expires_at": ChatGPTProvider._iso_timestamp(session["expires_at"]),
            "error": session.get("error"),
        }

    @staticmethod
    def _callback_error(code: str, message: str, status_code: int = 400) -> ChatGPTError:
        return ChatGPTError(status_code, code, message)

    @staticmethod
    def _issued_client_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and value != DYNAMIC_CLIENT_ID
            and bool(_CLIENT_ID_RE.fullmatch(value))
        )

    def start_login(self) -> dict[str, Any]:
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            self._revoke_invalid_subscriber()
            return self._start_login()

    def _revoke_invalid_subscriber(self) -> None:
        with self.store.locked() as state:
            if (
                state["connection_status"] != "reconnect_required"
                or state.get("last_provider_error") != "subscription_sharing_invalid_user"
            ):
                return
            client_id = state.get("active_client_id")
            registration = state["registrations"].get(client_id, {})
            refresh_token = registration.get("refresh_token")
        if not isinstance(refresh_token, str):
            return
        try:
            confirmed = self.adapter.revoke(client_id=client_id, refresh_token=refresh_token)
        except Exception:
            confirmed = False
        if not confirmed:
            raise ChatGPTError(
                503, "revocation_unconfirmed", "Confirm revocation before signing in again."
            )
        with self.store.locked(write=True) as state:
            self._clear_tokens(state["registrations"][client_id])

    @staticmethod
    def _registration_name(value: object) -> str:
        if not isinstance(value, str):
            raise ChatGPTError(
                400, "invalid_registration_name", "Choose a valid ChatGPT registration name."
            )
        name = value.strip()
        try:
            name_bytes = len(name.encode("utf-8"))
        except UnicodeEncodeError:
            name_bytes = MAX_REGISTRATION_NAME_BYTES + 1
        if (
            not name
            or name_bytes > MAX_REGISTRATION_NAME_BYTES
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
        ):
            raise ChatGPTError(
                400, "invalid_registration_name", "Choose a valid ChatGPT registration name."
            )
        return name

    @staticmethod
    def _valid_session_id(session_id: object) -> bool:
        return isinstance(session_id, str) and bool(
            re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id)
        )

    def recreate_registration(
        self, *, display_name: str = DEFAULT_REGISTRATION_NAME
    ) -> dict[str, Any]:
        """Revoke the current registration, then start a deliberately dynamic login."""
        registration_name = self._registration_name(display_name)
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            if not self.callback_url:
                raise ChatGPTError(
                    409,
                    "callback_not_configured",
                    "Configure an HTTP loopback callback before signing in to ChatGPT.",
                )
            with self.store.locked() as state:
                if state["connection_status"] not in {"connected", "reconnect_required"}:
                    raise ChatGPTError(
                        409,
                        "registration_recreation_unavailable",
                        "Connect ChatGPT before recreating its registration.",
                    )
                client_id = state.get("active_client_id")
                registration = state["registrations"].get(client_id) if client_id else None
                refresh_token = (
                    registration.get("refresh_token") if isinstance(registration, dict) else None
                )
                if not self._issued_client_id(client_id) or not isinstance(refresh_token, str):
                    raise ChatGPTError(
                        409,
                        "registration_recreation_unavailable",
                        "The current ChatGPT registration cannot be recreated safely.",
                    )
            try:
                confirmed = self.adapter.revoke(
                    client_id=client_id,
                    refresh_token=refresh_token,
                )
            except Exception:
                confirmed = False
            if not confirmed:
                raise ChatGPTError(
                    503,
                    "revocation_unconfirmed",
                    "Confirm revocation before recreating the ChatGPT registration.",
                )
            with self.store.locked(write=True) as state:
                state["registrations"].pop(client_id, None)
                state["active_client_id"] = None
                state["connection_status"] = "disconnected"
                state["connection_version"] += 1
                state["notes_provider"] = None
                state.pop("provisional_client_id", None)
                state.pop("last_provider_error", None)
                state.pop("last_diagnostic", None)
                state["verification"] = None
            response = self._start_login(
                force_dynamic_registration=True,
                registration_name=registration_name,
            )
            response["registration_recreation_started"] = True
            return response

    def _start_login(
        self,
        *,
        force_dynamic_registration: bool = False,
        registration_name: str = DEFAULT_REGISTRATION_NAME,
    ) -> dict[str, Any]:
        if not self.callback_url:
            raise ChatGPTError(
                409,
                "callback_not_configured",
                "Configure an HTTP loopback callback before signing in to ChatGPT.",
            )
        now = self._now()
        with self.store.locked(write=True) as state:
            self._expire_sessions(state, now)
            pending_sessions = [
                session
                for session in state["sessions"].values()
                if session.get("status") == "pending"
            ]
            for session in pending_sessions:
                session["status"] = "cancelled"
                session["error"] = None
                self._clear_session_secrets(session)
            client_id = None if force_dynamic_registration else state.get("active_client_id")
            registration = state["registrations"].get(client_id) if client_id else None
            if (
                not force_dynamic_registration
                and registration is None
                and state.get("provisional_client_id")
            ):
                client_id = state["provisional_client_id"]
                registration = {}
            if registration is not None and not self._issued_client_id(client_id):
                raise ProviderStateError("Provider registration mapping is invalid")

            session_id = secrets.token_urlsafe(24)
            oauth_state = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            expires_at = now + SESSION_TTL_SECONDS
            session: dict[str, Any] = {
                "id": session_id,
                "status": "pending",
                "created_at": now,
                "expires_at": expires_at,
                "state": oauth_state,
                "nonce": nonce,
                "verifier": verifier,
                "expected_client_id": client_id if registration is not None else None,
                "expected_subject": registration.get("subject")
                if registration is not None
                else None,
                "claimed": False,
                "error": None,
            }
            state["sessions"][session_id] = session
            parameters = {
                "client_id": client_id if registration is not None else DYNAMIC_CLIENT_ID,
                "ext_agent_host_id": state["host_id"],
                "response_type": "code",
                "redirect_uri": self.callback_url,
                "scope": REQUESTED_SCOPES,
                "resource": RESOURCE,
                "state": oauth_state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if registration is None:
                parameters["agent_name_hint"] = registration_name
            elif registration.get("email"):
                parameters["login_hint"] = registration["email"]
            if state.get("last_provider_error") in {
                "scope_not_authorized",
                "scope_down",
                "chatpass_v2_scope_not_authorized",
            }:
                parameters["prompt"] = "consent"
            authorization_url = f"{AUTHORIZATION_URL}?{urlencode(parameters)}"
            response: dict[str, Any] = {
                "id": session_id,
                "status": "pending",
                "authorization_url": authorization_url,
                "expires_at": self._iso_timestamp(expires_at),
            }
            if pending_sessions:
                response["replaced_previous"] = True
            return response

    def login_session(self, session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id):
            raise ChatGPTError(404, "login_session_not_found", "ChatGPT sign-in session not found.")
        now = self._now()
        with self.store.locked(write=True) as state:
            self._expire_sessions(state, now)
            session = state["sessions"].get(session_id)
            if session is None:
                raise ChatGPTError(
                    404, "login_session_not_found", "ChatGPT sign-in session not found."
                )
            return self._session_view(session)

    def cancel_login(self, session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id):
            raise ChatGPTError(404, "login_session_not_found", "ChatGPT sign-in session not found.")
        now = self._now()
        with self.store.locked(write=True) as state:
            self._expire_sessions(state, now)
            session = state["sessions"].get(session_id)
            if session is None:
                raise ChatGPTError(
                    404, "login_session_not_found", "ChatGPT sign-in session not found."
                )
            if session["status"] == "pending":
                session["status"] = "cancelled"
                self._clear_session_secrets(session)
                session["error"] = None
                return self._session_view(session)
            if session["status"] in {"cancelled", "expired"}:
                return self._session_view(session)
            raise ChatGPTError(
                409,
                "login_session_completed",
                "This ChatGPT sign-in session has already completed.",
            )

    @staticmethod
    def _query_values(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
        if len(pairs) > 20:
            raise ChatGPTError(400, "invalid_callback", "The ChatGPT sign-in response is invalid.")
        values: dict[str, str] = {}
        for key, value in pairs:
            if key not in {"state", "code", "client_id", "error", "scope"}:
                continue
            if (
                key in values
                or len(value) > 4096
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            ):
                raise ChatGPTError(
                    400, "invalid_callback", "The ChatGPT sign-in response is invalid."
                )
            values[key] = value
        return values

    def _callback_url_pairs(self, callback_url: object) -> list[tuple[str, str]]:
        if not isinstance(callback_url, str):
            raise self._callback_error(
                "invalid_callback_url", "Provide the ChatGPT callback URL from the browser."
            )
        try:
            callback_bytes = len(callback_url.encode("utf-8"))
        except UnicodeEncodeError:
            callback_bytes = MAX_CALLBACK_URL_BYTES + 1
        if (
            not callback_url
            or callback_bytes > MAX_CALLBACK_URL_BYTES
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in callback_url)
        ):
            raise self._callback_error(
                "invalid_callback_url", "Provide the ChatGPT callback URL from the browser."
            )
        try:
            configured = urlsplit(self.callback_url)
            parsed = urlsplit(callback_url)
            if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
                raise ValueError("Invalid callback encoding")
            pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=20,
                errors="strict",
            )
        except (TypeError, ValueError):
            raise self._callback_error(
                "invalid_callback_url", "Provide the ChatGPT callback URL from the browser."
            ) from None
        if (
            configured.scheme != "http"
            or configured.hostname is None
            or configured.hostname.lower() != "127.0.0.1"
            or configured.username is not None
            or configured.password is not None
            or configured.path != "/auth/chatgpt/callback"
            or configured.query
            or configured.fragment
            or parsed.scheme != configured.scheme
            or parsed.netloc != configured.netloc
            or parsed.path != configured.path
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise self._callback_error(
                "invalid_callback_url", "Provide the ChatGPT callback URL from the browser."
            )
        seen: set[str] = set()
        for key, value in pairs:
            if (
                not key
                or key in seen
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in key)
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            ):
                raise self._callback_error(
                    "invalid_callback_url", "Provide the ChatGPT callback URL from the browser."
                )
            seen.add(key)
        return pairs

    def _claim_callback(
        self, oauth_state: str, *, expected_session_id: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        now = self._now()
        with self.store.locked(write=True) as state:
            self._expire_sessions(state, now)
            if expected_session_id is None:
                matches = [
                    (session_id, session)
                    for session_id, session in state["sessions"].items()
                    if session.get("status") == "pending"
                    and isinstance(session.get("state"), str)
                    and hmac.compare_digest(session["state"], oauth_state)
                ]
            else:
                session = state["sessions"].get(expected_session_id)
                matches = (
                    [(expected_session_id, session)]
                    if isinstance(session, dict)
                    and session.get("status") == "pending"
                    and isinstance(session.get("state"), str)
                    and hmac.compare_digest(session["state"], oauth_state)
                    else []
                )
            if len(matches) != 1:
                raise ChatGPTError(
                    400,
                    "invalid_or_replayed_state",
                    "The ChatGPT sign-in response could not be matched to an active attempt.",
                )
            session_id, session = matches[0]
            if session.get("claimed"):
                raise ChatGPTError(
                    409, "callback_replayed", "This ChatGPT sign-in response was already received."
                )
            session["claimed"] = True
            return session_id, dict(session)

    def _finish_session(
        self,
        session_id: str,
        *,
        status: str,
        error: dict[str, str] | None = None,
    ) -> None:
        with self.store.locked(write=True) as state:
            session = state["sessions"].get(session_id)
            if session is None or session.get("status") != "pending":
                return
            session["status"] = status
            session["error"] = error
            self._clear_session_secrets(session)

    def _fail_session(self, session_id: str, error: ChatGPTError) -> None:
        self._finish_session(
            session_id,
            status="failed",
            error={"code": error.code, "message": error.public_message},
        )

    @staticmethod
    def _token_fields(
        tokens: dict[str, Any], *, previous_scopes: set[str] | None = None
    ) -> dict[str, Any]:
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        token_type = tokens.get("token_type", "Bearer")
        expires_in = tokens.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > 64 * 1024
            or not isinstance(refresh_token, str)
            or not refresh_token
            or len(refresh_token) > 64 * 1024
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 7 * 24 * 60 * 60
        ):
            raise ChatGPTError(
                502, "invalid_token_response", "ChatGPT returned an invalid token response."
            )
        raw_scope = tokens.get("scope")
        if raw_scope is None and previous_scopes is not None:
            scopes = set(previous_scopes)
        elif isinstance(raw_scope, str) and len(raw_scope) <= 4096:
            scopes = set(raw_scope.split())
        else:
            raise ChatGPTError(
                502, "invalid_token_response", "ChatGPT returned an invalid token response."
            )
        id_token = tokens.get("id_token")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token
            if isinstance(id_token, str) and len(id_token) <= 64 * 1024
            else None,
            "token_type": "Bearer",
            "expires_at": time.time() + expires_in,
            "scopes": sorted(scopes),
        }

    def handle_callback(self, pairs: Sequence[tuple[str, str]]) -> None:
        # Never consume a one-time OAuth result before acquiring the commit lock.
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            self._handle_callback(pairs)

    def submit_callback(self, session_id: str, callback_url: str) -> dict[str, Any]:
        if not self._valid_session_id(session_id):
            raise ChatGPTError(404, "login_session_not_found", "ChatGPT sign-in session not found.")
        pairs = self._callback_url_pairs(callback_url)
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            self._handle_callback(pairs, expected_session_id=session_id)
        return self.login_session(session_id)

    def _revoke_rejected_exchange(self, client_id: str, tokens: object) -> None:
        # Best-effort cleanup mirrors explicit logout: discarded credentials are never usable.
        refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
        if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 64 * 1024:
            return
        with suppress(Exception):
            self.adapter.revoke(client_id=client_id, refresh_token=refresh_token)

    def _handle_callback(
        self, pairs: Sequence[tuple[str, str]], *, expected_session_id: str | None = None
    ) -> None:
        values = self._query_values(pairs)
        oauth_state = values.get("state")
        if oauth_state is None or not oauth_state.isascii() or not 20 <= len(oauth_state) <= 256:
            raise ChatGPTError(400, "invalid_callback", "The ChatGPT sign-in response is invalid.")
        session_id, session = self._claim_callback(
            oauth_state, expected_session_id=expected_session_id
        )

        oauth_error = values.get("error")
        if oauth_error:
            error = ChatGPTError(
                400, "access_denied", "ChatGPT sign-in was declined or could not be completed."
            )
            self._fail_session(session_id, error)
            raise error

        code = values.get("code")
        expected_client_id = session.get("expected_client_id")
        returned_client_id = values.get("client_id")
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 2048
            or (expected_client_id is None and not self._issued_client_id(returned_client_id))
            or (
                expected_client_id is not None
                and returned_client_id is not None
                and returned_client_id != expected_client_id
            )
        ):
            error = ChatGPTError(
                400, "invalid_callback", "The ChatGPT sign-in response is incomplete or mismatched."
            )
            self._fail_session(session_id, error)
            raise error
        client_id = expected_client_id or returned_client_id
        if not self._issued_client_id(client_id):
            error = ChatGPTError(
                400,
                "invalid_client_registration",
                "ChatGPT did not return a valid app registration.",
            )
            self._fail_session(session_id, error)
            raise error

        code_exchanged = False
        tokens: dict[str, Any] | None = None
        try:
            tokens = self.adapter.exchange_code(
                code=code,
                verifier=session["verifier"],
                redirect_uri=self.callback_url,
                client_id=client_id,
            )
            code_exchanged = True
            token_fields = self._token_fields(tokens)
            id_token = token_fields["id_token"]
            if not isinstance(id_token, str):
                raise ChatGPTError(
                    502, "invalid_identity_token", "ChatGPT returned an invalid identity token."
                )
            identity = self.adapter.validate_identity(
                id_token, client_id=client_id, nonce=session["nonce"]
            )
            if session.get("expected_subject") and identity.subject != session["expected_subject"]:
                raise ChatGPTError(
                    409,
                    "account_identity_mismatch",
                    "The selected ChatGPT account did not match the saved registration.",
                )
            token_fields["expires_at"] = self._now() + int(tokens["expires_in"])
            token_fields.update(
                {
                    "subject": identity.subject,
                    "email": identity.email if identity.email_verified else None,
                    "label": identity.email
                    if identity.email_verified
                    else f"ChatGPT account · {client_id[-6:]}",
                    "client_id": client_id,
                }
            )
        except ChatGPTError as error:
            if code_exchanged:
                self._revoke_rejected_exchange(client_id, tokens)
            if not code_exchanged and error.code == "invalid_grant" and expected_client_id is None:
                with self.store.locked(write=True) as state:
                    current = state["sessions"].get(session_id)
                    if current and current.get("status") == "pending":
                        # Retain routing metadata only; identity remains unverified.
                        state["provisional_client_id"] = client_id
            self._fail_session(session_id, error)
            raise
        except (KeyError, TypeError, ValueError):
            if code_exchanged:
                self._revoke_rejected_exchange(client_id, tokens)
            error = ChatGPTError(
                502, "provider_invalid_response", "ChatGPT returned an invalid sign-in response."
            )
            self._fail_session(session_id, error)
            raise error from None

        commit_error: ChatGPTError | None = None
        now = self._now()
        with self.store.locked(write=True) as state:
            current = state["sessions"].get(session_id)
            if current is None or current.get("status") != "pending" or not current.get("claimed"):
                commit_error = ChatGPTError(
                    409,
                    "login_cancelled",
                    "This ChatGPT sign-in was cancelled before it completed.",
                )
            elif current.get("expires_at", 0) <= now:
                current["status"] = "expired"
                current["error"] = None
                self._clear_session_secrets(current)
                commit_error = ChatGPTError(
                    410, "login_session_expired", "This ChatGPT sign-in session expired."
                )
            else:
                prior = state["registrations"].get(client_id)
                if prior is not None and prior.get("subject") != identity.subject:
                    current["status"] = "failed"
                    current["error"] = {
                        "code": "account_identity_mismatch",
                        "message": (
                            "The selected ChatGPT account did not match the saved registration."
                        ),
                    }
                    self._clear_session_secrets(current)
                    commit_error = ChatGPTError(
                        409,
                        "account_identity_mismatch",
                        "The selected ChatGPT account did not match the saved registration.",
                    )
                else:
                    state["registrations"][client_id] = token_fields
                    state["active_client_id"] = client_id
                    state.pop("provisional_client_id", None)
                    state["connection_status"] = (
                        "connected"
                        if DIRECT_SCOPE in token_fields["scopes"]
                        else "reconnect_required"
                    )
                    if DIRECT_SCOPE in token_fields["scopes"]:
                        state.pop("last_provider_error", None)
                    else:
                        state["last_provider_error"] = "scope_not_authorized"
                    state.pop("last_diagnostic", None)
                    state["connection_version"] += 1
                    state["verification"] = None
                    current["status"] = "connected"
                    current["error"] = None
                    self._clear_session_secrets(current)
        if commit_error is not None:
            self._revoke_rejected_exchange(client_id, tokens)
            raise commit_error

    def get_providers(self) -> dict[str, Any]:
        with self.store.locked() as state:
            client_id = state.get("active_client_id")
            registration = state["registrations"].get(client_id) if client_id else None
            account = None
            if state["connection_status"] != "disconnected" and registration is not None:
                account = {"label": registration.get("label", "ChatGPT account")}
            return {
                "providers": [
                    {
                        "id": "chatgpt",
                        "status": state["connection_status"],
                        "account": account,
                        "connection_version": state["connection_version"],
                        "verification": state.get("verification"),
                    }
                ]
            }

    def saved_notes_settings(self) -> tuple[str | None, str]:
        """Return the saved notes model and reasoning effort, whatever the connection state."""
        with self.store.locked() as state:
            model = state.get("selected_model")
            effort = state.get("selected_reasoning_effort", "default")
            return (
                model if isinstance(model, str) and model else None,
                effort if isinstance(effort, str) else "default",
            )

    def _active_registration(self) -> tuple[str, dict[str, Any]]:
        with self.store.locked() as state:
            if state["connection_status"] != "connected":
                if state["connection_status"] == "reconnect_required":
                    raise ChatGPTError(
                        409, "reconnect_required", "Reconnect ChatGPT before using its plan."
                    )
                raise ChatGPTError(
                    409, "provider_disconnected", "Connect ChatGPT before using its plan."
                )
            client_id = state.get("active_client_id")
            registration = state["registrations"].get(client_id) if client_id else None
            if not isinstance(registration, dict) or not isinstance(
                registration.get("access_token"), str
            ):
                raise ProviderStateError("Provider registration credentials are unavailable")
            if DIRECT_SCOPE not in registration.get("scopes", []):
                raise ChatGPTError(
                    409, "scope_not_authorized", "Sign in again and allow ChatGPT plan use."
                )
            return client_id, dict(registration)

    @staticmethod
    def _clear_tokens(registration: dict[str, Any]) -> None:
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "token_type",
            "expires_at",
            "scopes",
        ):
            registration.pop(key, None)

    def _record_failure(self, error: ChatGPTError, *, budget: NotesBudget | None = None) -> None:
        diagnostics = error.diagnostics
        if error.code == "notes_invalid_output":
            diagnostics = _safe_notes_validation_diagnostics(diagnostics, budget=budget)
        elif isinstance(diagnostics, dict) and diagnostics.get("stage") in _NOTES_VALIDATION_STAGES:
            diagnostics = _safe_notes_call_diagnostics(diagnostics, budget=budget)
        elif not isinstance(diagnostics, dict):
            diagnostics = {}
        with self.store.locked(write=True) as state:
            state["last_diagnostic"] = {
                "code": error.code,
                "at": self._iso_timestamp(self._now()),
                **diagnostics,
            }

    def _mark_reconnect_required(self, code: str, *, refresh_token: str | None = None) -> None:
        with self.store.locked(write=True) as state:
            client_id = state.get("active_client_id")
            registration = state["registrations"].get(client_id) if client_id else None
            if registration is not None:
                revocation_token = refresh_token or registration.get("refresh_token")
                self._clear_tokens(registration)
                if code not in _TERMINAL_REFRESH_CODES and isinstance(revocation_token, str):
                    # Block inference, but retain the renewable session for explicit revocation.
                    registration["refresh_token"] = revocation_token
            state["connection_status"] = "reconnect_required"
            state["connection_version"] += 1
            state["verification"] = None
            state["last_provider_error"] = code

    def _access_token(
        self, *, minimum_lifetime_seconds: float = ACCESS_REFRESH_MARGIN_SECONDS
    ) -> str:
        # One generation holds the store operation across its parallel map transports.
        # Serialize token rotation inside that operation, while model requests stay parallel.
        with self._token_refresh_lock:
            return self._access_token_locked(minimum_lifetime_seconds=minimum_lifetime_seconds)

    def _access_token_locked(self, *, minimum_lifetime_seconds: float) -> str:
        client_id, registration = self._active_registration()
        expires_at = registration.get("expires_at")
        refresh_token = registration.get("refresh_token")
        if not isinstance(expires_at, (float, int)) or not isinstance(refresh_token, str):
            raise ProviderStateError("Provider token state is invalid")
        if expires_at > self._now() + minimum_lifetime_seconds:
            return registration["access_token"]
        try:
            refreshed = self.adapter.refresh(client_id=client_id, refresh_token=refresh_token)
            fields = self._token_fields(
                refreshed, previous_scopes=set(registration.get("scopes", []))
            )
        except ChatGPTError as error:
            self._record_failure(error)
            if error.code in _TERMINAL_REFRESH_CODES:
                self._mark_reconnect_required(error.code)
                raise ChatGPTError(
                    409, "reconnect_required", "Reconnect ChatGPT to renew plan access."
                ) from None
            if error.code in _RECONNECT_ERROR_CODES:
                self._mark_reconnect_required(error.code, refresh_token=refresh_token)
            raise
        old_scopes = set(registration.get("scopes", []))
        new_scopes = set(fields["scopes"])
        if not old_scopes.issubset(new_scopes) or DIRECT_SCOPE not in new_scopes:
            self._mark_reconnect_required("scope_down", refresh_token=fields["refresh_token"])
            raise ChatGPTError(
                409, "reconnect_required", "Reconnect ChatGPT to restore the required plan access."
            )
        fields["subject"] = registration["subject"]
        fields["email"] = registration.get("email")
        fields["label"] = registration.get("label", "ChatGPT account")
        fields["client_id"] = client_id
        fields["id_token"] = registration.get("id_token")
        fields["expires_at"] = self._now() + int(refreshed["expires_in"])
        with self.store.locked(write=True) as state:
            current = state["registrations"].get(client_id)
            if (
                current is None
                or current.get("refresh_token") != refresh_token
                or state.get("connection_status") != "connected"
            ):
                raise ChatGPTError(
                    409, "connection_changed", "The ChatGPT connection changed. Try again."
                )
            state["registrations"][client_id] = fields
        return fields["access_token"]

    def _models(self) -> list[dict[str, Any]]:
        access_token = self._access_token()
        try:
            raw_models = self.adapter.list_models(access_token=access_token)
        except ChatGPTError as error:
            self._record_failure(error)
            if error.code in _RECONNECT_ERROR_CODES:
                self._mark_reconnect_required(error.code)
            raise
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = item.get("slug")
            display_name = item.get("display_name")
            if (
                not isinstance(slug, str)
                or not slug
                or len(slug) > 200
                or not isinstance(display_name, str)
                or not display_name
                or len(display_name) > 200
                or slug in seen
            ):
                continue
            seen.add(slug)
            models.append(
                {
                    "id": slug,
                    "display_name": display_name,
                    "reasoning_efforts": list(reasoning_efforts_for_model(slug)),
                }
            )
        return models

    def list_models(self) -> dict[str, Any]:
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            models = self._models()
            with self.store.locked() as state:
                return {"models": models, "selected_model": state.get("selected_model")}

    def save_notes_preference(self, *, model: str, reasoning_effort: str = "default") -> None:
        """Save the next-run model after static validation only; never touches the network."""
        if not isinstance(model, str) or not 1 <= len(model) <= 200:
            raise ChatGPTError(400, "invalid_preference", "Choose an available ChatGPT model.")
        if not isinstance(reasoning_effort, str):
            raise ChatGPTError(400, "invalid_preference", "Choose an available ChatGPT model.")
        validate_reasoning_effort(model, reasoning_effort)
        with self.store.locked(write=True) as state:
            state["selected_model"] = model
            state["selected_reasoning_effort"] = reasoning_effort

    def assert_notes_acceptable(self) -> None:
        """ChatGPT sign-in cannot replace the account during a run, so nothing blocks notes."""

    def notes_task_pin(self) -> dict[str, Any]:
        """Return the connected account and selected inference settings under its lock."""
        with self.store.locked() as state:
            if state["connection_status"] != "connected":
                if state["connection_status"] == "reconnect_required":
                    raise ChatGPTError(
                        409, "reconnect_required", "Reconnect ChatGPT before using its plan."
                    )
                raise ChatGPTError(
                    409, "provider_disconnected", "Connect ChatGPT before creating notes."
                )
            model = state.get("selected_model")
            effort = state.get("selected_reasoning_effort", "default")
            connection_version = state.get("connection_version")
            if model is None:
                raise ChatGPTError(
                    409, "notes_model_unselected", "Choose a ChatGPT model before creating notes."
                )
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(effort, str)
                or isinstance(connection_version, bool)
                or not isinstance(connection_version, int)
                or connection_version < 0
            ):
                raise ProviderStateError("Selected notes settings are invalid")
            validate_reasoning_effort(model, effort)
            return {
                "provider": "chatgpt",
                "connection_version": connection_version,
                "model": model,
                "reasoning_effort": effort,
            }

    def _assert_pinned_connection(self, connection_version: int) -> None:
        """Check only that the pinned connection is still in use, never the saved preference."""
        with self.store.locked() as state:
            if state["connection_status"] == "reconnect_required":
                raise ChatGPTError(
                    409, "reconnect_required", "Reconnect ChatGPT before using its plan."
                )
            if state["connection_status"] != "connected":
                raise ChatGPTError(
                    409, "provider_disconnected", "Connect ChatGPT before creating notes."
                )
            if state["connection_version"] != connection_version:
                raise ChatGPTError(
                    409,
                    "connection_changed",
                    "The ChatGPT connection changed before notes started.",
                )

    def generate_notes_document(
        self,
        *,
        snapshot: dict[str, Any],
        connection_version: int,
        model: str,
        reasoning_effort: str,
        output_language: str = "en",
        on_coverage: Callable[[dict[str, Any]], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Generate and validate notes using only the account and model pinned at acceptance."""
        validate_reasoning_effort(model, reasoning_effort)
        budget = notes_budget("chatgpt", model)
        with self.store.operation():
            self._assert_pinned_connection(connection_version)

            def transport(
                prompt: str,
                *,
                allowed_source_ids: Sequence[str],
                max_output_bytes: int,
                stage: str,
                attempt: str,
                provider_call_index: int,
                prompt_bytes: int,
                budget: NotesBudget,
                max_unit_size: int = 1,
                response_schema: Mapping[str, Any] | None = None,
            ) -> str:
                observed_usage = getattr(self.adapter, "last_notes_usage", None)
                if isinstance(observed_usage, dict):
                    observed_usage.clear()
                access_token = self._access_token(
                    minimum_lifetime_seconds=(
                        NOTES_HTTP_TOTAL_TIMEOUT_SECONDS + ACCESS_REFRESH_MARGIN_SECONDS
                    )
                )
                try:
                    input_bytes = notes_provider_input_bytes(
                        prompt,
                        allowed_source_ids,
                        budget=budget,
                        max_unit_size=max_unit_size,
                        response_schema=response_schema,
                    )
                    estimated_input_tokens = estimate_notes_tokens(input_bytes, budget)
                except (NotesDocumentError, TypeError, ValueError, UnicodeError):
                    input_bytes = prompt_bytes
                    estimated_input_tokens = estimate_notes_tokens(input_bytes, budget)
                try:
                    adapter_call = {
                        "access_token": access_token,
                        "model": model,
                        "reasoning_effort": reasoning_effort,
                        "prompt": prompt,
                        "allowed_source_ids": allowed_source_ids,
                        "max_output_bytes": max_output_bytes,
                        "budget": budget,
                    }
                    if max_unit_size > 1:
                        adapter_call["max_unit_size"] = max_unit_size
                    if response_schema is not None:
                        adapter_call["response_schema"] = response_schema
                    return self.adapter.generate_notes(
                        **adapter_call,
                    )
                except ChatGPTError as error:
                    diagnostics = (
                        dict(error.diagnostics) if isinstance(error.diagnostics, dict) else {}
                    )
                    diagnostics.update(
                        {
                            "stage": stage,
                            "attempt": attempt,
                            "provider_call_index": provider_call_index,
                            "prompt_bytes": prompt_bytes,
                            "response_limit": max_output_bytes,
                        }
                    )
                    error.diagnostics = _safe_notes_call_diagnostics(diagnostics, budget=budget)
                    self._record_failure(error, budget=budget)
                    if error.code in _RECONNECT_ERROR_CODES:
                        self._mark_reconnect_required(error.code)
                    raise
                finally:
                    log_notes_provider_call(
                        provider="chatgpt",
                        model=model,
                        stage=stage,
                        attempt=attempt,
                        provider_call_index=provider_call_index,
                        prompt_bytes=prompt_bytes,
                        estimated_input_tokens=estimated_input_tokens,
                        usage=getattr(self.adapter, "last_notes_usage", None),
                    )

            def invalid_output_error(diagnostics: dict[str, Any]) -> ChatGPTError:
                safe_diagnostics = log_notes_validation_failure(
                    provider="chatgpt",
                    model=model,
                    diagnostics=diagnostics,
                    budget=budget,
                    output_bytes_limit=_notes_sse_body_limit(budget),
                )
                error = ChatGPTError(
                    502,
                    "notes_invalid_output",
                    "ChatGPT returned notes that did not match the saved transcript.",
                )
                error.diagnostics = safe_diagnostics
                self._record_failure(error, budget=budget)
                return error

            document = generate_notes_pipeline(
                snapshot=snapshot,
                output_language=output_language,
                transport=transport,
                invalid_output_error=invalid_output_error,
                limit_error=lambda message: ChatGPTError(
                    413, "notes_synthesis_limit_exceeded", message
                ),
                on_coverage=on_coverage,
                on_progress=on_progress,
                provider="chatgpt",
                model=model,
            )
            with self.store.locked(write=True) as state:
                state.pop("last_diagnostic", None)
            return document

    def verify(self, *, model: str, reasoning_effort: str | None = None) -> dict[str, Any]:
        if not isinstance(model, str) or not 1 <= len(model) <= 200:
            raise ChatGPTError(400, "invalid_model", "Choose an available ChatGPT model to verify.")
        if reasoning_effort is None:
            with self.store.locked() as state:
                reasoning_effort = (
                    state.get("selected_reasoning_effort", "default")
                    if state.get("selected_model") == model
                    else "default"
                )
        validate_reasoning_effort(model, reasoning_effort)
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            models = self._models()
            if not any(item["id"] == model for item in models):
                raise ChatGPTError(
                    422,
                    "model_unavailable",
                    "The selected model is not available to this ChatGPT account.",
                )
            access_token = self._access_token()
            try:
                self.adapter.verify(
                    access_token=access_token, model=model, reasoning_effort=reasoning_effort
                )
            except ChatGPTError as error:
                self._record_failure(error)
                if error.code in _RECONNECT_ERROR_CODES:
                    self._mark_reconnect_required(error.code)
                raise
            verified_at = self._iso_timestamp(self._now())
            with self.store.locked(write=True) as state:
                state["verification"] = {
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "verified_at": verified_at,
                }
            return {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "verified_at": verified_at,
                "output": {"ok": True},
            }

    def disconnect(self) -> dict[str, Any]:
        confirmed = False
        self._assert_provider_mutation_allowed()
        with self.store.operation():
            self._assert_provider_mutation_allowed()
            try:
                with self.store.locked() as state:
                    client_id = state.get("active_client_id")
                    registration = state["registrations"].get(client_id) if client_id else None
                    refresh_token = registration.get("refresh_token") if registration else None
            except ProviderStateError:
                raise

            if isinstance(refresh_token, str) and self._issued_client_id(client_id):
                for attempt in range(3):
                    try:
                        confirmed = self.adapter.revoke(
                            client_id=client_id,
                            refresh_token=refresh_token,
                        )
                        break
                    except ChatGPTError as error:
                        if error.status_code < 500 or attempt == 2:
                            break
                        self._sleep(0.1 * (2**attempt))
                    except Exception:
                        # An adapter failure must not prevent local logout.
                        break

            with self.store.locked(write=True) as state:
                client_id = state.get("active_client_id")
                if self._issued_client_id(client_id):
                    # Keep the installation's issued client registration for the next
                    # sign-in, but remove all account identity and credential fields so
                    # disconnect can be followed by choosing a different account.
                    state["registrations"].pop(client_id, None)
                    state["provisional_client_id"] = client_id
                state["active_client_id"] = None
                state["connection_status"] = "disconnected"
                state["connection_version"] += 1
                state["notes_provider"] = None
                state["verification"] = None
                state.pop("last_provider_error", None)
                for session in state["sessions"].values():
                    # Older releases could persist the bound account subject on
                    # an already closed login session. Disconnect removes that
                    # identity from every retained session, including legacy data.
                    session.pop("expected_subject", None)
                    if session.get("status") == "pending":
                        session["status"] = "cancelled"
                        session["error"] = None
                        self._clear_session_secrets(session)
        return {"status": "disconnected", "remote_revocation_confirmed": confirmed}
