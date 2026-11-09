from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from notes_helpers import StubNotesProvider, get_notes_source, seed_completed_transcript

from asr_studio import antigravity as antigravity_module
from asr_studio import chatgpt as chatgpt_module
from asr_studio import notes as notes_module
from asr_studio import notes_document as nd
from asr_studio.antigravity import AntigravityError, AntigravityProvider, OmpRpcAdapter
from asr_studio.chatgpt import (
    DIRECT_SCOPE,
    ChatGPTError,
    ChatGPTHTTPAdapter,
    ChatGPTProvider,
)
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.storage import database_path


def _snapshot(text_bytes: int) -> dict:
    return {
        "meeting_date": "2026-10-19",
        "segments": [
            {
                "id": "0" * 32,
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": "x" * text_bytes,
            }
        ],
    }


def _many_segment_snapshot(*, repeats: int = 5, count: int = 1_706) -> dict:
    return {
        "meeting_date": "2026-10-19",
        "segments": [
            {
                "id": f"{index:032x}",
                "start_seconds": float(index),
                "end_seconds": float(index + 1),
                "text": f"segment {index:04d} " + "discussion " * repeats,
            }
            for index in range(count)
        ],
    }


def _empty_document() -> dict:
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _document_stream(document: dict) -> bytes:
    delta = {
        "type": "response.output_text.delta",
        "delta": json.dumps(document, ensure_ascii=False, separators=(",", ":")),
    }
    completed = {"type": "response.completed", "response": {"output": [], "usage": {}}}
    return b"".join(
        b"data: " + json.dumps(event).encode("utf-8") + b"\n\n" for event in (delta, completed)
    )


class _ProviderNotesAdapter:
    def __init__(self) -> None:
        self.last_notes_usage: dict[str, int] = {}
        self.generate_calls = 0
        self.generate_messages: list[str] = []

    def generate_notes(self, **_kwargs: object) -> str:
        return json.dumps(_empty_document())

    def generate(self, _model: str, message: str, _thinking_level: str = "high") -> str:
        self.generate_calls += 1
        self.generate_messages.append(message)
        return json.dumps(_empty_document())


def _connected_chatgpt_provider(tmp_path, adapter: _ProviderNotesAdapter) -> ChatGPTProvider:
    provider = ChatGPTProvider(tmp_path / "chatgpt", callback_url="", adapter=adapter)
    provider.ensure()
    with provider.store.locked(write=True) as state:
        state["connection_status"] = "connected"
        state["connection_version"] = 7
        state["active_client_id"] = "test-client"
        state["registrations"]["test-client"] = {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "expires_at": 10**12,
            "scopes": [DIRECT_SCOPE],
            "subject": "test-subject",
            "label": "Test ChatGPT",
        }
    return provider


def _connected_antigravity_provider(
    tmp_path, adapter: _ProviderNotesAdapter
) -> AntigravityProvider:
    provider = AntigravityProvider(tmp_path / "antigravity", adapter=adapter)
    provider.ensure()
    with provider.store.locked(write=True) as state:
        state["connection_status"] = "connected"
        state["connection_version"] = 0
    return provider


def _independent_request_bytes(
    prompt: str,
    allowed_source_ids: tuple[str, ...],
    *,
    provider: str,
    model: str,
    budget: nd.NotesBudget,
) -> int:
    schema = nd.build_notes_schema(allowed_source_ids)
    if provider == "chatgpt":
        body = {
            "model": model,
            "max_output_tokens": budget.output_reserve_tokens,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "meeting_notes_v1",
                    "strict": True,
                    "schema": schema,
                }
            },
            "store": False,
            "stream": True,
            "reasoning": {"effort": "xhigh"},
        }
        return len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
    message = nd.build_notes_provider_message(prompt, schema)
    frame = {"id": "x" * 16, "type": "prompt", "message": message}
    return len(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode()) + 1


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_realistic_many_segment_source_uses_grouped_strict_single_schema(provider, model):
    snapshot = _many_segment_snapshot(count=1_779, repeats=1)
    projection = nd.select_notes_source_projection(
        snapshot,
        budget=nd.notes_budget(provider, model),
    )
    projected_ids = [segment["id"] for segment in projection.snapshot["segments"]]
    unit_ids = tuple(dict.fromkeys(projected_ids))

    assert len(projected_ids) == 1_779
    assert len(unit_ids) == 890
    assert len(unit_ids) <= nd.MAX_SOURCE_IDS_PER_SCOPE
    assert all(
        projected_ids[index] == projected_ids[index + 1]
        for index in range(0, len(projected_ids), 2)
        if index + 1 < len(projected_ids)
    )
    assert projection.alias_to_original[unit_ids[0]] == (
        snapshot["segments"][0]["id"],
        snapshot["segments"][1]["id"],
    )
    assert [
        projection.snapshot["segments"][index][field]
        for index in (0, 1)
        for field in ("start_seconds", "end_seconds", "text")
    ] == [
        snapshot["segments"][index][field]
        for index in (0, 1)
        for field in ("start_seconds", "end_seconds", "text")
    ]

    plan = nd.plan_source(snapshot, provider=provider, model=model)
    other_model = "gpt-5.6-other" if provider == "chatgpt" else "gemini-3.8-pro"
    other_plan = nd.plan_source(snapshot, provider=provider, model=other_model)

    assert plan.chunked is False
    assert other_plan.chunked is True
    schema = nd.build_notes_schema(unit_ids)
    assert schema["$defs"]["source_id"] == {"type": "string", "enum": list(unit_ids)}
    assert schema["properties"]["overview"]["items"]["properties"]["source_ids"]["items"] == {
        "$ref": "#/$defs/source_id"
    }
    assert "unknown-id" not in schema["$defs"]["source_id"]["enum"]

    document = _empty_document()
    document["overview"] = [{"text": "unit", "source_ids": [unit_ids[0]]}]
    restored = nd.restore_source_refs(document, projection.alias_to_original)
    assert restored["overview"][0]["source_ids"] == [
        snapshot["segments"][0]["id"],
        snapshot["segments"][1]["id"],
    ]


