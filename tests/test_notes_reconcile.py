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
    return {"ledger_version": 2, "dispositions": dispositions, "additions": []}


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


EMPTY_PATCH = {"ledger_version": 2, "dispositions": [], "additions": []}


def test_empty_overlay_keeps_every_fact_and_input_is_immutable():
    ledger = _ledger()
    before = json.dumps(ledger.prompt_payload(), sort_keys=True)
    result = ledger.apply(dict(EMPTY_PATCH, dispositions=[], additions=[]))
    assert json.dumps(ledger.prompt_payload(), sort_keys=True) == before
    assert _map()["decisions"][0] in result.document["decisions"]
    assert result.document["key_topics"] == _map()["key_topics"]
    assert result.counts["default_kept"] == 5
    assert result.counts["final"]["keep"] == 3 and result.counts["map"]["keep"] == 2
    assert result.document == ledger.apply(_keep_all(ledger)).document


def test_sparse_update_preserves_unlisted_facts():
    ledger = _ledger()
    result = ledger.apply(
        dict(
            EMPTY_PATCH,
            dispositions=[reconcile_disposition("m1", "remove", reason="duplicate", target="f0")],
        )
    )
    assert result.document["decisions"] == _final()["decisions"]
    assert result.document["key_topics"] == _map()["key_topics"]
    assert result.counts["default_kept"] == 4 and result.counts["map"]["remove"] == 1


def test_old_explicit_complete_patch_still_works_with_version_2():
    ledger = _ledger()
    assert ledger.apply(_keep_all(ledger)).counts["default_kept"] == 0


def test_default_keep_does_not_bypass_grounding():
    ledger = _cue_ledger("key_topics", "Priya Okafor shared survey results.", ["n3"])
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        ledger.apply(EMPTY_PATCH)


@pytest.mark.parametrize(
    "mutate",
    [
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
        pytest.param(lambda patch: patch.update(ledger_version=1), id="version"),
    ],
)
def test_patch_accounting_rejects_duplicates_and_unknown_refs(mutate):
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


def _merge_ledger(text="Pilot ships in 3 regions.", source_ids=("s3",), section="decisions"):
    """f0..f2 from _final(); m0 restates a final fact (or not) with its own citation."""
    map_doc = _empty()
    map_doc[section] = [{"text": text, "source_ids": list(source_ids)}]
    return ReconcileLedger.build(SOURCE, _final(), [map_doc])


def test_merge_unions_citations_into_a_kept_target():
    ledger = _merge_ledger()
    assert ledger.merge_targets()["m0"] == ["f0"]
    result = ledger.apply(_keep_all(ledger, m0=reconcile_disposition("m0", "merge", target="f0")))
    assert result.document["decisions"][0]["text"] == "Pilot ships in 3 regions."
    assert result.document["decisions"][0]["source_ids"] == ["s1", "s3"]
    assert len(result.document["decisions"]) == 1


@pytest.mark.parametrize("action", ["merge", "duplicate"])
@pytest.mark.parametrize(
    ("text", "section"),
    [
        pytest.param("Budget review at finance board.", "decisions", id="distinct-fact"),
        pytest.param("Pilot ships in 3 regions.", "key_topics", id="other-section"),
        pytest.param("Pilot ships in 3 regions by May.", "decisions", id="target-lacks-detail"),
        pytest.param("Pilot may ship in 3 regions.", "decisions", id="modality-differs"),
    ],
)
def test_lossy_merge_or_duplicate_is_rejected(action, text, section):
    ledger = _merge_ledger(text, section=section)
    assert ledger.merge_targets()["m0"] == []
    if action == "merge":
        disposition = reconcile_disposition("m0", "merge", target="f0")
    else:
        disposition = reconcile_disposition("m0", "remove", reason="duplicate", target="f0")
    with pytest.raises(nd.NotesDocumentError, match="full merged fact"):
        ledger.apply(_keep_all(ledger, m0=disposition))
    # Keeping the distinct fact separately stays valid.
    assert ledger.apply(_keep_all(ledger)).document[section][-1]["text"] == text


def test_merge_into_a_target_updated_to_contain_the_whole_fact_is_accepted():
    ledger = _merge_ledger("Pilot ships in 3 regions; budget review at finance board.")
    update = _update(
        "f0",
        "Pilot ships in 3 regions; budget review at finance board.",
        ["s1", "s3"],
        [("s1", "ship the pilot in 3 regions"), ("s3", "Budget review moves")],
    )
    result = ledger.apply(
        _keep_all(ledger, f0=update, m0=reconcile_disposition("m0", "merge", target="f0"))
    )
    assert result.document["decisions"] == [
        {
            "text": "Pilot ships in 3 regions; budget review at finance board.",
            "source_ids": ["s1", "s3"],
        }
    ]


