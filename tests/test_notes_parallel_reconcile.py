"""Registered Gemini fact inventory: the reconcile runs as two concurrent owned ledger views."""

from __future__ import annotations

import json
import threading
import time

import pytest
from notes_helpers import SEMANTIC_KEEP_PATCH, reconcile_keep_patch, reconcile_payload

from asr_studio import notes_document as nd
from asr_studio.notes_pipeline import generate_notes_document

GEMINI_MODEL = "gemini-3.8-flash"


def _empty() -> dict:
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _segment(index: int, start: float, text: str) -> dict:
    return {
        "id": f"{index:032x}",
        "start_seconds": start,
        "end_seconds": start + 1,
        "text": text,
        "speaker_name": "Morgan",
    }


def _snapshot() -> dict:
    # Two quarter-hour fact parts over the hour, so the ledger holds two map entries.
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            _segment(1, 0.0, "Launch plan is approved."),
            _segment(2, 3_601.0, "Vendor contract renewal is still open."),
        ],
    }


def _source(prompt: str) -> dict:
    return json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]


def _map_reply(prompt: str) -> str:
    owned = [s for s in _source(prompt)["segments"] if not s.get("overlap")]
    document = _empty()
    document["key_topics"] = [{"text": owned[0]["text"], "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


def _run(transport, *, coverage: list, provider: str = "antigravity", model: str = GEMINI_MODEL):
    return generate_notes_document(
        snapshot=_snapshot(),
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )


def _owned(prompt: str) -> list[str]:
    payload = reconcile_payload(prompt)
    return [item["ref"] for item in payload["final_items"] + payload["map_items"]]


def test_two_owned_views_run_concurrently_over_the_full_source_and_merge_once():
    calls: list[dict] = []
    barrier = threading.Barrier(2, timeout=10)  # breaks unless both views are in flight at once

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        assert kwargs["stage"] == "reconcile" and kwargs["attempt"] == "primary"
        barrier.wait()
        return reconcile_keep_patch(prompt)

    coverage: list[dict] = []
    result = _run(transport, coverage=coverage)

    reconciles = [call for call in calls if call["stage"] == "reconcile"]
    assert len(reconciles) == 2
    owned = [_owned(call["prompt"]) for call in reconciles]
    assert sorted(ref for refs in owned for ref in refs) == ["m0", "m1"]
    assert all(len(refs) == 1 for refs in owned)  # disjoint, balanced
    snapshot_ids = [segment["id"] for segment in _snapshot()["segments"]]
    additions_caps = {}
    for call, refs in zip(reconciles, owned, strict=True):
        payload = reconcile_payload(call["prompt"])
        assert len(payload["source"]["segments"]) == len(snapshot_ids)  # full source on each
        # The other view's entry is context only, with no ref this view could name.
        assert len(payload["read_only_items"]) == 1
        assert "ref" not in payload["read_only_items"][0]
        assert call["response_schema"]["required"] == [
            "ledger_version",
            "dispositions",
            "additions",
        ]
        additions_caps[refs[0]] = call["response_schema"]["properties"]["additions"]["maxItems"]
    assert additions_caps["m0"] > 0 and additions_caps["m1"] == 0  # only the first view adds

    audit = coverage[0]["source_audit"]
    indices = sorted(c["provider_call_index"] for c in reconciles)
    assert audit["mode"] == "reconcile" and audit["status"] == "applied"
    assert audit["part_count"] == 2
    assert sorted(audit["provider_call_indices"]) == indices
    assert audit["provider_call_index"] == max(indices)
    assert audit["counts"]["map"]["keep"] == 2
    assert coverage[0]["provider_calls"] == len(calls) <= nd.MAX_PROVIDER_CALLS
    assert [call["stage"] for call in calls].count("semantic_audit") == 2
    assert {item["text"] for item in result["key_topics"]} == {
        "Launch plan is approved.",
        "Vendor contract renewal is still open.",
    }


def test_both_views_are_charged_to_the_reduction_budget(monkeypatch):
    calls: list[str] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs["stage"])
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        return reconcile_keep_patch(prompt)

    monkeypatch.setattr(nd, "MAX_REDUCTION_CALLS", 1)
    with pytest.raises(RuntimeError, match="reduction limit"):
        _run(transport, coverage=(coverage := []))
    assert "reconcile" not in calls and coverage == []

    calls.clear()
    monkeypatch.setattr(nd, "MAX_REDUCTION_CALLS", 2)
    _run(transport, coverage=(coverage := []))
    assert calls.count("reconcile") == 2 and coverage[0]["source_audit"]["part_count"] == 2


def test_both_views_are_charged_to_the_global_call_budget(monkeypatch):
    calls: list[str] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs["stage"])
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        return reconcile_keep_patch(prompt)

    # Two maps and two views fill the budget, so no semantic-audit part can be sent.
    monkeypatch.setattr(nd, "MAX_PROVIDER_CALLS", 4)
    with pytest.raises(RuntimeError, match="request limit"):
        _run(transport, coverage=(coverage := []))
    assert calls == ["chunk", "chunk", "reconcile", "reconcile"] and coverage == []


def test_a_failed_view_aborts_its_sibling_and_publishes_nothing():
    calls: list[dict] = []
    barrier = threading.Barrier(2, timeout=10)
    failed = threading.Event()

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        assert kwargs["stage"] == "reconcile", "nothing runs after the failed reconcile"
        assert kwargs["attempt"] == "primary", "the sibling sends no repair after the abort"
        barrier.wait()
        if _owned(prompt) == ["m0"]:
            failed.set()
            raise ConnectionError("provider failed")
        assert failed.wait(10)
        time.sleep(0.2)  # let the failed view mark the abort first
        return "not json"  # would earn the one repair if the task were still running

    coverage: list[dict] = []
    with pytest.raises((ConnectionError, RuntimeError)):
        _run(transport, coverage=coverage)
    assert [c["stage"] for c in calls] == ["chunk", "chunk", "reconcile", "reconcile"]
    assert coverage == []


def test_one_global_repair_spans_both_reconcile_views_and_both_semantic_parts():
    calls: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            if kwargs["attempt"] == "primary" and _owned(prompt) == ["m1"]:
                return "not json"  # consumes the task's one repair
            return reconcile_keep_patch(prompt)
        assert kwargs["stage"] == "semantic_audit"
        return "not json"  # a later invalid part finds the repair already used

    coverage: list[dict] = []
    with pytest.raises(AssertionError) as failure:
        _run(transport, coverage=coverage)
    attempts = [(c["stage"], c["attempt"]) for c in calls]
    assert attempts.count(("reconcile", "repair")) == 1
    assert [a for a in attempts if a[1] == "repair"] == [("reconcile", "repair")]
    assert attempts.count(("reconcile", "primary")) == 2
    assert 1 <= attempts.count(("semantic_audit", "primary")) <= 2
    assert failure.value.args[0]["repair_state"] == "already_consumed"
    assert failure.value.args[0]["stage"] == "semantic_audit"
    assert coverage == []


def test_chatgpt_long_reconcile_stays_one_unsplit_request():
    calls: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        return reconcile_keep_patch(prompt)

    coverage: list[dict] = []
    _run(transport, coverage=coverage, provider="chatgpt", model="gpt-5.6-sol")
    reconciles = [call for call in calls if call["stage"] == "reconcile"]
    assert len(reconciles) == 1
    assert "read_only_items" not in reconcile_payload(reconciles[0]["prompt"])
    assert _owned(reconciles[0]["prompt"]) == ["m0", "m1"]
    assert set(coverage[0]["source_audit"]) == {"status", "mode", "provider_call_index", "counts"}
