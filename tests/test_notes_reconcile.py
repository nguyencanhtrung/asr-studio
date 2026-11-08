from __future__ import annotations

import json

import httpx
import pytest
from notes_helpers import reconcile_disposition, reconcile_keep_patch, reconcile_payload

from asr_studio import notes_document as nd
from asr_studio.chatgpt import ChatGPTError, ChatGPTHTTPAdapter
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.notes_reconcile import MAX_RECONCILE_ADDITIONS, ReconcileLedger

SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": "s1",
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "text": "We agreed to ship the pilot in 3 regions.",
            "speaker_name": "Avery",
        },
        {
            "id": "s2",
            "start_seconds": 5.0,
            "end_seconds": 9.0,
            "text": "I will send the audit checklist by tomorrow.",
            "speaker_name": "Rowan",
        },
        {
            "id": "s3",
            "start_seconds": 9.0,
            "end_seconds": 14.0,
            "text": "Budget review moves to the finance board on Friday.",
            "speaker_name": "Avery",
        },
    ],
}


def _empty():
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _final():
    document = _empty()
    document["decisions"] = [{"text": "Pilot ships in 3 regions.", "source_ids": ["s1"]}]
    document["confirmation_items"] = [{"text": "Pilot may need extra staff.", "source_ids": ["s1"]}]
    document["action_items"] = [
        {
            "text": "Send audit checklist.",
            "source_ids": ["s2"],
            "owner": None,
            "deadline_text": None,
            "deadline_date": None,
        }
    ]
    return document


def _map():
    document = _empty()
    document["key_topics"] = [{"text": "Budget review at finance board.", "source_ids": ["s3"]}]
    document["decisions"] = [{"text": "Pilot ships in 3 regions.", "source_ids": ["s1"]}]
    return document


def _ledger():
    return ReconcileLedger.build(SOURCE, _final(), [_map()])


def _keep_all(ledger, **overrides):
    dispositions = []
    for ref in ledger.refs:
        dispositions.append(overrides.get(ref, reconcile_disposition(ref)))
    return {"ledger_version": 1, "dispositions": dispositions, "additions": []}


def _apply(patch):
    return _ledger().apply(patch)


def test_ledger_has_every_final_and_map_ref():
    ledger = _ledger()
    assert ledger.refs == ("f0", "f1", "f2", "m0", "m1")
    payload = ledger.prompt_payload()
    assert [item["ref"] for item in payload["map_items"]] == ["m0", "m1"]
    assert payload["source"]["segments"][2]["text"].startswith("Budget review")


def test_keep_preserves_original_items_and_map_keep_adds_the_map_item():
    ledger = _ledger()
    patch = _keep_all(
        ledger,
        m1=reconcile_disposition("m1", "remove", reason="duplicate", target="f0"),
    )
    result = ledger.apply(patch)
    assert result.document["decisions"] == _final()["decisions"]
    assert result.document["action_items"] == _final()["action_items"]
    assert result.document["key_topics"] == _map()["key_topics"]
    assert result.counts["map"] == {"keep": 1, "update": 0, "merge": 0, "remove": 1, "confirm": 0}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda patch: patch["dispositions"].pop(0), id="omitted-final-ref"),
        pytest.param(lambda patch: patch["dispositions"].pop(), id="unaccounted-map-ref"),
        pytest.param(
            lambda patch: patch["dispositions"].append(reconcile_disposition("f0")),
            id="duplicate-ref",
        ),
        pytest.param(
            lambda patch: patch["dispositions"].__setitem__(0, reconcile_disposition("f9")),
            id="unknown-ref",
        ),
        pytest.param(
            lambda patch: patch["dispositions"].__setitem__(
                0, reconcile_disposition("f0", text="Changed under keep.")
            ),
            id="keep-with-text",
        ),
        pytest.param(lambda patch: patch.update(extra=1), id="extra-field"),
        pytest.param(lambda patch: patch.update(ledger_version=2), id="version"),
    ],
)
def test_patch_accounting_rejects_omission_duplicates_and_unknown_refs(mutate):
    patch = _keep_all(_ledger())
    mutate(patch)
    with pytest.raises(nd.NotesDocumentError):
        _apply(patch)