def test_merge_containment_ignores_only_case_whitespace_and_punctuation():
    ledger = _merge_ledger("pilot  SHIPS in 3 regions")
    assert ledger.merge_targets()["m0"] == ["f0"]
    assert _merge_ledger("Pilot ships in 4 regions.").merge_targets()["m0"] == []


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
            m1=reconcile_disposition("m1", "remove", reason="nonmaterial"),
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
        ledger.apply({"ledger_version": 2, "dispositions": [flipped], "additions": []})
    assert failure.value.reason_code == "text"


def test_confirm_cannot_park_an_unsupported_name_unchanged():
    ledger = _confirm_ledger("Pilot approved by Quinn in 3 regions.")
    unchanged = reconcile_disposition("f0", "confirm")
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply({"ledger_version": 2, "dispositions": [unchanged], "additions": []})
    anonymised = reconcile_disposition(
        "f0",
        "confirm",
        text="Pilot approver in 3 regions unclear.",
        source_ids=["s1"],
        evidence=[{"source_id": "s1", "quote": "ship the pilot in 3 regions"}],
    )
    document = ledger.apply(
        {"ledger_version": 2, "dispositions": [anonymised], "additions": []}
    ).document
    assert document["confirmation_items"][0]["text"].startswith("Pilot approver")
    assert "reconcile" not in json.dumps(document)


def test_schema_counts_every_ref_without_source_id_enums():
    ledger = _ledger()
    schema = ledger.schema()
    dispositions = schema["properties"]["dispositions"]
    assert dispositions["maxItems"] == 5
    assert "minItems" not in dispositions
    assert schema["properties"]["ledger_version"]["enum"] == [2]
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
    # Quarter-hour maps, then the structured reconcile is the final synthesis.
    assert stages == ["chunk", "chunk", "reconcile"]
    assert reconcile_payload(calls[-1]["prompt"])["final_items"] == []
    assert all("response_schema" not in call for call in calls[:-1])
    assert calls[-1]["response_schema"]["required"] == [
        "ledger_version",
        "dispositions",
        "additions",
    ]
    assert result["open_questions"] == [
        {"text": "Vendor contract renewal still open.", "source_ids": [f"{2:032x}"]}
    ]
    assert result["key_topics"] == [
        {"text": "Launch plan is approved.", "source_ids": [f"{1:032x}"]},
        {"text": "Vendor contract renewal is still open.", "source_ids": [f"{2:032x}"]},
    ]
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


# --- compat: redundant merge echo and optional superseded target ---


def _evidence(sid, quote):
    return [{"source_id": sid, "quote": quote}]


def test_merge_may_echo_exactly_its_original_source_ids():
    ledger = _merge_ledger()
    echo = reconcile_disposition("m0", "merge", target="f0", source_ids=["s3"])
    plain = ledger.apply(_keep_all(ledger, m0=reconcile_disposition("m0", "merge", target="f0")))
    echoed = ledger.apply(_keep_all(ledger, m0=echo))
    assert echoed.document == plain.document
    assert echoed.document["decisions"][0]["source_ids"] == ["s1", "s3"]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"source_ids": ["s1"]}, id="changed-existing-id"),
        pytest.param({"source_ids": ["s2"]}, id="new-citation"),
        pytest.param({"source_ids": ["s3", "s1"]}, id="superset"),
        pytest.param({"source_ids": ["s3", "s3"]}, id="duplicate-id"),
        pytest.param({"source_ids": ["zz"]}, id="unknown-id"),
        pytest.param(
            {"source_ids": ["s3"], "evidence": _evidence("s3", "Budget review")},
            id="echo-with-evidence",
        ),
        pytest.param({"source_ids": ["s3"], "text": "New prose."}, id="echo-with-text"),
        pytest.param({"source_ids": ["s3"], "owner": "Avery"}, id="echo-with-metadata"),
        pytest.param({"evidence": _evidence("s3", "Budget review")}, id="evidence-only"),
    ],
)
def test_merge_echo_rejects_anything_but_the_exact_original_ids(fields):
    ledger = _merge_ledger()
    merge = reconcile_disposition("m0", "merge", target="f0", **fields)
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, m0=merge))