@pytest.mark.parametrize("count", [999, 1_000])
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_budgeted_citation_unit_boundary_keeps_owner_plan_single(provider, model, count):
    snapshot = _many_segment_snapshot(count=count)
    budget = nd.notes_budget(provider, model)
    projection = nd.select_notes_source_projection(snapshot, budget=budget)
    ids = tuple(dict.fromkeys(segment["id"] for segment in projection.snapshot["segments"]))

    assert nd.plan_source(snapshot, provider=provider, model=model).chunked is False
    assert len(ids) == count if count == 999 else len(ids) == 500
    if count == 999:
        assert all(isinstance(value, str) for value in projection.alias_to_original.values())
    else:
        assert all(isinstance(value, tuple) for value in projection.alias_to_original.values())
    schema = nd.build_notes_schema(ids)
    assert len(schema["$defs"]["source_id"]["enum"]) <= nd.MAX_SOURCE_IDS_PER_SCOPE


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_unknown_id_in_large_single_pass_uses_the_existing_repair(provider, model):
    snapshot = _many_segment_snapshot()
    empty = _empty_document()
    invalid = json.loads(json.dumps(empty))
    invalid["overview"] = [{"text": "untrusted", "source_ids": ["unknown-id"]}]
    calls: list[dict[str, object]] = []

    def transport(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return json.dumps(invalid if len(calls) == 1 else empty)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
    )

    assert result == empty
    expected = [("single", "primary"), ("single", "repair")]
    expected.append(("audit", "primary"))
    assert [(call["stage"], call["attempt"]) for call in calls] == expected
    assert all(call["budget"].uses_token_estimate for call in calls)
    assert len(calls[0]["allowed_source_ids"]) == 853


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_grouped_citation_limit_accepts_100_units_expanding_to_200(provider, model):
    snapshot = _many_segment_snapshot(count=1_000)
    projection = nd.select_notes_source_projection(
        snapshot,
        budget=nd.notes_budget(provider, model),
    )
    unit_ids = tuple(dict.fromkeys(segment["id"] for segment in projection.snapshot["segments"]))
    schema = nd.build_notes_schema(unit_ids, max_unit_size=projection.max_unit_size)
    source_refs = schema["properties"]["overview"]["items"]["properties"]["source_ids"]
    calls: list[dict[str, object]] = []

    def transport(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        document = _empty_document()
        if kwargs["stage"] == "audit":
            payload = json.loads(_prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
            text = (
                payload["audit_catalog"]["draft_candidates"][0]["ref"]
                if provider == "antigravity"
                else payload["summaries"][0]["notes"]["overview"][0]["text"]
            )
        else:
            text = "one hundred units"
        document["overview"] = [{"text": text, "source_ids": list(unit_ids[:100])}]
        return json.dumps(document)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=AssertionError,
        limit_error=RuntimeError,
    )

    assert projection.max_unit_size == 2
    assert source_refs["maxItems"] == nd.MAX_SOURCE_REFS_PER_ITEM // 2
    assert len(calls) == 2
    assert all(call["max_unit_size"] == 2 for call in calls)
    assert result["overview"][0]["source_ids"] == [
        segment["id"] for segment in snapshot["segments"][:200]
    ]


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_grouped_citation_limit_rejects_101_units_at_ingress_and_repairs(provider, model):
    snapshot = _many_segment_snapshot(count=1_000)
    projection = nd.select_notes_source_projection(
        snapshot,
        budget=nd.notes_budget(provider, model),
    )
    unit_ids = tuple(dict.fromkeys(segment["id"] for segment in projection.snapshot["segments"]))
    calls: list[dict[str, object]] = []

    def transport(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        document = _empty_document()
        if len(calls) == 1:
            document["overview"] = [{"text": "too many units", "source_ids": list(unit_ids[:101])}]
        return json.dumps(document)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
    )

    assert result == _empty_document()
    expected = [("single", "primary"), ("single", "repair")]
    expected.append(("audit", "primary"))
    assert [(call["stage"], call["attempt"]) for call in calls] == expected
    assert all(call["max_unit_size"] == 2 for call in calls)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_short_owner_pipeline_runs_one_qualified_source_audit(provider, model):
    snapshot = _many_segment_snapshot(count=10)
    calls: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []

    def transport(prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        if kwargs["stage"] == "single":
            document = _empty_document()
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            document["overview"] = [{"text": "Draft", "source_ids": [source["segments"][0]["id"]]}]
            return json.dumps(document)

        assert kwargs["stage"] == "audit"
        payload = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
        assert payload["source"] == nd.provider_source(
            nd.select_notes_source_projection(
                snapshot, budget=nd.notes_budget(provider, model)
            ).snapshot
        )
        assert payload["summaries"][0]["notes"]["overview"][0]["text"] == "Draft"
        document = _empty_document()
        if provider == "antigravity":
            ref = payload["audit_catalog"]["draft_candidates"][0]["ref"]
        else:
            assert "audit_catalog" not in payload
            ref = "Draft"
        document["overview"] = [
            {"text": ref, "source_ids": [payload["source"]["segments"][0]["id"]]}
        ]
        return json.dumps(document)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=AssertionError,
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )

    assert result["overview"][0]["text"] == "Draft"
    assert [call["stage"] for call in calls] == ["single", "audit"]
    assert coverage[0]["provider_calls"] == 2
    assert coverage[0]["source_audit"] == {
        "status": "applied",
        "provider_call_index": 2,
        "mode": "references" if provider == "antigravity" else "rewrite",
    }
    assert coverage[0]["output_recoveries"] == 0


def test_source_audit_failure_is_terminal_instead_of_publishing_an_unsupported_addition():
    snapshot = _snapshot(0)
    snapshot["segments"][0]["text"] = "A checklist is required for the rollout."
    calls = []

    def transport(_prompt, **kwargs):
        calls.append(kwargs["stage"])
        document = _empty_document()
        text = "A checklist is required for the rollout."
        if kwargs["stage"] == "audit":
            text = "A biometric reader was installed for the rollout."
        document["key_topics"] = [{"text": text, "source_ids": [kwargs["allowed_source_ids"][0]]}]
        return json.dumps(document)

    with pytest.raises(AssertionError) as failure:
        generate_notes_document(
            snapshot=snapshot,
            output_language="en",
            provider="antigravity",
            model="gemini-3.8-flash",
            transport=transport,
            invalid_output_error=AssertionError,
            limit_error=RuntimeError,
        )

    assert calls == ["single", "audit", "audit"]
    assert failure.value.args[0]["stage"] == "audit"


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_short_owner_source_audit_reports_why_it_is_skipped(provider, model, monkeypatch):
    snapshot = _snapshot(0)
    real_budget = nd.notes_budget(provider, model)
    projection = nd.select_notes_source_projection(snapshot, budget=real_budget)
    scope = tuple(segment["id"] for segment in projection.snapshot["segments"])
    single_prompt = nd._source_prompt(projection.snapshot, budget=real_budget)
    single_repair = nd._repair_prompt(single_prompt, nd._worst_case_repair_output())
    schema_bytes = nd.notes_schema_json_bytes(scope)
    single_limit = (
        nd.estimate_notes_tokens(len(single_repair.encode()) + schema_bytes, real_budget)
        + real_budget.output_reserve_tokens
        + nd.NOTES_CONTEXT_HEADROOM_TOKENS
    )
    constrained = replace(
        real_budget,
        context_window_tokens=single_limit,
        usable_context_tokens=single_limit,
    )
    monkeypatch.setitem(nd.NOTES_BUDGETS, (provider, model), constrained)
    calls: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []

    def transport(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        assert "SOURCE_JSON:\n" in _prompt
        document = _empty_document()
        document["overview"] = [
            {"text": ("x" * 10_000) + str(index), "source_ids": []} for index in range(10)
        ]
        return json.dumps(document)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=AssertionError,
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )

    assert len(result["overview"]) == 10
    assert result["overview"][0]["text"].startswith("x" * 10_000)
    assert len(calls) == 1 and calls[0]["stage"] == "single"
    assert coverage[0]["provider_calls"] == 1
    assert coverage[0]["source_audit"] == {"status": "skipped", "reason": "prompt_budget"}


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_acceptance_and_runtime_plans_match_independent_boundary_arithmetic(
    provider, model, monkeypatch
):
    real_budget = nd.notes_budget(provider, model)
    boundary_budget = replace(
        real_budget,
        context_window_tokens=100_000,
        usable_context_tokens=100_000,
    )
    monkeypatch.setitem(nd.NOTES_BUDGETS, (provider, model), boundary_budget)

    def expected_single(size: int) -> bool:
        snapshot = _snapshot(size)
        projected = nd.project_source_snapshot(snapshot).snapshot
        scope = tuple(segment["id"] for segment in projected["segments"])
        prompt = nd._source_prompt(projected, budget=boundary_budget)
        repair_prompt = prompt + nd._repair_prefix("en") + "\x00" * nd.MAX_REPAIR_OUTPUT_BYTES
        schema_bytes = len(
            json.dumps(
                nd.build_notes_schema(scope),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        estimated_tokens = (
            (len(repair_prompt.encode("utf-8")) + schema_bytes) * nd.NOTES_TOKEN_ESTIMATE_NUMERATOR
            + nd.NOTES_TOKEN_ESTIMATE_DENOMINATOR
            - 1
        ) // nd.NOTES_TOKEN_ESTIMATE_DENOMINATOR
        return (
            estimated_tokens
            + boundary_budget.output_reserve_tokens
            + nd.NOTES_CONTEXT_HEADROOM_TOKENS
            <= boundary_budget.usable_context_tokens
            and _independent_request_bytes(
                prompt,
                scope,
                provider=provider,
                model=model,
                budget=boundary_budget,
            )
            <= boundary_budget.request_guard_bytes
            and _independent_request_bytes(
                repair_prompt,
                scope,
                provider=provider,
                model=model,
                budget=boundary_budget,
            )
            <= boundary_budget.request_guard_bytes
        )

    low, high = 0, 100_000
    while expected_single(high):
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if expected_single(middle):
            low = middle
        else:
            high = middle

    for size, expected in ((low, True), (high, False)):
        snapshot = _snapshot(size)
        acceptance_plan = notes_module.plan_source(snapshot, provider=provider, model=model)
        runtime_plan = nd.plan_source(
            snapshot,
            projected_snapshot=nd.project_source_snapshot(snapshot).snapshot,
            provider=provider,
            model=model,
        )
        assert expected_single(size) is expected
        assert acceptance_plan.chunked is (not expected)
        assert runtime_plan == acceptance_plan


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_owner_models_make_a_single_plan_above_the_conservative_budget(provider, model):
    snapshot = _snapshot(70_000)

    owner_plan = nd.plan_source(snapshot, provider=provider, model=model)
    conservative_plan = nd.plan_source(snapshot, provider=provider, model="different-model")

    assert owner_plan.chunked is False
    assert conservative_plan.chunked is True


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-other"),
        ("antigravity", "gemini-3.8-pro"),
    ],
)
def test_non_owner_models_keep_the_conservative_chunk_plan(provider, model):
    snapshot = _snapshot(70_000)

    assert nd.plan_source(snapshot, provider=provider, model=model) == nd.plan_source(snapshot)


def test_budget_lookup_and_estimator_keep_exact_owner_keys_and_ceil():
    chatgpt_budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    antigravity_budget = nd.notes_budget("antigravity", "gemini-3.8-flash")
    fallback = nd.notes_budget("chatgpt", "gpt-5.6-other")

    assert chatgpt_budget.model == "gpt-5.6-sol"
    assert antigravity_budget.model == "gemini-3.8-flash"
    assert chatgpt_budget.uses_token_estimate is True
    assert antigravity_budget.uses_token_estimate is True
    assert fallback.uses_token_estimate is False
    assert fallback.output_reserve_tokens == nd.NOTES_MAX_OUTPUT_TOKENS
    assert nd.estimate_notes_tokens(1, chatgpt_budget) == 1
    assert nd.estimate_notes_tokens(4, chatgpt_budget) == 3


def test_chatgpt_boundary_is_derived_from_the_owner_budget():
    budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    assert len(nd.NOTES_BUDGETS) == 2

    # Convert the available estimated-token input allowance back to a byte search bound.
    high = (
        (
            budget.usable_context_tokens
            - budget.output_reserve_tokens
            - nd.NOTES_CONTEXT_HEADROOM_TOKENS
        )
        * nd.NOTES_TOKEN_ESTIMATE_DENOMINATOR
        // nd.NOTES_TOKEN_ESTIMATE_NUMERATOR
        * 2
    )

    def fits(size: int) -> bool:
        return not nd.plan_source(_snapshot(size), provider="chatgpt", model="gpt-5.6-sol").chunked

    low = 0
    while low + 1 < high:
        middle = (low + high) // 2
        if fits(middle):
            low = middle
        else:
            high = middle

    assert fits(low)
    assert not fits(high)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_owner_fit_checks_output_headroom_and_worst_case_repair(provider, model):
    owner_budget = nd.notes_budget(provider, model)
    projected = nd.project_source_snapshot(_snapshot(20_000)).snapshot
    scope = tuple(segment["id"] for segment in projected["segments"])
    prompt = nd._source_prompt(projected)
    schema_bytes = nd.notes_schema_json_bytes(scope)
    primary_estimate = nd.estimate_notes_tokens(
        len(prompt.encode("utf-8")) + schema_bytes,
        owner_budget,
    )
    repair_prompt = nd._repair_prompt(prompt, nd._worst_case_repair_output())
    repair_estimate = nd.estimate_notes_tokens(
        len(repair_prompt.encode("utf-8")) + schema_bytes,
        owner_budget,
    )
    primary_budget = replace(
        owner_budget,
        usable_context_tokens=(
            primary_estimate
            + owner_budget.output_reserve_tokens
            + nd.NOTES_CONTEXT_HEADROOM_TOKENS
            - 1
        ),
    )
    tight_budget = replace(
        owner_budget,
        usable_context_tokens=(
            repair_estimate
            + owner_budget.output_reserve_tokens
            + nd.NOTES_CONTEXT_HEADROOM_TOKENS
            - 1
        ),
    )

    with pytest.raises(nd.NotesDocumentError, match="safe model context"):
        nd.build_prompt(projected, allowed_source_ids=scope, budget=primary_budget)
    with pytest.raises(nd.NotesDocumentError, match="bounded schema repair"):
        nd._check_prompt(
            prompt,
            allowed_source_ids=scope,
            reserve_repair=True,
            budget=tight_budget,
        )
    assert nd.plan_source(
        _snapshot(20_000),
        projected_snapshot=projected,
        budget=tight_budget,
    ).chunked


def test_chatgpt_runtime_checks_prompt_body_and_usable_context_guards():
    owner_budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    schema = nd.build_notes_schema(())
    prompt = "x" * 50_000
    actual_body = nd.build_chatgpt_notes_request_body(
        model=owner_budget.model or "",
        prompt=prompt,
        schema=schema,
        reasoning_effort="medium",
        budget=owner_budget,
    )
    actual_body_bytes = len(
        json.dumps(actual_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )

    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(lambda _request: pytest.fail("transport must not run"))
    )
    with pytest.raises(ChatGPTError) as body_failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt=prompt,
            allowed_source_ids=(),
            budget=replace(owner_budget, request_guard_bytes=actual_body_bytes - 1),
        )
    assert body_failure.value.code == "notes_request_too_large"

    with pytest.raises(ChatGPTError) as prompt_failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt="x" * 1_001,
            allowed_source_ids=(),
            budget=replace(owner_budget, request_guard_bytes=1_000),
        )
    assert prompt_failure.value.code == "notes_request_too_large"

    with pytest.raises(ChatGPTError) as context_failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt=prompt,
            allowed_source_ids=(),
            budget=replace(
                owner_budget,
                context_window_tokens=110_000,
                usable_context_tokens=100_000,
            ),
        )
    assert context_failure.value.code == "notes_context_budget_exceeded"