@pytest.mark.parametrize(
    ("m1", "f0"),
    [
        pytest.param(reconcile_disposition("m1", "merge", target="m1"), None, id="self"),
        pytest.param(reconcile_disposition("m1", "merge"), None, id="no-target"),
        pytest.param(reconcile_disposition("m1", "merge", target="zz"), None, id="unknown-target"),
        pytest.param(
            reconcile_disposition("m1", "merge", target="f0"),
            reconcile_disposition("f0", "remove", reason="unsupported"),
            id="removed-target",
        ),
        pytest.param(
            reconcile_disposition("m1", "merge", target="f0"),
            reconcile_disposition("f0", "merge", target="m1"),
            id="cycle",
        ),
        pytest.param(reconcile_disposition("m1", "remove"), None, id="remove-without-reason"),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="duplicate"), None, id="dup-no-target"
        ),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="unsupported", target="f0"),
            None,
            id="unsupported-with-target",
        ),
        pytest.param(
            reconcile_disposition("m1", "keep", reason="nonmaterial"), None, id="reason-on-keep"
        ),
    ],
)
def test_merge_and_removal_targets_are_validated(m1, f0):
    ledger = _ledger()
    overrides = {"m1": m1}
    if f0 is not None:
        overrides["f0"] = f0
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, **overrides))


def test_merge_unions_citations_into_a_kept_target():
    ledger = _ledger()
    result = ledger.apply(_keep_all(ledger, m0=reconcile_disposition("m0", "merge", target="f0")))
    assert result.document["decisions"][0]["text"] == "Pilot ships in 3 regions."
    assert result.document["decisions"][0]["source_ids"] == ["s1", "s3"]
    assert result.document["key_topics"] == []


def _update(ref, text, source_ids, quotes, **fields):
    return reconcile_disposition(
        ref,
        "update",
        text=text,
        source_ids=source_ids,
        evidence=[{"source_id": sid, "quote": quote} for sid, quote in quotes],
        **fields,
    )


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        pytest.param(
            _update("f0", "Pilot ships in 4 regions.", ["s1"], [("s1", "ship the pilot")]),
            "items",
            id="new-number-absent",
        ),
        pytest.param(
            _update("f0", "Pilot ships in 3 regions.", ["s1"], [("s1", "ship the pilot in 9")]),
            "source_ids",
            id="quote-not-substring",
        ),
        pytest.param(
            _update("f0", "Pilot ships in 3 regions.", ["s1"], [("s2", "audit checklist")]),
            "source_ids",
            id="quote-from-uncited-segment",
        ),
        pytest.param(
            _update("f0", "Pilot ships in 3 regions.", ["s7"], [("s7", "anything")]),
            "source_ids",
            id="unknown-source-id",
        ),
        pytest.param(
            _update("f0", "Pilot ships in 3 regions.", ["s1"], []),
            "source_ids",
            id="no-evidence",
        ),
        pytest.param(
            _update("f2", "Pilot needs extra staff.", ["s1"], [("s1", "ship the pilot")]),
            "text",
            id="drops-uncertainty-marker",
        ),
        pytest.param(
            _update(
                "f0", "Pilot ships in 3 regions, says Quinn.", ["s1"], [("s1", "ship the pilot")]
            ),
            "items",
            id="new-name-absent",
        ),
    ],
)
def test_updates_need_grounded_evidence(disposition, reason):
    ledger = _ledger()
    with pytest.raises(nd.NotesDocumentError) as failure:
        ledger.apply(_keep_all(ledger, **{disposition["ref"]: disposition}))
    assert failure.value.reason_code == reason


def test_grounded_update_replaces_only_that_item():
    ledger = _ledger()
    update = _update(
        "f0",
        "Pilot ships in 3 regions; budget review on Friday.",
        ["s1", "s3"],
        [("s1", "ship the pilot in 3 regions"), ("s3", "on Friday")],
    )
    result = ledger.apply(_keep_all(ledger, f0=update))
    assert result.document["decisions"][0]["source_ids"] == ["s1", "s3"]
    assert result.document["action_items"] == _final()["action_items"]


@pytest.mark.parametrize(
    ("fields", "ok"),
    [
        pytest.param({"owner": "Rowan"}, True, id="owner-is-cited-speaker"),
        pytest.param({"owner": "Avery"}, False, id="owner-not-cited"),
        pytest.param({"deadline_text": "tomorrow"}, True, id="deadline-in-source"),
        pytest.param({"deadline_text": "next week"}, False, id="deadline-absent"),
    ],
)
def test_action_metadata_changes_require_source_support(fields, ok):
    ledger = _ledger()
    update = _update(
        "f1", "Send audit checklist.", ["s2"], [("s2", "send the audit checklist")], **fields
    )
    patch = _keep_all(ledger, f1=update)
    if ok:
        item = ledger.apply(patch).document["action_items"][0]
        assert {key: item[key] for key in fields} == fields
        if "deadline_text" in fields:
            assert item["deadline_date"] == "2026-01-06"
        return
    with pytest.raises(nd.NotesDocumentError) as failure:
        ledger.apply(patch)
    assert failure.value.reason_code == "action_fields"