def test_merge_echo_of_a_subset_of_several_original_ids_is_rejected():
    ledger = _merge_ledger(source_ids=("s1", "s3"))
    ref = ledger.refs[-1]

    def run(ids):
        merge = reconcile_disposition(ref, "merge", target="f0", source_ids=ids)
        return ledger.apply(_keep_all(ledger, **{ref: merge}))

    assert run(["s3", "s1"]).document["decisions"][0]["source_ids"] == ["s1", "s3"]
    with pytest.raises(nd.NotesDocumentError):
        run(["s1"])


def _superseded(ref="m1", target=None, sid="s3", quote="Budget review moves", **fields):
    """A superseded removal citing a later replacing segment (m1 cites s1)."""
    return reconcile_disposition(
        ref,
        "remove",
        reason="superseded",
        target=target,
        source_ids=[sid],
        evidence=_evidence(sid, quote),
        **fields,
    )


@pytest.mark.parametrize("target", ["f0", "f1"])
def test_superseded_removal_may_name_a_kept_target_without_citation_union(target):
    ledger = _ledger()
    before = ledger.apply(_keep_all(ledger, m1=_superseded()))
    after = ledger.apply(_keep_all(ledger, m1=_superseded(target=target)))
    assert after.document == before.document
    assert after.document["decisions"][0]["source_ids"] == ["s1"]


@pytest.mark.parametrize(
    "m1",
    [
        pytest.param(reconcile_disposition("m1", "remove", reason="superseded"), id="no-evidence"),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="superseded", source_ids=["s3"]),
            id="ids-without-quote",
        ),
        pytest.param(_superseded(sid="s1", quote="ship the pilot"), id="not-later"),
        pytest.param(_superseded(quote="Budget review is cancelled"), id="inexact-quote"),
    ],
)
def test_superseded_removal_needs_a_later_exact_replacing_quote(m1):
    ledger = _ledger()
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, m1=m1))


def test_superseded_target_may_be_an_updated_item():
    ledger = _ledger()
    update = _update(
        "f0", "Pilot ships in 3 regions now.", ["s1"], [("s1", "ship the pilot in 3 regions")]
    )
    result = ledger.apply(
        _keep_all(
            ledger,
            f0=update,
            m1=_superseded(target="f0"),
        )
    )
    assert result.document["decisions"][0]["text"] == "Pilot ships in 3 regions now."


def test_superseded_removal_without_a_target_stays_allowed():
    ledger = _ledger()
    ledger.apply(_keep_all(ledger, m1=_superseded()))


@pytest.mark.parametrize(
    ("m1", "f0"),
    [
        pytest.param(_superseded(target="m1"), None, id="self"),
        pytest.param(
            _superseded(target="zz"),
            None,
            id="unknown",
        ),
        pytest.param(
            _superseded(target="f0"),
            reconcile_disposition("f0", "remove", reason="unsupported"),
            id="removed-target",
        ),
        pytest.param(
            _superseded(target="f0"),
            reconcile_disposition("f0", "merge", target="f1"),
            id="merged-target",
        ),
        pytest.param(
            _superseded(target="f0"),
            _superseded("f0", target="m1"),
            id="chain-cycle",
        ),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="nonmaterial", target="f0"),
            None,
            id="nonmaterial-with-target",
        ),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="unsupported", target="f0"),
            None,
            id="unsupported-with-target",
        ),
        pytest.param(
            reconcile_disposition("m1", "remove", reason="duplicate"),
            None,
            id="duplicate-no-target",
        ),
        pytest.param(
            _superseded(target="f0", text="x"),
            None,
            id="superseded-with-text",
        ),
    ],
)
def test_superseded_target_is_validated_like_merge_and_duplicate(m1, f0):
    ledger = _ledger()
    overrides = {"m1": m1}
    if f0 is not None:
        overrides["f0"] = f0
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger, **overrides))


# --- notes79 atomic: source-support cues and guards ---

CUE_SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {"id": "n1", "start_seconds": 0.0, "end_seconds": 1.0, "text": "Next is Priya Okafor."},
        {
            "id": "n2",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text": "I am Priya Okafor and I coordinate the garden survey.",
        },
        {
            "id": "n3",
            "start_seconds": 2.0,
            "end_seconds": 3.0,
            "text": "Ms. Okafor, I will send you the survey form.",
            "speaker_name": "Chair Ito",
        },
        {
            "id": "n4",
            "start_seconds": 3.0,
            "end_seconds": 4.0,
            "text": "Library Board 4471 met. The library voted on the board seats.",
        },
        {
            "id": "n5",
            "start_seconds": 4.0,
            "end_seconds": 5.0,
            "text": "This adds 40 to 2 storage shelves for volunteers.",
        },
        {
            "id": "n6",
            "start_seconds": 5.0,
            "end_seconds": 6.0,
            "text": "Fees fell from 50 to 30 percent. Visits dropped 40 to 20 after the change.",
        },
        {
            "id": "n7",
            "start_seconds": 6.0,
            "end_seconds": 7.0,
            "text": "To clarify, the order is 40 shelves in total.",
        },
    ],
}


