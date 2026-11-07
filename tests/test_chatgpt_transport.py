from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from asr_studio.chatgpt import (
    API_BASE_URL,
    AUTHORIZATION_URL,
    DISCOVERY_URL,
    ISSUER,
    NOTES_MAX_OUTPUT_TOKENS,
    NOTES_MAX_REQUEST_BODY_BYTES,
    TOKEN_URL,
    ChatGPTError,
    ChatGPTHTTPAdapter,
)
from asr_studio.notes_document import (
    MAX_PROMPT_BYTES,
    MAX_REPAIR_OUTPUT_BYTES,
    build_chunk_prompt,
    build_notes_schema,
    plan_source,
    project_source_snapshot,
)

CLIENT_ID = "oaiapp_test-123456"


def test_chatgpt_notes_usage_is_isolated_per_transport_thread():
    adapter = ChatGPTHTTPAdapter()
    barrier = threading.Barrier(2)

    def observe(value):
        adapter.last_notes_usage = {"output_tokens": value}
        barrier.wait()
        return dict(adapter.last_notes_usage)

    with ThreadPoolExecutor(max_workers=2) as executor:
        observed = list(executor.map(observe, (11, 22)))

    assert observed == [{"output_tokens": 11}, {"output_tokens": 22}]


class SlowByteStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay: float) -> None:
        self.chunks = chunks
        self.delay = delay

    def __iter__(self):
        for chunk in self.chunks:
            time.sleep(self.delay)
            yield chunk