def test_metadata_on_keep_or_on_a_non_action_update_is_rejected():
    ledger = _ledger()
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, f1=reconcile_disposition("f1", owner="Rowan")))
    update = _update(
        "f0", "Pilot ships in 3 regions.", ["s1"], [("s1", "ship the pilot")], owner="Avery"
    )
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, f0=update))


def _addition(section, text, source_ids, quotes, **fields):
    return {
        "section": section,
        "text": text,
        "source_ids": source_ids,
        "evidence": [{"source_id": sid, "quote": quote} for sid, quote in quotes],
        "owner": None,
        "deadline_text": None,
        **fields,
    }


def test_missing_fact_from_full_source_can_be_added_without_a_map_ref():
    source = json.loads(json.dumps(SOURCE))
    source["segments"].append(
        {
            "id": "s4",
            "start_seconds": 14.0,
            "end_seconds": 18.0,
            "text": "Vendor contract renewal is still open.",
            "speaker_name": "Rowan",
        }
    )
    ledger = ReconcileLedger.build(source, _final(), [])  # no map item holds this fact
    patch = _keep_all(ledger)
    patch["additions"] = [
        _addition(
            "open_questions",
            "Vendor contract renewal still open.",
            ["s4"],
            [("s4", "contract renewal is still open")],
        )
    ]
    result = ledger.apply(patch)
    assert result.document["open_questions"] == [
        {"text": "Vendor contract renewal still open.", "source_ids": ["s4"]}
    ]
    assert result.counts["additions"] == 1


@pytest.mark.parametrize(
    "addition",
    [
        pytest.param(
            _addition("key_topics", "Finance board meets Friday.", ["s3"], []), id="no-evidence"
        ),
        pytest.param(
            _addition("key_topics", "Board meets 7 times.", ["s3"], [("s3", "finance board")]),
            id="number-absent",
        ),
        pytest.param(
            _addition(
                "key_topics", "Finance board meets.", ["s3"], [("s3", "board")], owner="Avery"
            ),
            id="metadata-outside-actions",
        ),
        pytest.param(
            _addition(
                "action_items", "Review budget.", ["s3"], [("s3", "Budget review")], owner="Quinn"
            ),
            id="unsupported-owner",
        ),
    ],
)
def test_bad_additions_are_rejected(addition):
    ledger = _ledger()
    patch = _keep_all(ledger)
    patch["additions"] = [addition]
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(patch)


def test_additions_are_bounded():
    ledger = _ledger()
    patch = _keep_all(ledger)
    patch["additions"] = [
        _addition("key_topics", f"Finance topic {chr(97 + index)}.", ["s3"], [("s3", "finance")])
        for index in range(MAX_RECONCILE_ADDITIONS + 1)
    ]
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(patch)
    assert ledger.schema()["properties"]["additions"]["maxItems"] == MAX_RECONCILE_ADDITIONS


def test_confirm_demotes_an_item_unchanged():
    ledger = _ledger()
    result = ledger.apply(
        _keep_all(
            ledger,
            f0=reconcile_disposition("f0", "confirm"),
            m1=reconcile_disposition("m1", "remove", reason="superseded"),
        )
    )
    assert result.document["decisions"] == []
    assert result.document["confirmation_items"][0] == {
        "text": "Pilot ships in 3 regions.",
        "source_ids": ["s1"],
    }


def _confirm_ledger(text):
    final = _empty()
    final["decisions"] = [{"text": text, "source_ids": ["s1"]}]
    return ReconcileLedger.build(SOURCE, final, [])


@pytest.mark.parametrize(
    "original",
    ["Pilot not approved in 3 regions.", "Pilot ships in 3 regions if staffing allows."],
)
def test_confirm_with_new_prose_keeps_negation_and_conditions(original):
    ledger = _confirm_ledger(original)
    flipped = reconcile_disposition(
        "f0",
        "confirm",
        text="Pilot approved in 3 regions.",
        source_ids=["s1"],
        evidence=[{"source_id": "s1", "quote": "ship the pilot"}],
    )
    with pytest.raises(nd.NotesDocumentError) as failure:
        ledger.apply({"ledger_version": 1, "dispositions": [flipped], "additions": []})
    assert failure.value.reason_code == "text"