def _cue_ledger(section, text, source_ids, **fields):
    map_doc = _empty()
    item = {"text": text, "source_ids": list(source_ids)}
    if section == "action_items":
        item.update({"owner": None, "deadline_text": None, "deadline_date": None, **fields})
    map_doc[section] = [item]
    return ReconcileLedger.build(CUE_SOURCE, _empty(), [map_doc])


def _cue_payload(ledger):
    return ledger.prompt_payload()["map_items"][0]


def test_uncited_source_name_is_cued_with_closest_candidates_and_rejected_on_keep():
    ledger = _cue_ledger("key_topics", "Priya Okafor shared survey results.", ["n3"])
    assert _cue_payload(ledger)["attention"] == {
        "uncited_names": [{"name": "Priya Okafor", "candidate_ids": ["n2", "n1"]}]
    }
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        ledger.apply(_keep_all(ledger))
    repaired = _update(
        "m0", "Priya Okafor shared survey results.", ["n2", "n3"], [("n2", "Priya Okafor")]
    )
    assert ledger.apply(_keep_all(ledger, m0=repaired)).document["key_topics"][0]["source_ids"] == [
        "n2",
        "n3",
    ]


def test_merge_citation_union_can_repair_an_uncited_name():
    final = _empty()
    final["key_topics"] = [{"text": "Priya Okafor shared results.", "source_ids": ["n3"]}]
    map_doc = _empty()
    map_doc["key_topics"] = [{"text": "Priya Okafor shared results.", "source_ids": ["n2"]}]
    ledger = ReconcileLedger.build(CUE_SOURCE, final, [map_doc])
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        ledger.apply(
            _keep_all(ledger, m0=reconcile_disposition("m0", "remove", reason="unsupported"))
        )
    merged = ledger.apply(_keep_all(ledger, m0=reconcile_disposition("m0", "merge", target="f0")))
    assert merged.document["key_topics"] == [
        {"text": "Priya Okafor shared results.", "source_ids": ["n3", "n2"]}
    ]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Library Board 4471 met.", id="generic-capitalised-title"),
        pytest.param("Hội Đồng Thành Phố bàn về khu vườn.", id="translated-heading"),
        pytest.param("Okafor follow-up planned.", id="single-word"),
    ],
)
def test_generic_titles_translated_headings_and_single_words_are_not_name_cues(text):
    ledger = _cue_ledger("key_topics", text, ["n3"])
    assert "attention" not in _cue_payload(ledger)
    ledger.apply(_keep_all(ledger))


def test_translated_item_still_needs_the_cited_source_name():
    ledger = _cue_ledger("key_topics", "Bà Priya Okafor chia sẻ kết quả.", ["n3"])
    assert _cue_payload(ledger)["attention"]["uncited_names"][0]["name"] == "Priya Okafor"
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_keep_all(ledger))


def test_owner_needs_literal_cited_text_or_speaker_even_when_unchanged():
    uncited = _cue_ledger("action_items", "Send the survey form.", ["n3"], owner="Priya Okafor")
    assert _cue_payload(uncited)["attention"]["uncited_names"] == [
        {"name": "Priya Okafor", "candidate_ids": ["n2", "n1"]}
    ]
    with pytest.raises(nd.NotesDocumentError) as failure:
        uncited.apply(_keep_all(uncited))
    assert failure.value.field_name == "owner"
    speaker = _cue_ledger("action_items", "Send the survey form.", ["n3"], owner="Chair Ito")
    assert speaker.apply(_keep_all(speaker)).document["action_items"][0]["owner"] == "Chair Ito"


def test_incoherent_descending_range_detection_is_generic_and_clause_scoped():
    from asr_studio.notes_reconcile import _incoherent_ranges

    assert _incoherent_ranges("This adds 50 to 3 storage shelves.") == [("50", "50 to 3")]
    assert _incoherent_ranges("Tạo 12 đến 2 căn hộ.") == [("12", "12 đến 2")]
    assert _incoherent_ranges("Budget 15 to $2500 a month; 2 to 5 people.") == []
    assert _incoherent_ranges("Fees fell from 50 to 30 percent.") == []
    assert _incoherent_ranges("Units dropped 40 to 20. Then 9 to 1 rooms.") == [("9", "9 to 1")]
    assert _incoherent_ranges("Giảm từ 50 đến 30 căn.") == []