def _notes_stream(usage: dict[str, object]) -> bytes:
    event = {
        "type": "response.completed",
        "response": {"output": [], "usage": usage},
    }
    return b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"


def test_chatgpt_owner_transport_uses_large_output_and_body_budgets():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_notes_stream(
                {
                    "input_tokens": 120_000,
                    "output_tokens": 321,
                    "output_tokens_details": {"reasoning_tokens": 123},
                }
            ),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    adapter.generate_notes(
        access_token="test-token",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        prompt="x" * 200_000,
        allowed_source_ids=(),
    )

    body = json.loads(requests[0].content)
    assert body["max_output_tokens"] == 65_536
    assert len(requests[0].content) <= nd.notes_budget("chatgpt", "gpt-5.6-sol").request_guard_bytes
    assert adapter.last_notes_usage == {
        "input_tokens": 120_000,
        "output_tokens": 321,
        "reasoning_tokens": 123,
    }


def test_owner_and_conservative_sse_and_diagnostic_limits_are_distinct():
    from asr_studio.chatgpt import _notes_sse_body_limit, _safe_notes_call_diagnostics

    owner = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    conservative = nd.conservative_notes_budget("chatgpt")
    custom_conservative = replace(conservative, output_reserve_tokens=123)
    assert _notes_sse_body_limit(owner) == owner.output_reserve_tokens * 512 + 1024 * 1024
    assert _notes_sse_body_limit(conservative) == 25_000 * 512 + 1024 * 1024
    assert _notes_sse_body_limit(custom_conservative) == chatgpt_module.NOTES_MAX_SSE_BODY_BYTES

    owner_diagnostics = _safe_notes_call_diagnostics(
        {"stage": "single", "prompt_bytes": 200_000}, budget=owner
    )
    conservative_diagnostics = _safe_notes_call_diagnostics(
        {"stage": "chunk", "prompt_bytes": 200_000}, budget=conservative
    )
    assert owner_diagnostics["prompt_bytes"] == 200_000
    assert "prompt_bytes" not in conservative_diagnostics