def test_confirm_cannot_park_an_unsupported_name_unchanged():
    ledger = _confirm_ledger("Pilot approved by Quinn in 3 regions.")
    unchanged = reconcile_disposition("f0", "confirm")
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply({"ledger_version": 1, "dispositions": [unchanged], "additions": []})
    anonymised = reconcile_disposition(
        "f0",
        "confirm",
        text="Pilot approver in 3 regions unclear.",
        source_ids=["s1"],
        evidence=[{"source_id": "s1", "quote": "ship the pilot in 3 regions"}],
    )
    document = ledger.apply(
        {"ledger_version": 1, "dispositions": [anonymised], "additions": []}
    ).document
    assert document["confirmation_items"][0]["text"].startswith("Pilot approver")
    assert "reconcile" not in json.dumps(document)


def test_schema_counts_every_ref_without_source_id_enums():
    ledger = _ledger()
    schema = ledger.schema()
    dispositions = schema["properties"]["dispositions"]
    assert dispositions["minItems"] == dispositions["maxItems"] == 5
    assert "s1" not in json.dumps(schema)


# ----- adapter and pipeline -------------------------------------------------------------------


def _stream(text: str) -> bytes:
    return (
        "data: "
        + json.dumps({"type": "response.output_text.delta", "delta": text})
        + "\n\ndata: "
        + json.dumps({"type": "response.completed", "response": {"usage": {}}})
        + "\n\n"
    ).encode()


def test_custom_schema_is_sent_and_counted_by_the_chatgpt_adapter_guard():
    budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    schema = _ledger().schema()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=_stream("{}"), headers={"content-type": "text/event-stream"}
        )

    adapter = ChatGPTHTTPAdapter(transport=httpx.MockTransport(handler))
    call = {
        "access_token": "test-token",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "prompt": "p",
        "allowed_source_ids": ["s1"],
        "max_output_bytes": 1_000,
        "budget": budget,
    }
    adapter.generate_notes(**call, response_schema=schema)
    assert bodies[-1]["text"]["format"]["schema"] == schema
    adapter.generate_notes(**call)
    assert bodies[-1]["text"]["format"]["schema"] == nd.build_notes_schema(["s1"])

    huge = {**schema, "description": "x" * budget.request_guard_bytes}
    with pytest.raises(ChatGPTError) as failure:
        adapter.generate_notes(**call, response_schema=huge)
    # Either size guard may trip first; both measure the schema actually sent.
    assert failure.value.code in {"notes_request_too_large", "notes_context_budget_exceeded"}
    assert len(bodies) == 2
    measured = nd.notes_request_body_bytes("p", ["s1"], budget=budget, response_schema=schema)
    default = nd.notes_request_body_bytes("p", ["s1"], budget=budget)

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    assert measured - size(schema) == default - size(nd.build_notes_schema(["s1"]))
    assert nd.notes_request_body_bytes("p", ["s1"], budget=budget) == nd.notes_request_body_bytes(
        "p", ["s1"], budget=budget, response_schema=None
    )


def test_pipeline_budget_check_counts_the_actual_reconcile_schema():
    ledger = _ledger()
    budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    prompt = ledger.build_prompt(budget=budget)
    ids = [segment["id"] for segment in SOURCE["segments"]]
    padded = {**ledger.schema(), "description": "x" * (budget.request_guard_bytes // 2)}
    nd._check_prompt(prompt, allowed_source_ids=ids, budget=budget)  # schema 1 fits
    with pytest.raises(nd.NotesDocumentError):
        nd._check_prompt(prompt, allowed_source_ids=ids, budget=budget, response_schema=padded)


def _long_snapshot():
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            {
                "id": f"{index:032x}",
                "start_seconds": start,
                "end_seconds": start + 1,
                "text": text,
                "speaker_name": "Morgan",
            }
            for index, start, text in (
                (1, 0.0, "Launch plan is approved."),
                (2, 3_601.0, "Vendor contract renewal is still open."),
            )
        ],
    }


def _doc(section, text, source_id):
    document = _empty()
    document[section] = [{"text": text, "source_ids": [source_id]}]
    return json.dumps(document)


def _long_transport(calls, reconcile, *, chunk=None):
    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        stage = kwargs["stage"]
        if stage == "chunk":
            if chunk is not None:
                return chunk(prompt, kwargs)
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            owned = [segment for segment in source["segments"] if not segment.get("overlap")]
            return _doc("key_topics", owned[0]["text"], owned[0]["id"])
        if stage == "final":
            return _doc("overview", "Launch plan approved.", kwargs["allowed_source_ids"][0])
        assert stage == "reconcile"
        return reconcile(prompt, kwargs)

    return transport