def test_incoherent_range_value_cannot_be_published_as_a_confident_count():
    ledger = _cue_ledger("key_topics", "Order adds 40 shelves.", ["n5"])
    assert _cue_payload(ledger)["attention"] == {
        "unclear_ranges": [{"value": "40", "source_id": "n5", "span": "40 to 2"}]
    }
    with pytest.raises(nd.NotesDocumentError, match="confident quantity"):
        ledger.apply(_keep_all(ledger))
    # Dropping the ambiguous source ID is no bypass: the original entry's risk still applies.
    bypass = _update("m0", "Order adds 40 shelves.", ["n4"], [("n4", "Library Board")])
    with pytest.raises(nd.NotesDocumentError, match="confident quantity"):
        ledger.apply(_keep_all(ledger, m0=bypass))


@pytest.mark.parametrize(
    ("text", "ids", "quotes"),
    [
        pytest.param(
            "Order adds shelves; count unclear.",
            ["n5"],
            [("n5", "storage shelves")],
            id="quantity-removed",
        ),
        pytest.param(
            "Order adds '40 to 2 storage' shelves (unclear).",
            ["n5"],
            [("n5", "40 to 2 storage shelves")],
            id="quoted-as-unclear",
        ),
        pytest.param(
            "Order adds 40 shelves.",
            ["n5", "n7"],
            [("n7", "the order is 40 shelves")],
            id="later-exact-clarification",
        ),
    ],
)
def test_incoherent_range_may_be_removed_marked_unclear_or_clarified_later(text, ids, quotes):
    ledger = _cue_ledger("key_topics", "Order adds 40 shelves.", ["n5"])
    document = ledger.apply(_keep_all(ledger, m0=_update("m0", text, ids, quotes))).document
    assert document["key_topics"][0]["text"] == text


def test_legitimate_descending_change_is_allowed_and_additions_are_checked():
    ledger = _cue_ledger("key_topics", "Fees fell from 50 to 30 percent.", ["n6"])
    assert "attention" not in _cue_payload(ledger)
    ledger.apply(_keep_all(ledger))
    patch = _keep_all(ledger)
    patch["additions"] = [
        _addition("key_topics", "Order adds 40 shelves.", ["n5"], [("n5", "40 to 2 storage")])
    ]
    with pytest.raises(nd.NotesDocumentError, match="confident quantity"):
        ledger.apply(patch)


# --- action metadata: merge/duplicate must not lose owner or deadline -----------------------

META_SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": "t1",
            "start_seconds": 0.0,
            "end_seconds": 4.0,
            "text": "I will send memo by tomorrow.",
            "speaker_name": "Ada",
        },
        {
            "id": "t2",
            "start_seconds": 4.0,
            "end_seconds": 8.0,
            "text": "I will send memo by tomorrow.",
            "speaker_name": "Mia",
        },
    ],
}


def _action(owner, deadline_text, deadline_date=None, source_id="t1"):
    return {
        "text": "Send memo.",
        "source_ids": [source_id],
        "owner": owner,
        "deadline_text": deadline_text,
        "deadline_date": deadline_date,
    }


def _meta_ledger(final_action, map_action):
    final, mapped = _empty(), _empty()
    final["action_items"] = [final_action]
    mapped["action_items"] = [map_action]
    return ReconcileLedger.build(META_SOURCE, final, [mapped])


def _dup(ledger, f0=None, action="remove"):
    return {
        "ledger_version": 2,
        "dispositions": [
            f0 or reconcile_disposition("f0"),
            reconcile_disposition(
                "m0", action, reason="duplicate" if action == "remove" else "none", target="f0"
            ),
        ],
        "additions": [],
    }


def _targets(ledger):
    return ledger.prompt_payload()["map_items"][0]["allowed_merge_targets"]


@pytest.mark.parametrize(
    ("final_meta", "map_meta"),
    [
        pytest.param(("Ada", "tomorrow"), ("Mia", "tomorrow"), id="distinct-owners"),
        pytest.param(("Ada", "tomorrow"), ("Ada", "Friday"), id="deadline-text-mismatch"),
        pytest.param(("Ada", "tomorrow"), ("Ada", None), id="deadline-vs-null"),
        pytest.param(("Ada", None), ("Mia", None), id="owners-no-deadline"),
        pytest.param((None, "tomorrow"), ("Ada", "tomorrow"), id="owner-null-vs-set"),
        pytest.param(("Ada", "2026-01-06"), ("Ada", "tomorrow"), id="canonical-date-text-differs"),
    ],
)
@pytest.mark.parametrize("action", ["remove", "merge"])
def test_incompatible_action_metadata_is_not_a_merge_target_and_is_rejected(
    final_meta, map_meta, action
):
    ledger = _meta_ledger(_action(*final_meta), _action(*map_meta, source_id="t2"))
    assert _targets(ledger) == []
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_dup(ledger, action=action))