def test_chatgpt_usage_is_reset_before_each_request():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        usage = {"output_tokens": calls}
        if calls == 1:
            usage.update({"input_tokens": 100, "output_tokens_details": {"reasoning_tokens": 50}})
        return httpx.Response(
            200,
            content=_notes_stream(usage),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    for _ in range(2):
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt="small prompt",
            allowed_source_ids=(),
        )
    assert adapter.last_notes_usage == {"output_tokens": 2}


def test_chatgpt_usage_is_read_from_incomplete_terminal_response():
    event = {
        "type": "response.incomplete",
        "response": {
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {
                "input_tokens": 180_000,
                "output_tokens": 65_536,
                "output_tokens_details": {"reasoning_tokens": 60_000},
            },
        },
    }
    stream = b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=stream,
                headers={"content-type": "text/event-stream"},
            )
        )
    )

    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt="small prompt",
            allowed_source_ids=(),
        )

    assert failure.value.code == "notes_token_budget_exceeded"
    assert adapter.last_notes_usage == {
        "input_tokens": 180_000,
        "output_tokens": 65_536,
        "reasoning_tokens": 60_000,
    }


@pytest.mark.parametrize(
    ("provider", "model", "language", "clause"),
    [
        (
            "chatgpt",
            "gpt-5.6-sol",
            "en",
            "This transcript is the entire meeting in one request.",
        ),
        (
            "chatgpt",
            "gpt-5.6-sol",
            "vi",
            "Transcript này là toàn bộ cuộc họp trong một yêu cầu.",
        ),
        (
            "antigravity",
            "gemini-3.8-flash",
            "en",
            "This transcript is the entire meeting in one request.",
        ),
        (
            "antigravity",
            "gemini-3.8-flash",
            "vi",
            "Transcript này là toàn bộ cuộc họp trong một yêu cầu.",
        ),
    ],
)
def test_completeness_clause_is_only_in_budgeted_single_pass(provider, model, language, clause):
    snapshot = _snapshot(0)
    owner = nd.notes_budget(provider, model)
    conservative = nd.conservative_notes_budget(provider)

    ordinary = nd.build_prompt(snapshot, output_language=language)
    conservative_prompt = nd.build_prompt(snapshot, output_language=language, budget=conservative)
    owner_prompt = nd.build_prompt(snapshot, output_language=language, budget=owner)
    chunk = nd.build_chunk_prompt(
        snapshot,
        chunk_index=1,
        chunk_count=1,
        output_language=language,
    )
    owner_chunk = nd.build_chunk_prompt(
        snapshot,
        chunk_index=1,
        chunk_count=1,
        output_language=language,
        budget=owner,
    )
    empty_document = _empty_document()
    reduce = nd.build_reduction_prompt(
        snapshot,
        [empty_document],
        final=False,
        output_language=language,
    )
    owner_reduce = nd.build_reduction_prompt(
        snapshot,
        [empty_document],
        final=False,
        output_language=language,
        budget=owner,
    )

    assert ordinary == conservative_prompt
    assert clause in owner_prompt
    assert clause not in ordinary
    # The registered Gemini source rule is the only map-prompt difference.
    gemini_rule = nd._KNOWN_GEMINI_RULE[language] if provider == "antigravity" else ""
    assert gemini_rule in owner_chunk
    assert chunk == owner_chunk.replace(gemini_rule, "", 1)
    assert reduce == owner_reduce


def test_planning_and_runtime_use_the_same_owner_single_pass_prompt(monkeypatch):
    snapshot = _snapshot(0)
    projected = nd.project_source_snapshot(snapshot).snapshot
    owner = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    planned_prompts: list[str] = []
    real_fits = nd._fits_repairable_prompt

    def record_prompt(prompt, allowed_source_ids, **kwargs):
        planned_prompts.append(prompt)
        return real_fits(prompt, allowed_source_ids, **kwargs)

    monkeypatch.setattr(nd, "_fits_repairable_prompt", record_prompt)
    plan = nd.plan_source(snapshot, provider="chatgpt", model="gpt-5.6-sol")

    actual_prompt = nd.build_prompt(
        projected,
        allowed_source_ids=tuple(segment["id"] for segment in projected["segments"]),
        budget=owner,
    )
    assert plan.chunked is False
    assert planned_prompts == [actual_prompt]