class DroppedStream(httpx.SyncByteStream):
    """A response whose connection fails after part of the body arrived."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __iter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"{\\"overview\\":"}\n\n'
        raise self.error


class TerminalThenExtraStream(httpx.SyncByteStream):
    def __init__(self, terminal_event: bytes) -> None:
        self.terminal_event = terminal_event
        self.read_after_terminal = False

    def __iter__(self):
        yield self.terminal_event
        self.read_after_terminal = True
        yield b": keep-alive after terminal event\n\n"


def _base64url_integer(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _identity_transport():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "unit-test-key",
                "use": "sig",
                "alg": "RS256",
                "n": _base64url_integer(numbers.n),
                "e": _base64url_integer(numbers.e),
            }
        ]
    }
    discovery = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_URL,
        "token_endpoint": TOKEN_URL,
        "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
        "revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json=discovery)
        if str(request.url) == discovery["jwks_uri"]:
            return httpx.Response(200, json=jwks)
        raise AssertionError(f"Unexpected provider URL: {request.url}")

    return private_key, requests, httpx.MockTransport(handler)


def _signed_id_token(private_key, *, audience=CLIENT_ID, nonce="expected-nonce", headers=None):
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": "verified-subject",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": "verified@example.test",
            "email_verified": True,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "unit-test-key", **(headers or {})},
    )


def test_identity_token_requires_trusted_signature_issuer_audience_and_nonce():
    private_key, requests, transport = _identity_transport()
    adapter = ChatGPTHTTPAdapter(transport=transport)

    identity = adapter.validate_identity(
        _signed_id_token(private_key), client_id=CLIENT_ID, nonce="expected-nonce"
    )

    assert identity.subject == "verified-subject"
    assert identity.email == "verified@example.test"
    assert identity.email_verified
    assert [str(request.url) for request in requests] == [
        DISCOVERY_URL,
        "https://auth.openai.com/.well-known/jwks.json",
    ]

    with pytest.raises(ChatGPTError) as wrong_nonce:
        adapter.validate_identity(
            _signed_id_token(private_key), client_id=CLIENT_ID, nonce="different-nonce"
        )
    assert wrong_nonce.value.code == "invalid_identity_token"
    with pytest.raises(ChatGPTError) as wrong_audience:
        adapter.validate_identity(
            _signed_id_token(private_key, audience="another-client"),
            client_id=CLIENT_ID,
            nonce="expected-nonce",
        )
    assert wrong_audience.value.code == "invalid_identity_token"


def test_identity_token_rejects_untrusted_jku_without_fetching_it():
    private_key, requests, transport = _identity_transport()
    adapter = ChatGPTHTTPAdapter(transport=transport)

    with pytest.raises(ChatGPTError) as untrusted_key_url:
        adapter.validate_identity(
            _signed_id_token(private_key, headers={"jku": "https://attacker.example/jwks"}),
            client_id=CLIENT_ID,
            nonce="expected-nonce",
        )

    assert untrusted_key_url.value.code == "invalid_identity_token"
    assert requests == []


def test_verification_sends_one_synthetic_strict_sse_request_and_requires_completion():
    requests: list[httpx.Request] = []
    stream = (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
        b'"delta":"{\\"ok\\":true}"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed",'
        b'"response":{"output":[]}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{API_BASE_URL}/responses"
    assert request.headers["authorization"] == "Bearer access-secret"
    body = json.loads(request.content)
    assert set(body) == {"model", "input", "text", "store", "stream"}
    assert body["model"] == "gpt-6.1-sol"
    assert body["store"] is False and body["stream"] is True
    assert body["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": 'Return exactly {"ok":true}.'}],
        }
    ]
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }


def test_verification_sends_selected_reasoning_effort():
    requests: list[httpx.Request] = []
    stream = (
        b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\n'
        b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    adapter.verify(access_token="test-token", model="gpt-5.6-luna", reasoning_effort="max")

    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["reasoning"] == {"effort": "max"}
    assert body["store"] is False and body["stream"] is True


def test_notes_request_caps_output_tokens_and_returns_only_a_completed_result():
    requests: list[httpx.Request] = []
    stream = (
        b'data: {"type":"response.output_text.delta","delta":"{\\"overview\\":[]}"}\n\n'
        b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    output = adapter.generate_notes(
        access_token="access-secret",
        model="gpt-6.1-sol",
        reasoning_effort="high",
        prompt="Bounded test prompt.",
        allowed_source_ids=("abcde", "vwxyz"),
    )

    assert output == '{"overview":[]}'
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    # Reasoning and visible JSON share this cap; high effort needs room for both.
    assert body["max_output_tokens"] == NOTES_MAX_OUTPUT_TOKENS == 25_000
    assert body["reasoning"] == {"effort": "high"}
    assert body["store"] is False and body["stream"] is True
    assert body["text"]["format"]["strict"] is True
    schema = body["text"]["format"]["schema"]
    assert schema["$defs"]["source_id"]["enum"] == ["abcde", "vwxyz"]
    assert schema["properties"]["overview"]["items"]["properties"]["source_ids"]["items"] == {
        "$ref": "#/$defs/source_id"
    }
    assert len(requests[0].content) <= NOTES_MAX_REQUEST_BODY_BYTES


@pytest.mark.parametrize(("response_limit", "observed_output_bytes"), [(4, 5), (16_000, 16_001)])
def test_notes_request_enforces_the_per_stage_stream_output_cap(
    response_limit, observed_output_bytes
):
    stream = (
        "data: "
        + json.dumps({"type": "response.output_text.delta", "delta": "x" * observed_output_bytes})
        + "\n\n"
        + 'data: {"type":"response.completed","response":{"output":[]}}\n\n'
    ).encode("utf-8")
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=stream
            )
        )
    )

    with pytest.raises(ChatGPTError) as oversized:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Bounded test prompt.",
            allowed_source_ids=(),
            max_output_bytes=response_limit,
        )

    assert oversized.value.code == "provider_output_too_large"
    assert oversized.value.diagnostics["output_bytes"] == observed_output_bytes


def test_notes_completed_output_over_cap_reports_observed_text_bytes():
    output_text = "x" * 16_001
    completed = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output_text}],
                }
            ]
        },
    }
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(completed) + "\n\n",
            )
        )
    )

    with pytest.raises(ChatGPTError) as oversized:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Bounded test prompt.",
            allowed_source_ids=(),
            max_output_bytes=16_000,
        )

    assert oversized.value.code == "provider_output_too_large"
    assert oversized.value.diagnostics["output_bytes"] == len(output_text.encode("utf-8"))


def test_notes_reasoning_stream_with_heartbeats_outlives_control_call_deadline(monkeypatch):
    clock = [0.0]
    requests = []
    monkeypatch.setattr("asr_studio.chatgpt.time.monotonic", lambda: clock[0])

    class ReasoningStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 45.0
            yield b": reasoning\n\n"
            clock[0] = 90.0
            yield b": reasoning\n\n"
            clock[0] = 120.0
            yield b'data: {"type":"response.output_text.delta","delta":"{}"}\n\n'
            clock[0] = 165.0
            yield b": reasoning\n\n"
            clock[0] = 180.0
            yield b'data: {"type":"response.completed","response":{"output":[]}}\n\n'

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=ReasoningStream())

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    assert (
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Test",
            allowed_source_ids=(),
        )
        == "{}"
    )
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 60.0
    # The short connection-check budget remains independent from notes inference.
    with adapter._client() as client:
        assert client.timeout.read == 15.0


def test_notes_total_deadline_closes_the_stream_without_resending(monkeypatch):
    clock = [0.0]
    calls = []
    closed = []
    monkeypatch.setattr("asr_studio.chatgpt.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("asr_studio.chatgpt.NOTES_HTTP_TOTAL_TIMEOUT_SECONDS", 1.0, raising=False)

    class ExpiredStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 2.0
            yield b'data: {"type":"response.completed","response":{"output":[]}}\n\n'

        def close(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=ExpiredStream())

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Test",
            allowed_source_ids=(),
        )
    assert failure.value.code == "provider_timeout"
    assert len(calls) == 1 and closed == [True]


def test_notes_stream_allows_framing_overhead_near_the_output_token_cap():
    text = "a" * NOTES_MAX_OUTPUT_TOKENS
    frames = []
    for index, character in enumerate(text):
        event = {
            "type": "response.output_text.delta",
            "sequence_number": index,
            "item_id": "msg_" + "a" * 48,
            "output_index": 0,
            "content_index": 0,
            "delta": character,
            "logprobs": [],
            "obfuscation": "Xa9kLm2Q",
        }
        frames.append(("data: " + json.dumps(event) + "\n\n").encode())
    frames.append(b'data: {"type":"response.completed","response":{"output":[]}}\n\n')
    wire_body = b"".join(frames)
    assert len(wire_body) > 512 * 1024
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=wire_body)

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    assert (
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Test",
            allowed_source_ids=(),
        )
        == text
    )
    assert len(calls) == 1


def test_notes_completed_event_can_exceed_the_short_verification_line_budget():
    text = "a" * 70_000
    event = {
        "type": "response.completed",
        "response": {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        },
    }
    wire_body = ("data: " + json.dumps(event) + "\n\n").encode()
    assert len(wire_body) > 64 * 1024
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, stream=SlowByteStream([wire_body[:66_000], wire_body[66_000:]], delay=0)
            )
        )
    )
    assert (
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Test",
            allowed_source_ids=(),
        )
        == text
    )


@pytest.mark.parametrize(
    "budget,error_code",
    [
        ("NOTES_MAX_SSE_BODY_BYTES", "provider_stream_too_large"),
        ("NOTES_MAX_SSE_LINE_BYTES", "provider_stream_line_too_large"),
    ],
)
def test_notes_wire_budgets_remain_bounded_for_complete_single_chunk_lines(
    monkeypatch, budget, error_code
):
    monkeypatch.setattr("asr_studio.chatgpt." + budget, 128, raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b":" + b"x" * 200 + b"\n\n")

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Test",
            allowed_source_ids=(),
        )
    assert failure.value.code == error_code
    assert len(calls) == 1


def test_notes_request_body_cap_rejects_expanded_json_without_sending_it():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("An oversized notes body must not be sent.")

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as oversized:
        adapter.generate_notes(
            access_token="access-secret",
            model="gpt-6.1-sol",
            reasoning_effort="default",
            prompt="\x01" * MAX_PROMPT_BYTES,
            allowed_source_ids=(),
        )

    assert oversized.value.code == "notes_request_too_large"
    assert calls == 0


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadError("connection reset"), "provider_unavailable"),
        (httpx.RemoteProtocolError("server disconnected"), "provider_unavailable"),
        (httpx.ReadTimeout("no data"), "provider_timeout"),
    ],
)
def test_notes_network_error_while_receiving_is_reported_and_never_resent(error, code):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=DroppedStream(error)
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="access-secret",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Bounded test prompt.",
            allowed_source_ids=(),
        )

    assert failure.value.code == code
    assert len(requests) == 1  # the inference request is sent once; the operator starts a new run


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.WriteError("broken pipe"), httpx.ReadError("reset")],
)
def test_notes_network_error_after_sending_is_reported_and_never_resent(error):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise error

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="access-secret",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            prompt="Bounded test prompt.",
            allowed_source_ids=(),
        )

    assert failure.value.code == "provider_unavailable"
    assert len(requests) == 1


def test_notes_incomplete_stream_never_returns_partial_output():
    stream = (
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        b'data: {"type":"response.incomplete","response":{}}\n\n'
    )
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=stream
            )
        )
    )

    with pytest.raises(ChatGPTError) as incomplete:
        adapter.generate_notes(
            access_token="access-secret",
            model="gpt-6.1-sol",
            reasoning_effort="default",
            prompt="Bounded test prompt.",
            allowed_source_ids=(),
        )

    assert incomplete.value.code == "notes_incomplete"


@pytest.mark.parametrize(
    ("reason", "code", "message"),
    [
        ("max_output_tokens", "notes_token_budget_exceeded", "token budget"),
        ("content_filter", "notes_content_filtered", "content filter"),
        ("untrusted-provider-text", "notes_incomplete", "did not complete"),
    ],
)
def test_notes_incomplete_reports_only_allowlisted_reason_without_retry(reason, code, message):
    calls = []
    event = {
        "type": "response.incomplete",
        "response": {"incomplete_details": {"reason": reason}},
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: " + json.dumps(event).encode() + b"\n\n",
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failed:
        adapter.generate_notes(
            access_token="local-test-token",
            model="gpt-6-luna",
            reasoning_effort="max",
            prompt="Test",
            allowed_source_ids=(),
        )
    assert failed.value.code == code
    assert message in failed.value.public_message
    assert reason not in failed.value.public_message
    assert len(calls) == 1


def test_invalid_notes_json_has_specific_public_error_instead_of_provider_outage():
    error = ChatGPTError(502, "notes_invalid_output", "private provider content")
    assert "invalid notes" in error.public_message
    assert "private provider content" not in error.public_message


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("json", "invalid JSON"),
        ("fields", "invalid structure"),
        ("version", "invalid structure"),
        ("items", "invalid structure"),
        ("text", "invalid structure"),
        ("action_fields", "invalid structure"),
        ("source_ids", "source references"),
        ("size", "output budget"),
    ],
)
def test_invalid_notes_public_message_uses_known_safe_reason_mapping(reason, message):
    error = ChatGPTError(502, "notes_invalid_output", "private provider content")
    error.diagnostics = {"validation_reason": reason}

    assert message in error.public_message
    assert "private provider content" not in error.public_message


@pytest.mark.parametrize("reason", ["private transcript text", ["source_ids"], {"code": "json"}])
def test_unknown_notes_diagnostic_reason_falls_back_without_interpolation(reason):
    error = ChatGPTError(502, "notes_invalid_output", "private provider content")
    error.diagnostics = {"validation_reason": reason}

    assert error.public_message == "ChatGPT returned invalid notes. No new notes were saved."
    assert "private provider content" not in error.public_message


@pytest.mark.parametrize(
    "status_code,code,message",
    [
        (
            502,
            "provider_output_too_large",
            "ChatGPT's response exceeded the allowed output size.",
        ),
        (
            502,
            "provider_stream_too_large",
            "ChatGPT's response exceeded the allowed transfer size.",
        ),
        (
            502,
            "provider_stream_line_too_large",
            "ChatGPT sent a response line that exceeded the allowed size.",
        ),
        (
            502,
            "provider_invalid_stream",
            "ChatGPT returned a response in an invalid format.",
        ),
        (
            503,
            "provider_timeout",
            "ChatGPT did not finish responding in time.",
        ),
    ],
)
def test_notes_transport_errors_have_accurate_safe_public_messages(status_code, code, message):
    error = ChatGPTError(status_code, code, "private provider response details")

    assert message in error.public_message
    assert "temporarily unavailable" not in error.public_message
    assert "private provider response details" not in error.public_message


def test_actual_provider_unavailable_keeps_its_unavailable_message():
    error = ChatGPTError(503, "provider_unavailable", "private provider response details")

    assert error.public_message == "ChatGPT plan use is temporarily unavailable."
    assert "private provider response details" not in error.public_message


def test_planned_repair_keeps_reasoning_and_schema_inside_the_transport_context():
    snapshot = {
        "meeting_date": "2026-10-19",
        "segments": [
            {
                "id": f"{index:032x}",
                "start_seconds": index,
                "end_seconds": index + 1,
                "text": "Lời thoại cuộc họp " * 5,
            }
            for index in range(1000)
        ],
    }
    projection = project_source_snapshot(snapshot)
    plan = plan_source(snapshot, projected_snapshot=projection.snapshot)
    requests = []
    expected_scopes = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type":"response.completed","response":{"output":[]}}\n\n',
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    for index, chunk in enumerate(plan.chunks, 1):
        projected_chunk = projection.project_chunk(chunk)
        allowed_source_ids = tuple(segment["id"] for segment in projected_chunk["segments"])
        expected_scopes.append(allowed_source_ids)
        prompt = build_chunk_prompt(
            projected_chunk,
            chunk_index=index,
            chunk_count=len(plan.chunks),
            allowed_source_ids=allowed_source_ids,
            invalid_output="x" * MAX_REPAIR_OUTPUT_BYTES,
        )
        adapter.generate_notes(
            access_token="local-test-token",
            model="gpt-6-luna",
            reasoning_effort="max",
            prompt=prompt,
            allowed_source_ids=allowed_source_ids,
        )
    assert len(requests) == len(plan.chunks) > 1
    for request, allowed_source_ids in zip(requests, expected_scopes, strict=True):
        body = json.loads(request.content)
        prompt_bytes = len(body["input"][0]["content"][0]["text"].encode())
        schema = body["text"]["format"]["schema"]
        assert schema == build_notes_schema(allowed_source_ids)
        schema_bytes = len(json.dumps(schema, separators=(",", ":")).encode())
        assert prompt_bytes + schema_bytes + 1024 + body["max_output_tokens"] + 4000 <= 64_000
        assert len(request.content) <= NOTES_MAX_REQUEST_BODY_BYTES


def test_verification_accepts_valid_sse_without_content_type():
    stream = (
        b"data: "
        + json.dumps({"type": "response.output_text.delta", "delta": '{"ok":true}'}).encode("utf-8")
        + b"\n\n"
        + b"data: "
        + json.dumps({"type": "response.completed", "response": {"output": []}}).encode("utf-8")
        + b"\n\n"
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response = httpx.Response(200, content=stream)
        assert "content-type" not in response.headers
        return response

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert calls == 1


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
def test_verification_rejects_explicit_non_sse_content_type(content_type):
    stream = (
        b'data: {"type":"response.completed","response":{"output":[{"type":"message",'
        b'"content":[{"type":"output_text","text":"{\\"ok\\":true}"}]}]}}\n\n'
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response = httpx.Response(
            200,
            headers={"content-type": content_type},
            content=stream,
        )
        assert "content-type" in response.headers
        return response

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as rejected:
        adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert rejected.value.code == "provider_invalid_stream"
    assert calls == 1


@pytest.mark.parametrize(
    "body,expected_code",
    [
        (b"", "verification_incomplete"),
        (b'{"ok":true}', "verification_incomplete"),
        (b"<!doctype html><html><body>error</body></html>", "verification_incomplete"),
        (b"data: {invalid-json}\n\n", "provider_invalid_stream"),
        (
            b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\n',
            "verification_incomplete",
        ),
        (
            (
                "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": '{"ok":true,"extra":false}',
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
                + "\n\n"
            ).encode("utf-8"),
            "verification_invalid_output",
        ),
    ],
)
def test_headerless_verification_requires_valid_complete_exact_output(body, expected_code):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response = httpx.Response(200, content=body)
        assert "content-type" not in response.headers
        return response

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as rejected:
        adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert rejected.value.code == expected_code
    assert calls == 1


def test_json_body_has_a_monotonic_total_deadline(monkeypatch):
    total_timeout = 0.05
    read_timeout = 0.04
    monkeypatch.setattr(
        "asr_studio.chatgpt.HTTP_TOTAL_TIMEOUT_SECONDS", total_timeout, raising=False
    )
    monkeypatch.setattr("asr_studio.chatgpt.HTTP_READ_TIMEOUT_SECONDS", read_timeout, raising=False)
    slow_stream = SlowByteStream([b" "] * 8, delay=read_timeout)
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=slow_stream))
    )

    started = time.monotonic()
    with adapter._client() as client, pytest.raises(ChatGPTError) as timed_out:
        adapter._json_request(client, "GET", f"{API_BASE_URL}/models")
    elapsed = time.monotonic() - started

    assert timed_out.value.code == "provider_timeout"
    assert total_timeout <= elapsed <= total_timeout + read_timeout + 0.05


def test_verification_sse_has_a_monotonic_total_deadline(monkeypatch):
    total_timeout = 0.05
    read_timeout = 0.04
    monkeypatch.setattr(
        "asr_studio.chatgpt.HTTP_TOTAL_TIMEOUT_SECONDS", total_timeout, raising=False
    )
    monkeypatch.setattr("asr_studio.chatgpt.HTTP_READ_TIMEOUT_SECONDS", read_timeout, raising=False)
    slow_stream = SlowByteStream([b": keep-alive\n\n"] * 8, delay=read_timeout)
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=slow_stream,
            )
        )
    )

    started = time.monotonic()
    with pytest.raises(ChatGPTError) as timed_out:
        adapter.verify(access_token="access-secret", model="gpt-6.1-sol")
    elapsed = time.monotonic() - started

    assert timed_out.value.code == "provider_timeout"
    assert total_timeout <= elapsed <= total_timeout + read_timeout + 0.05


def test_verification_stops_reading_at_response_completed():
    terminal = (
        b'data: {"type":"response.completed","response":{"output":[{"type":"message",'
        b'"content":[{"type":"output_text","text":"{\\"ok\\":true}"}]}]}}\n\n'
    )
    stream = TerminalThenExtraStream(terminal)
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
    )

    adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert stream.read_after_terminal is False


@pytest.mark.parametrize(
    "events,expected_code,expected_status",
    [
        (
            'data: {"type":"response.failed","response":{"error":'
            '{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n',
            "subscription_sharing_usage_limit_exceeded",
            429,
        ),
        (
            'data: {"type":"response.completed","response":{"output":[]}}\n\n',
            "verification_invalid_output",
            502,
        ),
        ('data: {"type":"response.incomplete"}\n\n', "verification_incomplete", 502),
        (
            'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\n',
            "verification_incomplete",
            502,
        ),
    ],
)
def test_verification_errors_are_provider_specific_and_never_retried(
    events, expected_code, expected_status
):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=events.encode("utf-8"),
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ChatGPTError) as failed:
        adapter.verify(access_token="access-secret", model="gpt-6.1-sol")

    assert failed.value.code == expected_code
    assert failed.value.status_code == expected_status
    assert calls == 1


def test_provider_401_is_mapped_to_non_401_with_its_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "subscription_sharing_invalid_user"}},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    with adapter._client() as client, pytest.raises(ChatGPTError) as rejected:
        adapter._json_request(
            client,
            "GET",
            f"{API_BASE_URL}/models",
            headers={"Authorization": "Bearer access-secret"},
        )

    assert rejected.value.status_code == 409
    assert rejected.value.code == "subscription_sharing_invalid_user"
    assert "access-secret" not in rejected.value.message


def test_failed_stream_retains_safe_request_and_parameter_diagnostics():
    event = {
        "type": "response.failed",
        "response": {
            "error": {
                "code": "subscription_sharing_unsupported_capability",
                "param": "text.format",
                "message": "private raw provider detail",
            }
        },
    }
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content="data: " + json.dumps(event) + "\n\n",
                headers={"Content-Type": "text/event-stream", "x-request-id": "req_stream123"},
            )
        )
    )
    with pytest.raises(ChatGPTError) as failure:
        adapter.verify(access_token="test-token", model="test-model")
    assert failure.value.diagnostics == {
        "body_shape": "error",
        "http_status": 200,
        "request_id": "req_stream123",
        "param": "text.format",
    }
    assert "private" not in str(failure.value)
    assert "private" not in json.dumps(failure.value.diagnostics)


@pytest.mark.parametrize(
    "override",
    [
        {"id_token_signing_alg_values_supported": None},
        {"revocation_endpoint": "https://auth.openai.com:invalid/revoke"},
        {"jwks_uri": "https://[invalid"},
    ],
)
def test_malformed_discovery_is_a_safe_provider_error(override):
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_URL,
        "token_endpoint": TOKEN_URL,
        "jwks_uri": "https://auth.openai.com/jwks",
        "revocation_endpoint": "https://auth.openai.com/revoke",
        "id_token_signing_alg_values_supported": ["RS256"],
        **override,
    }
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    )
    with pytest.raises(ChatGPTError) as failure:
        adapter.revoke(client_id=CLIENT_ID, refresh_token="test-refresh")
    assert failure.value.code == "provider_discovery_invalid"