@pytest.mark.parametrize(
    "map_date",
    [pytest.param(None, id="map-date-unresolved"), pytest.param("2026-01-06", id="map-date-set")],
)
@pytest.mark.parametrize("action", ["remove", "merge"])
def test_equal_action_metadata_stays_a_valid_target(map_date, action):
    ledger = _meta_ledger(
        _action("Ada", "tomorrow", "2026-01-06"), _action("Ada", "tomorrow", map_date)
    )
    assert _targets(ledger) == ["f0"]
    document = ledger.apply(_dup(ledger, action=action)).document
    assert document["action_items"] == [_action("Ada", "tomorrow", "2026-01-06")] or [
        item["owner"] for item in document["action_items"]
    ] == ["Ada"]
    assert len(document["action_items"]) == 1


def test_update_that_changes_target_owner_rejects_duplicate_removal():
    ledger = _meta_ledger(_action("Ada", "tomorrow"), _action("Ada", "tomorrow"))
    update = _update(
        "f0", "Send memo.", ["t2"], [("t2", "send memo")], owner="Mia", deadline_text="tomorrow"
    )
    with pytest.raises(nd.NotesDocumentError):
        ledger.apply(_dup(ledger, f0=update))


def test_update_keeping_metadata_compares_canonical_date_not_none():
    # Updated target has deadline_date None until the final validation; the map item carries 01-06.
    ledger = _meta_ledger(
        _action("Ada", "tomorrow", "2026-01-06"), _action("Ada", "tomorrow", "2026-01-06")
    )
    update = _update(
        "f0", "Send memo.", ["t1"], [("t1", "send memo")], owner="Ada", deadline_text="tomorrow"
    )
    document = ledger.apply(_dup(ledger, f0=update)).document
    assert [(i["owner"], i["deadline_date"]) for i in document["action_items"]] == [
        ("Ada", "2026-01-06")
    ]


@pytest.mark.parametrize("language", ["en", "vi"])
def test_instructions_ask_only_for_changed_operations(language):
    prompt = _ledger().build_prompt(output_language=language)
    assert '{"ledger_version":2,"dispositions":[],"additions":[]}' in prompt
    assert "exactly one" not in prompt and "đúng một" not in prompt


def test_sparse_wire_schema_omits_unchanged_keep_operations():
    ledger = _ledger()
    actions = ledger.schema()["properties"]["dispositions"]["items"]["properties"]["action"]["enum"]
    assert "keep" not in actions
    assert set(actions) == {"update", "merge", "remove", "confirm"}


# ----- targeted repair: boundary names, collected failures, sparse repair -------------------

BOUNDARY_SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": "b1",
            "start_seconds": 0.0,
            "end_seconds": 1.0,
            "text": "We walked into Discovery",
            "speaker_name": "SPEAKER_05",
        },
        {
            "id": "b2",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "text": "Park. It was open.",
            "speaker_name": "SPEAKER_05",
        },
        {"id": "b3", "start_seconds": 2.0, "end_seconds": 3.0, "text": "Discovery Park hosts it."},
        {"id": "b4", "start_seconds": 3.0, "end_seconds": 4.0, "text": "Park benches need paint."},
    ],
}


def _boundary_ledger(source_ids):
    map_doc = _empty()
    map_doc["key_topics"] = [{"text": "Visit to Discovery Park.", "source_ids": source_ids}]
    return ReconcileLedger.build(BOUNDARY_SOURCE, _empty(), [map_doc])


def test_name_split_across_adjacent_cited_segments_is_cited():
    ledger = _boundary_ledger(["b2", "b1"])  # order of the citation list does not matter
    assert "attention" not in ledger.prompt_payload()["map_items"][0]
    assert ledger.apply(EMPTY_PATCH).counts["default_kept"] == 1


@pytest.mark.parametrize("source_ids", [["b1"], ["b2"], ["b1", "b4"]])
def test_name_not_in_contiguous_cited_text_still_rejects(source_ids):
    ledger = _boundary_ledger(source_ids)
    assert ledger.prompt_payload()["map_items"][0]["attention"]["uncited_names"][0]["name"] == (
        "Discovery Park"
    )
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        ledger.apply(EMPTY_PATCH)