def test_antigravity_invalid_output_logs_bounded_codes_without_text(caplog, tmp_path):
    class InvalidAdapter(_ProviderNotesAdapter):
        def generate(self, _model: str, message: str, _thinking_level: str = "high") -> str:
            self.generate_calls += 1
            self.generate_messages.append(message)
            invalid = _empty_document()
            invalid["overview"] = [
                {
                    "text": "SECRET_OUTPUT_TEXT",
                    "source_ids": ["private-unknown-source-id"],
                }
            ]
            return json.dumps(invalid)

    caplog.set_level(logging.WARNING, logger="asr_studio.notes_pipeline")
    snapshot = _snapshot(0)
    snapshot["segments"][0]["text"] = "SECRET_PROMPT_TEXT"
    adapter = InvalidAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)

    with pytest.raises(AntigravityError) as failure:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=0,
            model="gemini-3.8-flash",
            reasoning_effort="high",
        )

    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "provider=antigravity" in message
    assert "model=gemini-3.8-flash" in message
    assert "validation_reason=source_ids" in message
    assert "validation_detail=unknown_source_id" in message
    assert "stage=single" in message
    assert "attempt=repair" in message
    assert "provider_call_index=2" in message
    assert "prompt_bytes=" in message
    assert "output_bytes=" in message
    assert "response_limit=128000" in message
    assert "SECRET_OUTPUT_TEXT" not in message
    assert "private-unknown-source-id" not in message
    assert "SECRET_PROMPT_TEXT" not in message
    assert failure.value.diagnostics["validation_reason"] == "source_ids"


def test_chatgpt_invalid_output_logs_bounded_codes_without_text(caplog, tmp_path):
    class InvalidAdapter(_ProviderNotesAdapter):
        def generate_notes(self, **_kwargs: object) -> str:
            invalid = _empty_document()
            invalid["overview"] = [
                {
                    "text": "SECRET_OUTPUT_TEXT",
                    "source_ids": ["private-unknown-source-id"],
                }
            ]
            return json.dumps(invalid)

    caplog.set_level(logging.WARNING, logger="asr_studio.notes_pipeline")
    snapshot = _snapshot(0)
    snapshot["segments"][0]["text"] = "SECRET_PROMPT_TEXT"
    provider = _connected_chatgpt_provider(tmp_path, InvalidAdapter())

    with pytest.raises(ChatGPTError) as failure:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=7,
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )

    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "provider=chatgpt" in message
    assert "model=gpt-5.6-sol" in message
    assert "validation_reason=source_ids" in message
    assert "validation_detail=unknown_source_id" in message
    assert "stage=single" in message
    assert "attempt=repair" in message
    assert "provider_call_index=2" in message
    assert "SECRET_OUTPUT_TEXT" not in message
    assert "private-unknown-source-id" not in message
    assert "SECRET_PROMPT_TEXT" not in message
    assert failure.value.diagnostics["validation_reason"] == "source_ids"


def test_budgeted_oversize_pipeline_uses_the_pinned_chatgpt_owner_budget():
    requests: list[httpx.Request] = []
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        prompt = body["input"][0]["content"][0]["text"]
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            document = _empty_document()
            document["overview"] = [
                {
                    "text": "chunk detail " + "x" * 6_800,
                    "source_ids": [source["segments"][0]["id"]],
                }
            ]
        else:
            document = _empty_document()
        return httpx.Response(
            200,
            content=_document_stream(document),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))

    def transport(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt=prompt,
            allowed_source_ids=kwargs["allowed_source_ids"],
            max_output_bytes=kwargs["max_output_bytes"],
            budget=kwargs["budget"],
        )

    # The required reconcile audit cannot carry this whole source, so the run stops at its
    # bounded limit instead of publishing an unaudited draft.
    with pytest.raises(RuntimeError, match="source reconciliation limit"):
        generate_notes_document(
            snapshot=_many_segment_snapshot(repeats=10),
            output_language="en",
            provider="chatgpt",
            model="gpt-5.6-sol",
            transport=transport,
            invalid_output_error=AssertionError,
            limit_error=RuntimeError,
        )

    assert calls
    assert calls[-1]["stage"] == "final"
    assert all(call["budget"].uses_token_estimate is True for call in calls)
    assert all(
        json.loads(request.content)["max_output_tokens"]
        == nd.CHATGPT_GPT_5_6_SOL_OUTPUT_RESERVE_TOKENS
        for request in requests
    )
    assert all(len(request.content) <= 1 * 1024 * 1024 for request in requests)


def test_chunk_recovery_prompt_uses_the_pinned_owner_budget(monkeypatch):
    recovery_budgets: list[nd.NotesBudget] = []
    calls: list[dict[str, object]] = []
    real_recovery_prompt = nd.build_recovery_prompt

    def capture_recovery_prompt(*args: object, **kwargs: object) -> str:
        recovery_budgets.append(kwargs["budget"])
        return real_recovery_prompt(*args, **kwargs)

    monkeypatch.setattr(nd, "build_recovery_prompt", capture_recovery_prompt)

    def transport(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        if len(calls) == 1:
            raise ChatGPTError(
                502,
                "provider_output_too_large",
                "ChatGPT returned an oversized notes document.",
            )
        return json.dumps(_empty_document())

    with pytest.raises(RuntimeError, match="source reconciliation limit"):
        generate_notes_document(
            snapshot=_snapshot(300_000),
            output_language="en",
            provider="chatgpt",
            model="gpt-5.6-sol",
            transport=transport,
            invalid_output_error=AssertionError,
            limit_error=RuntimeError,
        )

    assert recovery_budgets
    assert recovery_budgets[0].uses_token_estimate is True
    assert calls[0]["stage"] == calls[1]["stage"] == "chunk"
    assert calls[1]["attempt"] == "recovery"


def test_budgeted_single_pipeline_inspects_the_large_chatgpt_body():
    requests: list[httpx.Request] = []
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_document_stream(_empty_document()),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))

    def transport(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt=prompt,
            allowed_source_ids=kwargs["allowed_source_ids"],
            max_output_bytes=kwargs["max_output_bytes"],
            budget=kwargs["budget"],
        )

    generate_notes_document(
        snapshot=_many_segment_snapshot(),
        output_language="en",
        provider="chatgpt",
        model="gpt-5.6-sol",
        transport=transport,
        invalid_output_error=AssertionError,
        limit_error=RuntimeError,
    )

    assert len(calls) == len(requests) == 2
    assert [call["stage"] for call in calls] == ["single", "audit"]
    assert all(call["budget"].uses_token_estimate is True for call in calls)
    for request in requests:
        body = json.loads(request.content)
        assert body["max_output_tokens"] == 65_536
        assert body["text"]["format"]["schema"]["properties"]["overview"]["items"]["properties"][
            "source_ids"
        ]["items"] == {"$ref": "#/$defs/source_id"}
        assert len(body["text"]["format"]["schema"]["$defs"]["source_id"]["enum"]) <= 999
        assert len(request.content) <= nd.notes_budget("chatgpt", "gpt-5.6-sol").request_guard_bytes