def _run(transport, provider="chatgpt", model="gpt-5.6-sol", coverage=None):
    return generate_notes_document(
        snapshot=_long_snapshot(),
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=(coverage.append if coverage is not None else None),
    )


def test_long_chatgpt_runs_one_reconcile_with_projected_ids_and_restores_once():
    calls: list[dict] = []
    coverage: list[dict] = []

    def reconcile(prompt, kwargs):
        payload = reconcile_payload(prompt)
        source = payload["source"]["segments"]
        assert all(len(segment["id"]) < 32 for segment in source)  # projected aliases
        patch = json.loads(reconcile_keep_patch(prompt))
        open_id = source[1]["id"]
        patch["additions"] = [
            _addition(
                "open_questions",
                "Vendor contract renewal still open.",
                [open_id],
                [(open_id, "renewal is still open")],
            )
        ]
        return json.dumps(patch)

    result = _run(_long_transport(calls, reconcile), coverage=coverage)

    stages = [call["stage"] for call in calls]
    assert stages.count("reconcile") == 1 and stages[-1] == "reconcile"
    assert all("response_schema" not in call for call in calls[:-1])
    assert calls[-1]["response_schema"]["required"] == [
        "ledger_version",
        "dispositions",
        "additions",
    ]
    assert result["open_questions"] == [
        {"text": "Vendor contract renewal still open.", "source_ids": [f"{2:032x}"]}
    ]
    assert result["overview"][0]["source_ids"][0] == f"{1:032x}"
    audit = coverage[0]["source_audit"]
    assert (audit["status"], audit["mode"], audit["provider_call_index"]) == (
        "applied",
        "reconcile",
        len(calls),
    )
    assert audit["counts"]["additions"] == 1


def test_invalid_reconcile_uses_the_one_shared_repair_then_fails():
    calls: list[dict] = []
    bad = json.dumps({"ledger_version": 1, "dispositions": [], "additions": []})
    with pytest.raises(AssertionError) as failure:
        _run(_long_transport(calls, lambda _prompt, _kwargs: bad))
    assert [(c["stage"], c["attempt"]) for c in calls[-2:]] == [
        ("reconcile", "primary"),
        ("reconcile", "repair"),
    ]
    assert failure.value.args[0]["stage"] == "reconcile"
    assert failure.value.args[0]["repair_state"] == "already_consumed"


def test_reconcile_gets_no_repair_after_a_map_consumed_it():
    calls: list[dict] = []
    chunk_attempts: list[str] = []

    def chunk(prompt, kwargs):
        chunk_attempts.append(kwargs["attempt"])
        source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
        owned = [segment for segment in source["segments"] if not segment.get("overlap")]
        if len(chunk_attempts) == 1:
            return "not json"
        return _doc("key_topics", owned[0]["text"], owned[0]["id"])

    bad = json.dumps({"ledger_version": 1, "dispositions": [], "additions": []})
    with pytest.raises(AssertionError) as failure:
        _run(_long_transport(calls, lambda _p, _k: bad, chunk=chunk))
    assert "repair" in chunk_attempts
    assert [c["stage"] for c in calls].count("reconcile") == 1
    assert failure.value.args[0]["repair_state"] == "already_consumed"


def test_oversized_reconcile_output_fails_without_a_schema1_recovery():
    calls: list[dict] = []

    def reconcile(_prompt, _kwargs):
        raise ChatGPTError(502, "provider_output_too_large", "too large")

    with pytest.raises(ChatGPTError):
        _run(_long_transport(calls, reconcile))
    assert [c["stage"] for c in calls].count("reconcile") == 1


def test_reconcile_prompt_budget_failure_is_a_bounded_limit(monkeypatch):
    calls: list[dict] = []

    def no_room(*_args, **_kwargs):
        raise nd.NotesDocumentError("no room")

    monkeypatch.setattr(ReconcileLedger, "build_prompt", no_room)
    with pytest.raises(RuntimeError, match="reconciliation limit"):
        _run(_long_transport(calls, lambda _p, _k: pytest.fail("no reconcile call")))
    assert "reconcile" not in [c["stage"] for c in calls]


def test_long_antigravity_keeps_its_previous_policy():
    calls: list[dict] = []
    coverage: list[dict] = []
    _run(
        _long_transport(calls, lambda _p, _k: pytest.fail("no reconcile call")),
        provider="antigravity",
        model="gemini-3.8-flash",
        coverage=coverage,
    )
    assert "reconcile" not in [c["stage"] for c in calls]
    assert coverage[0]["source_audit"] == {"status": "skipped", "reason": "quality_not_qualified"}