def _collect_ledger():
    map_doc = _empty()
    map_doc["key_topics"] = [
        {"text": "Priya Okafor shared survey results.", "source_ids": ["n3"]},
        {"text": "Library Board 4471 met.", "source_ids": ["n4"]},
        {"text": "Garden survey coordinated.", "source_ids": ["n2"]},
    ]
    return ReconcileLedger.build(CUE_SOURCE, _empty(), [map_doc])


def _bad_patch():
    malformed = reconcile_disposition("m2", "update")
    del malformed["evidence"]
    return {
        "ledger_version": 2,
        "dispositions": [
            # case-changed quote: not exact
            _update("m1", "Library Board 4471 met.", ["n4"], [("n4", "library Board 4471")]),
            malformed,
            reconcile_disposition("m9", "remove", reason="unsupported"),
        ],
        "additions": [
            _addition("decisions", "Library voted.", ["n4"], [("n4", "The library voted")]),
            _addition("decisions", "Fees fell.", ["n6"], [("n6", "fees fell")]),
        ],
    }


def test_apply_collects_every_independent_ref_failure():
    with pytest.raises(nd.NotesDocumentError) as failure:
        _collect_ledger().apply(_bad_patch())
    error = failure.value
    assert [(item.ref, item.reason_code) for item in error.failures] == [
        ("m0", "items"),  # default keep: uncited name
        ("m1", "source_ids"),
        ("m2", "fields"),
        ("m9", "items"),
        ("addition:1", "source_ids"),
    ]
    assert error.failures[0].names == ("Priya Okafor",)
    # The first failure in ledger order still sets the public diagnostics.
    assert (error.reason_code, error.detail_code) == ("items", None)
    assert "Okafor" not in str(error) and "5 reconcile refs" in str(error)


def test_repair_replaces_only_failing_refs_over_accepted_operations():
    ledger = _collect_ledger()
    raw = json.dumps(_bad_patch()) + " " * 9_000  # beyond the generic 8 KB repair cap
    plan = ledger.plan_repair(raw)
    assert plan.refs == ("m0", "m1", "m2")
    assert plan.addition_slots == 1
    prompt = ledger.build_repair_prompt(plan)
    payload = json.JSONDecoder().raw_decode(prompt.split("REPAIR_INPUT_JSON:\n", 1)[1])[0]
    assert [item["ref"] for item in payload["failures"]] == ["m0", "m1", "m2"]
    assert payload["failures"][0]["errors"][0]["names"] == ["Priya Okafor"]
    assert payload["failures"][0]["prior_disposition"] is None
    assert payload["dropped_refs"] == ["m9"]
    assert len(payload["failed_additions"]) == 1
    ids = {segment["id"] for segment in payload["source"]["segments"]}
    assert {"n1", "n2", "n3", "n4", "n6"} <= ids and "n7" not in ids
    assert "allowed_merge_targets" in payload["failures"][0]

    good = {
        "ledger_version": 2,
        "dispositions": [
            _update(
                "m0", "Priya Okafor shared survey results.", ["n2", "n3"], [("n2", "Priya Okafor")]
            ),
            _update("m1", "Library Board 4471 met.", ["n4"], [("n4", "Library Board 4471")]),
            reconcile_disposition("m2", "keep"),
        ],
        "additions": [_addition("decisions", "Fees fell.", ["n6"], [("n6", "Fees fell")])],
    }
    result = ledger.parse_repair(json.dumps(good), plan)
    decisions = [item["text"] for item in result.document["decisions"]]
    assert decisions == ["Library voted.", "Fees fell."]  # accepted addition retained
    assert result.document["key_topics"][2] == {
        "text": "Garden survey coordinated.",
        "source_ids": ["n2"],
    }  # m2 explicitly restores the original after its malformed primary operation


@pytest.mark.parametrize(
    "repair",
    [
        pytest.param(
            {"dispositions": [reconcile_disposition("m3", "remove", reason="unsupported")]},
            id="unknown-or-accepted-ref",
        ),
        pytest.param(
            {
                "additions": [
                    _addition("decisions", "Fees fell.", ["n6"], [("n6", "Fees fell")]),
                    _addition("decisions", "Visits dropped.", ["n6"], [("n6", "Visits dropped")]),
                ]
            },
            id="extra-addition",
        ),
        pytest.param({"dispositions": []}, id="failing-ref-still-fails"),
    ],
)
def test_bad_repair_rejects_atomically(repair):
    ledger = _collect_ledger()
    plan = ledger.plan_repair(json.dumps(_bad_patch()))
    patch = {"ledger_version": 2, "dispositions": [], "additions": [], **repair}
    with pytest.raises(nd.NotesDocumentError):
        ledger.parse_repair(json.dumps(patch), plan)