def test_antigravity_frame_guard_reserves_non_ascii_repair_payload():
    budget = nd.notes_budget("antigravity", "gemini-3.8-flash")
    snapshot = nd.project_source_snapshot(_snapshot(0)).snapshot
    scope = tuple(segment["id"] for segment in snapshot["segments"])
    prompt = nd._source_prompt(snapshot, budget=budget)
    non_ascii_repair = "é" * 4_000  # exactly 8,000 UTF-8 bytes
    actual_repair = nd._repair_prompt(prompt, non_ascii_repair)
    worst_repair = nd._repair_prompt(prompt, nd._worst_case_repair_output())
    actual_frame = nd.notes_request_body_bytes(actual_repair, scope, budget=budget)
    worst_frame = nd.notes_request_body_bytes(worst_repair, scope, budget=budget)

    assert len(non_ascii_repair.encode("utf-8")) == nd.MAX_REPAIR_OUTPUT_BYTES
    assert worst_frame > actual_frame
    assert (
        actual_frame
        == len(
            json.dumps(
                {
                    "id": "x" * 16,
                    "type": "prompt",
                    "message": nd.build_notes_provider_message(
                        actual_repair,
                        nd.build_notes_schema(scope),
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        )
        + 1
    )

    at_worst = replace(budget, request_guard_bytes=worst_frame)
    below_worst = replace(budget, request_guard_bytes=worst_frame - 1)
    assert nd.prompt_fits_repair(prompt, allowed_source_ids=scope, budget=at_worst)
    assert not nd.prompt_fits_repair(prompt, allowed_source_ids=scope, budget=below_worst)
    assert (
        nd.build_prompt(
            snapshot,
            allowed_source_ids=scope,
            invalid_output=nd._worst_case_repair_output(),
            budget=at_worst,
        )
        == worst_repair
    )
    with pytest.raises(nd.NotesDocumentError, match="request-size guard"):
        nd.build_prompt(
            snapshot,
            allowed_source_ids=scope,
            invalid_output=nd._worst_case_repair_output(),
            budget=below_worst,
        )


def test_chatgpt_other_model_keeps_conservative_transport_caps():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_notes_stream({}),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    adapter.generate_notes(
        access_token="test-token",
        model="gpt-5.6-other",
        reasoning_effort="medium",
        prompt="small prompt",
        allowed_source_ids=(),
    )

    body = json.loads(requests[0].content)
    assert body["max_output_tokens"] == nd.NOTES_MAX_OUTPUT_TOKENS
    assert len(requests[0].content) <= 128 * 1024


def test_antigravity_usage_is_read_from_message_end_message():
    class StubClient:
        def __init__(self):
            self.adapter = SimpleNamespace(last_notes_usage={})
            self.sent: dict[str, object] | None = None
            self.step = 0

        def send(self, payload):
            self.sent = payload

        def _read_frame(self, _timeout):
            self.step += 1
            if self.step == 1:
                return {
                    "type": "response",
                    "id": self.sent["id"],
                    "success": True,
                    "data": {"agentInvoked": True},
                }
            if self.step == 2:
                return {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 90_000, "output": 222, "reasoningTokens": 111},
                        "content": [{"type": "text", "text": "{}"}],
                    },
                }
            return {"type": "prompt_result", "id": self.sent["id"], "status": "completed"}

    client = StubClient()
    assert OmpRpcAdapter._prompt_for_text(client, "safe prompt", 5.0) == "{}"
    assert client.adapter.last_notes_usage == {
        "input_tokens": 90_000,
        "output_tokens": 222,
        "reasoning_tokens": 111,
    }


def test_notes_call_log_has_counts_only(caplog):
    from asr_studio.notes_pipeline import log_notes_provider_call

    caplog.set_level(logging.INFO, logger="asr_studio.notes_pipeline")
    log_notes_provider_call(
        provider="chatgpt",
        model="gpt-5.6-sol",
        stage="single",
        attempt="primary",
        provider_call_index=1,
        prompt_bytes=200_000,
        estimated_input_tokens=140_000,
        usage={
            "input_tokens": 120_000,
            "output_tokens": 321,
            "reasoning_tokens": 123,
            "private_usage_text": "PRIVATE_USAGE_SENTINEL",
        },
    )

    message = caplog.records[-1].getMessage()
    assert "provider=chatgpt" in message
    assert "estimated_input_tokens=140000" in message
    assert "input_tokens=120000" in message
    assert "prompt text" not in message
    assert "output text" not in message
    assert "PRIVATE_USAGE_SENTINEL" not in message


def test_chatgpt_provider_wires_pin_and_count_only_logging(monkeypatch, tmp_path):
    adapter = _ProviderNotesAdapter()
    adapter.last_notes_usage = {"stale": 1}
    provider = _connected_chatgpt_provider(tmp_path, adapter)
    pipeline_call: dict[str, object] = {}
    log_calls: list[dict[str, object]] = []

    def fake_pipeline(**kwargs: object) -> str:
        pipeline_call.update(kwargs)
        result = ""
        for call_index in (1, 2):
            result = kwargs["transport"](
                "SECRET_CHATGPT_PROMPT",
                allowed_source_ids=(),
                max_output_bytes=nd.MAX_OUTPUT_BYTES,
                stage="single",
                attempt="primary",
                provider_call_index=call_index,
                prompt_bytes=len("SECRET_CHATGPT_PROMPT"),
                budget=nd.notes_budget("chatgpt", "gpt-5.6-sol"),
            )
        return result

    monkeypatch.setattr(chatgpt_module, "generate_notes_pipeline", fake_pipeline)
    monkeypatch.setattr(
        chatgpt_module,
        "log_notes_provider_call",
        lambda **kwargs: log_calls.append(kwargs),
    )
    provider.generate_notes_document(
        snapshot=_snapshot(0),
        connection_version=7,
        model="gpt-5.6-sol",
        reasoning_effort="medium",
    )

    assert pipeline_call["provider"] == "chatgpt"
    assert pipeline_call["model"] == "gpt-5.6-sol"
    assert len(log_calls) == 2
    assert all(call["stage"] == "single" for call in log_calls)
    assert all("SECRET_CHATGPT_PROMPT" not in call["stage"] for call in log_calls)
    assert adapter.last_notes_usage == {}


def test_antigravity_provider_wires_pin_and_count_only_logging(monkeypatch, tmp_path):
    adapter = _ProviderNotesAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)
    pipeline_call: dict[str, object] = {}
    log_calls: list[dict[str, object]] = []

    def fake_pipeline(**kwargs: object) -> str:
        pipeline_call.update(kwargs)
        return kwargs["transport"](
            "SECRET_ANTIGRAVITY_MESSAGE",
            allowed_source_ids=(),
            max_output_bytes=nd.MAX_OUTPUT_BYTES,
            stage="single",
            attempt="primary",
            provider_call_index=1,
            prompt_bytes=len("SECRET_ANTIGRAVITY_MESSAGE"),
            budget=nd.notes_budget("antigravity", "gemini-3.8-flash"),
        )

    monkeypatch.setattr(antigravity_module, "generate_notes_pipeline", fake_pipeline)
    monkeypatch.setattr(
        antigravity_module,
        "log_notes_provider_call",
        lambda **kwargs: log_calls.append(kwargs),
    )
    provider.generate_notes_document(
        snapshot=_snapshot(0),
        connection_version=0,
        model="gemini-3.8-flash",
        reasoning_effort="high",
    )

    assert pipeline_call["provider"] == "antigravity"
    assert pipeline_call["model"] == "gemini-3.8-flash"
    assert len(log_calls) == 1
    assert log_calls[0]["stage"] == "single"
    assert "SECRET_ANTIGRAVITY_MESSAGE" not in log_calls[0]["stage"]