def test_global_failures_have_no_sparse_repair():
    ledger = _collect_ledger()
    assert ledger.plan_repair("not json") is None
    assert (
        ledger.plan_repair(json.dumps({"ledger_version": 1, "dispositions": [], "additions": []}))
        is None
    )


def test_repair_prompt_is_checked_against_the_actual_schema(monkeypatch):
    ledger = _collect_ledger()
    plan = ledger.plan_repair(json.dumps(_bad_patch()))
    budget = nd.notes_budget("chatgpt", "gpt-5.6-sol")
    seen = {}
    original = nd._check_prompt

    def spy(prompt, **kwargs):
        seen.update(kwargs)
        return original(prompt, **kwargs)

    monkeypatch.setattr(nd, "_check_prompt", spy)
    ledger.build_repair_prompt(plan, budget=budget)
    assert seen["response_schema"] == ledger.schema() and seen["budget"] is budget


@pytest.mark.parametrize("language", ["en", "vi"])
def test_primary_prompt_requires_every_attention_ref(language):
    prompt = _collect_ledger().build_prompt(output_language=language)
    assert ("Every ref that has attention must be listed" in prompt) == (language == "en")
    assert ("Mọi ref có attention phải được liệt kê" in prompt) == (language == "vi")


def _repair_transport(calls, primary, repair):
    def reconcile(prompt, kwargs):
        if kwargs["attempt"] == "primary":
            return primary(prompt)
        assert "REPAIR_INPUT_JSON:\n" in prompt and "RECONCILE_INPUT_JSON:\n" not in prompt
        return repair(json.JSONDecoder().raw_decode(prompt.split("REPAIR_INPUT_JSON:\n", 1)[1])[0])

    return _long_transport(calls, reconcile)


def _bad_primary(prompt):
    source = reconcile_payload(prompt)["source"]["segments"]
    second = source[1]["id"]
    patch = {
        "ledger_version": 2,
        "dispositions": [
            _update("m1", "Vendor renewal still open.", [second], [(second, "vendor contract")])
        ],
        "additions": [
            _addition(
                "open_questions",
                "Vendor renewal timing open.",
                [second],
                [(second, "renewal is still open")],
            )
        ],
    }
    return json.dumps(patch, indent=2) + " " * 9_000


def test_pipeline_sparse_repair_fixes_a_large_reconcile_response():
    calls: list[dict] = []
    coverage: list[dict] = []

    def repair(payload):
        assert [item["ref"] for item in payload["failures"]] == ["m1"]
        second = payload["failures"][0]["source_ids"][0]
        return json.dumps(
            {
                "ledger_version": 2,
                "additions": [],
                "dispositions": [
                    _update(
                        "m1", "Vendor renewal still open.", [second], [(second, "Vendor contract")]
                    )
                ],
            }
        )

    result = _run(_repair_transport(calls, _bad_primary, repair), coverage=coverage)
    assert [c["attempt"] for c in calls if c["stage"] == "reconcile"] == ["primary", "repair"]
    assert [i["text"] for i in result["key_topics"]] == [
        "Launch plan is approved.",
        "Vendor renewal still open.",
    ]
    assert [i["text"] for i in result["open_questions"]] == ["Vendor renewal timing open."]
    assert coverage[0]["source_audit"]["provider_call_index"] == len(calls)


def test_pipeline_repair_touching_an_accepted_ref_fails_without_publishing():
    calls: list[dict] = []

    def repair(_payload):
        return json.dumps(
            {
                "ledger_version": 2,
                "additions": [],
                "dispositions": [reconcile_disposition("m0", "remove", reason="unsupported")],
            }
        )

    with pytest.raises(AssertionError) as failure:
        _run(_repair_transport(calls, _bad_primary, repair))
    assert failure.value.args[0]["attempt"] == "repair"
    assert failure.value.args[0]["repair_state"] == "already_consumed"


def test_repair_must_explicitly_resolve_a_bad_update_even_if_original_was_valid():
    ledger = _ledger()
    invalid = {
        "ledger_version": 2,
        "dispositions": [_update("m0", "Invalid update.", ["s1"], [("s1", "NOT AN EXACT QUOTE")])],
        "additions": [],
    }
    plan = ledger.plan_repair(json.dumps(invalid))
    assert plan.refs == ("m0",)
    # Omitting the failed update would otherwise quietly restore a valid original item.
    with pytest.raises(nd.NotesDocumentError, match="every failing ref"):
        ledger.parse_repair(json.dumps(EMPTY_PATCH), plan)