def test_antigravity_runtime_enforces_the_rpc_frame_guard(monkeypatch, tmp_path):
    adapter = _ProviderNotesAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)
    real_budget = nd.notes_budget("antigravity", "gemini-3.8-flash")
    actual_frame = nd.notes_request_body_bytes("frame payload", (), budget=real_budget)
    owner_budget = replace(
        real_budget,
        request_guard_bytes=actual_frame - 1,
    )

    def fake_pipeline(**kwargs: object) -> str:
        return kwargs["transport"](
            "frame payload",
            allowed_source_ids=(),
            max_output_bytes=nd.MAX_OUTPUT_BYTES,
            stage="single",
            attempt="primary",
            provider_call_index=1,
            prompt_bytes=len("frame payload"),
            budget=owner_budget,
        )

    monkeypatch.setattr(antigravity_module, "generate_notes_pipeline", fake_pipeline)
    with pytest.raises(AntigravityError) as failure:
        provider.generate_notes_document(
            snapshot=_snapshot(0),
            connection_version=0,
            model="gemini-3.8-flash",
            reasoning_effort="high",
        )
    assert failure.value.code == "provider_request_too_large"
    assert adapter.generate_calls == 0


def test_acceptance_and_runtime_plan_the_pinned_owner_model(tmp_path, monkeypatch):
    class OwnerProvider(StubNotesProvider):
        def notes_task_pin(self):
            return {
                "provider": "chatgpt",
                "connection_version": 7,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
            }

    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    seen: list[tuple[object, object]] = []
    real_plan_source = nd.plan_source

    def record_plan(snapshot, **kwargs):
        seen.append((kwargs.get("provider"), kwargs.get("model")))
        return real_plan_source(snapshot, **kwargs)

    monkeypatch.setattr(notes_module, "plan_source", record_plan)
    monkeypatch.setattr(nd, "plan_source", record_plan)
    accepted = notes_module.accept_notes_task(
        data_dir,
        provider=OwnerProvider(),
        meeting_id=meeting_id,
        request_id="owner-plan-agreement",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )
    with sqlite3.connect(database_path(data_dir)) as connection:
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
    snapshot = get_notes_source(data_dir, source_id)
    empty_document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider="chatgpt",
        model="gpt-5.6-sol",
        transport=lambda *_args, **_kwargs: json.dumps(empty_document),
        invalid_output_error=lambda _diagnostics: ValueError("invalid"),
        limit_error=lambda message: ValueError(message),
    )

    assert result["schema_version"] == 1
    assert seen == [("chatgpt", "gpt-5.6-sol"), ("chatgpt", "gpt-5.6-sol")]


def test_d1_budget_constants_and_provider_guards_are_pinned():
    assert nd.CHATGPT_GPT_5_6_SOL_CONTEXT_WINDOW_TOKENS == 272_000
    assert nd.CHATGPT_GPT_5_6_SOL_USABLE_CONTEXT_TOKENS == 258_400
    assert nd.CHATGPT_GPT_5_6_SOL_OUTPUT_RESERVE_TOKENS == 65_536
    assert nd.CHATGPT_GPT_5_6_SOL_REQUEST_BODY_BYTES == 1_048_576
    assert nd.ANTIGRAVITY_GEMINI_3_8_FLASH_CONTEXT_WINDOW_TOKENS == 1_048_576
    assert nd.ANTIGRAVITY_GEMINI_3_8_FLASH_OUTPUT_RESERVE_TOKENS == 65_536
    assert nd.ANTIGRAVITY_GEMINI_3_8_FLASH_REQUEST_FRAME_BYTES == 1_048_576
    assert nd.conservative_notes_budget("chatgpt").request_guard_bytes == 128 * 1024
    assert nd.conservative_notes_budget("antigravity").request_guard_bytes == 1_048_576
    assert len(nd.NOTES_BUDGETS) == 2


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("chatgpt", "gpt-5.6-sol"),
        ("antigravity", "gemini-3.8-flash"),
    ],
)
def test_owner_schema_at_999_ids_keeps_the_enum(provider, model):
    scope = tuple(f"{index:05d}" for index in range(nd.MAX_SOURCE_IDS_PER_SCOPE))
    schema = nd.build_notes_schema(scope)

    assert schema["$defs"]["source_id"] == {"type": "string", "enum": list(scope)}
    assert schema["properties"]["overview"]["items"]["properties"]["source_ids"]["items"] == {
        "$ref": "#/$defs/source_id"
    }


def test_antigravity_owner_sends_large_scope_schema_in_one_message(tmp_path):
    snapshot = _many_segment_snapshot()
    adapter = _ProviderNotesAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)

    provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=0,
        model="gemini-3.8-flash",
        reasoning_effort="high",
    )

    projection = nd.select_notes_source_projection(
        snapshot,
        budget=nd.notes_budget("antigravity", "gemini-3.8-flash"),
    )
    projected = projection.snapshot
    scope = tuple(dict.fromkeys(segment["id"] for segment in projected["segments"]))
    schema = nd.build_notes_schema(scope, max_unit_size=projection.max_unit_size)
    schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    assert adapter.generate_calls == 2
    assert all(message.endswith(schema_json) for message in adapter.generate_messages)
    assert all(
        json.loads(message.rsplit("\n", 1)[-1]) == schema for message in adapter.generate_messages
    )
    assert len(scope) <= nd.MAX_SOURCE_IDS_PER_SCOPE
    assert json.loads(adapter.generate_messages[0].rsplit("\n", 1)[-1])["$defs"]["source_id"][
        "enum"
    ] == list(scope)


def test_antigravity_unknown_model_stays_within_its_conservative_frame_limit(tmp_path):
    snapshot = _snapshot(0)
    snapshot["segments"][0]["text"] = "\x7f" * 21_200
    adapter = _ProviderNotesAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)
    coverage: list[dict[str, object]] = []

    provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=0,
        model="gemini-3.8-pro",
        reasoning_effort="high",
        on_coverage=coverage.append,
    )

    message = adapter.generate_messages[0]
    frame_bytes = (
        len(
            json.dumps(
                {"id": "x" * 16, "type": "prompt", "message": message},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        )
        + 1
    )
    assert frame_bytes <= 1_048_576
    assert coverage[0]["provider_calls"] >= 1


def test_chatgpt_provider_passes_the_pinned_owner_budget_to_real_adapter(
    tmp_path,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        prompt = json.loads(request.content)["input"][0]["content"][0]["text"]
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            document = _empty_document()
            document["overview"] = [
                {"text": "chunk detail " + "x" * 6_800, "source_ids": [source["segments"][0]["id"]]}
            ]
        else:
            document = _empty_document()
        return httpx.Response(
            200,
            content=_document_stream(document),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    provider = _connected_chatgpt_provider(tmp_path, adapter)
    with pytest.raises(ChatGPTError) as failure:
        provider.generate_notes_document(
            snapshot=_many_segment_snapshot(repeats=10),
            connection_version=7,
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )

    assert failure.value.code == "notes_synthesis_limit_exceeded"
    assert requests
    assert any(
        "SYNTHESIS_INPUT_JSON:" in json.loads(request.content)["input"][0]["content"][0]["text"]
        for request in requests
    )
    assert all(
        json.loads(request.content)["max_output_tokens"]
        == nd.CHATGPT_GPT_5_6_SOL_OUTPUT_RESERVE_TOKENS
        for request in requests
    )
    assert all(len(request.content) <= 1 * 1024 * 1024 for request in requests)


def test_chatgpt_planner_body_matches_real_adapter_and_keeps_builder_fields():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_notes_stream({}),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    prompt = "Nội dung café"
    scope = ("00001",)
    schema = nd.build_notes_schema(scope)
    adapter.generate_notes(
        access_token="test-token",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        prompt=prompt,
        allowed_source_ids=scope,
        budget=budget,
    )

    expected = {
        "model": "gpt-5.6-sol",
        "max_output_tokens": 65_536,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "meeting_notes_v1",
                "strict": True,
                "schema": schema,
            }
        },
        "store": False,
        "stream": True,
        "reasoning": {"effort": "xhigh"},
    }
    expected_wire = json.dumps(
        expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert requests[0].content == expected_wire
    assert len(requests[0].content) == nd.notes_request_body_bytes(prompt, scope, budget=budget)

    adapter.generate_notes(
        access_token="test-token",
        model="gpt-5.6-sol",
        reasoning_effort="default",
        prompt=prompt,
        allowed_source_ids=scope,
        budget=budget,
    )
    default_body = json.loads(requests[1].content)
    assert default_body == {key: value for key, value in expected.items() if key != "reasoning"}
    assert "reasoning" not in default_body


def test_conservative_prompt_guard_accepts_exact_limit_and_rejects_next_byte():
    exact = "x" * nd.MAX_PROMPT_BYTES
    assert nd._check_prompt(exact, allowed_source_ids=()) == exact
    with pytest.raises(nd.NotesDocumentError):
        nd._check_prompt(exact + "x", allowed_source_ids=())


def test_chatgpt_body_guard_accepts_exact_limit_and_rejects_next_byte():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_notes_stream({}),
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    owner = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    prompt = "x" * 2_000
    schema = nd.build_notes_schema(())
    body = nd.build_chatgpt_notes_request_body(
        model="gpt-5.6-sol",
        prompt=prompt,
        schema=schema,
        reasoning_effort="medium",
        budget=owner,
    )
    limit = len(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    boundary_budget = replace(owner, request_guard_bytes=limit)
    adapter.generate_notes(
        access_token="test-token",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        prompt=prompt,
        allowed_source_ids=(),
        budget=boundary_budget,
    )
    assert len(requests[0].content) == limit

    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(
            access_token="test-token",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            prompt=prompt + "x",
            allowed_source_ids=(),
            budget=boundary_budget,
        )
    assert failure.value.code == "notes_request_too_large"
    assert len(requests) == 1


def test_antigravity_frame_guard_accepts_exact_limit_and_rejects_next_byte(tmp_path, monkeypatch):
    adapter = _ProviderNotesAdapter()
    provider = _connected_antigravity_provider(tmp_path, adapter)
    budget = nd.notes_budget("antigravity", "gemini-3.8-flash")
    prompt = "frame payload"
    limit = nd.notes_request_body_bytes(prompt, (), budget=budget)
    boundary_budget = replace(budget, request_guard_bytes=limit)
    pipeline_calls = 0

    def fake_pipeline(**kwargs: object) -> str:
        nonlocal pipeline_calls
        pipeline_calls += 1
        sent = prompt if pipeline_calls == 1 else prompt + "x"
        return kwargs["transport"](
            sent,
            allowed_source_ids=(),
            max_output_bytes=nd.MAX_OUTPUT_BYTES,
            stage="single",
            attempt="primary",
            provider_call_index=1,
            prompt_bytes=len(sent),
            budget=boundary_budget,
        )

    monkeypatch.setattr(antigravity_module, "generate_notes_pipeline", fake_pipeline)
    result = provider.generate_notes_document(
        snapshot=_snapshot(0),
        connection_version=0,
        model="gemini-3.8-flash",
        reasoning_effort="high",
    )
    assert result == json.dumps(_empty_document())
    assert adapter.generate_calls == 1

    with pytest.raises(AntigravityError) as failure:
        provider.generate_notes_document(
            snapshot=_snapshot(0),
            connection_version=0,
            model="gemini-3.8-flash",
            reasoning_effort="high",
        )
    assert failure.value.code == "provider_request_too_large"
    assert adapter.generate_calls == 1


def test_acceptance_non_owner_chatgpt_pin_plans_a_large_source_as_chunks(tmp_path, monkeypatch):
    class NonOwnerProvider(StubNotesProvider):
        def notes_task_pin(self):
            return {
                "provider": "chatgpt",
                "connection_version": 7,
                "model": "gpt-5.6-other",
                "reasoning_effort": "medium",
            }

    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    large = _many_segment_snapshot()
    real_source_snapshot = notes_module._source_snapshot
    seen: list[tuple[object, object]] = []
    plans: list[nd.NotesPlan] = []
    real_plan_source = nd.plan_source

    def large_source_snapshot(connection, *, meeting, generation, snapshot_id):
        snapshot = real_source_snapshot(
            connection, meeting=meeting, generation=generation, snapshot_id=snapshot_id
        )
        snapshot["segments"] = large["segments"]
        return snapshot

    def record_plan(snapshot, **kwargs):
        seen.append((kwargs.get("provider"), kwargs.get("model")))
        plan = real_plan_source(snapshot, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(notes_module, "_source_snapshot", large_source_snapshot)
    monkeypatch.setattr(notes_module, "plan_source", record_plan)
    accept_notes = notes_module.accept_notes_task(
        data_dir,
        provider=NonOwnerProvider(),
        meeting_id=meeting_id,
        request_id="non-owner-chatgpt-plan",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )

    assert accept_notes["status"] == "accepted"
    assert seen == [("chatgpt", "gpt-5.6-other")]
    assert plans[0].chunked is True
    assert len(plans[0].chunks) > 1


def test_owner_failure_diagnostics_keep_prompt_bytes_above_conservative_limit(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        events = (
            {
                "type": "response.output_text.delta",
                "delta": "not-json",
            },
            {"type": "response.completed", "response": {"output": [], "usage": {}}},
        )
        content = b"".join(
            b"data: " + json.dumps(event).encode("utf-8") + b"\n\n" for event in events
        )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "text/event-stream"},
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    provider = _connected_chatgpt_provider(tmp_path, adapter)
    with pytest.raises(ChatGPTError) as failure:
        provider.generate_notes_document(
            snapshot=_many_segment_snapshot(),
            connection_version=7,
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )

    assert failure.value.code == "notes_invalid_output"
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["prompt_bytes"] > 48_000


def test_notes_runner_info_logging_reaches_stderr():
    project_src = str(Path(__file__).resolve().parents[1] / "src")
    script = """
import logging
import sys

from asr_studio import notes_runner


def fake_run(*args, **kwargs):
    logging.getLogger("asr_studio.notes_pipeline").info("notes-provider-info-sentinel")
    return 0


notes_runner.run_notes_task = fake_run
sys.argv = [
    "notes_runner",
    "--data-dir",
    "/tmp",
    "--task-id",
    "task",
    "--owner-id",
    "owner",
]
raise SystemExit(notes_runner.main())
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (project_src, environment.get("PYTHONPATH")) if item
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "INFO asr_studio.notes_pipeline notes-provider-info-sentinel" in result.stderr
