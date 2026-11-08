"""Registered-Gemini long notes: one sparse immutable-reference source-binding audit."""

from __future__ import annotations

import copy
import json

import pytest
from notes_helpers import SEMANTIC_KEEP_PATCH, reconcile_keep_patch, semantic_payload

from asr_studio import notes_document as nd
from asr_studio import notes_semantic
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.notes_reconcile import ReconcileLedger
from asr_studio.notes_semantic import SemanticAudit, anonymous_variants

GEMINI = ("antigravity", "gemini-3.8-flash")
CHATGPT = ("chatgpt", "gpt-5.6-sol")


def _empty() -> dict:
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _segment(index: int, start: float, text: str) -> dict:
    return {
        "id": f"{index:032x}",
        "start_seconds": start,
        "end_seconds": start + 1,
        "text": text,
        "speaker_name": "SPEAKER_00",
    }


SOURCE = {
    "meeting_date": "2026-01-05",
    "speakers": [],
    "segments": [
        _segment(1, 0.0, "the roll call: Tarrow yes, Okonjo yes."),
        _segment(2, 5.0, "I think the renewal must not slip past 6 months."),
        _segment(3, 9.0, "folks from Metro will review stops; the council agreed."),
        _segment(4, 12.0, "Someone will follow up on the contract."),
    ],
}
ID = [segment["id"] for segment in SOURCE["segments"]]


def _document() -> dict:
    document = _empty()
    document["key_topics"] = [
        {"text": "Tarrow noted renewal must not slip past 6 months", "source_ids": [ID[1]]},
        {"text": "Grand Valley Metro reviewing stops", "source_ids": [ID[2]]},
        {"text": "Council agreed review", "source_ids": [ID[2]]},
    ]
    document["action_items"] = [
        {
            "text": "Follow up on contract",
            "source_ids": [ID[3]],
            "owner": "Okonjo",
            "deadline_text": "Friday",
            "deadline_date": None,
        }
    ]
    return nd.validate_document(document, SOURCE)


def _audit() -> SemanticAudit:
    return SemanticAudit(SOURCE, _document())


def _change(ref: str, text_ref: str | None = None, ids=(), owner=False, deadline=False) -> dict:
    return {
        "ref": ref,
        "text_ref": text_ref or f"{ref}.o",
        "add_source_ids": list(ids),
        "clear_owner": owner,
        "clear_deadline": deadline,
    }


def _patch(*changes: dict, unresolved=()) -> str:
    return json.dumps(
        {"audit_version": 1, "changes": list(changes), "unresolved_refs": list(unresolved)}
    )


# ----- anonymous variants ---------------------------------------------------------------------


def test_variants_drop_only_a_leading_attribution_or_qualifier_as_exact_suffixes():
    text = "Bobby Lee noted 6-month study, not a ban, due June 1"
    variants = anonymous_variants(text, cited_text="Mr. Lee, please come forward.")
    assert variants[0] == "Noted 6-month study, not a ban, due June 1"
    assert "Lee noted 6-month study, not a ban, due June 1" in variants
    for variant in variants:  # a literal suffix up to the case of its first character
        assert text.endswith(variant[1:]) and variant[0].lower() == text[-len(variant)].lower()
        for protected in ("6-month", "not", "June 1"):
            assert protected in variant
    cited = "folks from Metro will review stops"
    assert anonymous_variants("Grand Valley Metro reviewing stops", cited_text=cited) == [
        "Metro reviewing stops"
    ]
    # A compound name the cited text supports, or no cited tail, is never shortened.
    assert anonymous_variants("Grand Valley Metro reviewing", cited_text="Grand Valley Metro") == []
    assert anonymous_variants("Grand Valley Metro reviewing stops") == []
    assert anonymous_variants("Lindqvist: supports the lease") == ["Supports the lease"]
    assert anonymous_variants("Lan cho biết hạn chót thứ Sáu") == ["Cho biết hạn chót thứ Sáu"]


@pytest.mark.parametrize(
    "text",
    [
        "March Lee said the plan holds",  # calendar word
        "Ten Members said yes",  # number word
        "Never Again argued",  # negation
        "Council Bill 119507 passed",  # generic heading in the source vocabulary
        "Council noted the plan holds",  # generic heading, not a speaker name
        "The 2026 Morgan said",  # digits stop the run
        "approved by Morgan",  # no leading name
        "Morgan",  # never the whole text
    ],
)
def test_variants_never_drop_protected_or_generic_leading_words(text):
    assert anonymous_variants(text, frozenset({"council", "bill", "the"})) == []


# ----- sparse patch ---------------------------------------------------------------------------


def test_sparse_patch_keeps_unlisted_items_and_only_selects_appends_and_clears():
    audit = _audit()
    texts = {
        t["text_ref"]: t["text"] for i in audit.prompt_payload()["items"] for t in i["text_refs"]
    }
    assert texts["i0.a1"] == "Noted renewal must not slip past 6 months"
    document, counts = audit.parse(
        _patch(
            _change("i0", "i0.a1"),
            _change("i1", "i1.a1", ids=[ID[2], ID[0]]),  # duplicate of a cited ID is a no-op
            _change("i3", owner=True, deadline=True),
        )
    )
    expected = _document()
    expected["key_topics"][0]["text"] = "Noted renewal must not slip past 6 months"
    expected["key_topics"][1] = {"text": "Metro reviewing stops", "source_ids": [ID[2], ID[0]]}
    expected["action_items"][0].update(owner=None, deadline_text=None, deadline_date=None)
    assert document == expected  # i2 unlisted: unchanged; order and sections preserved
    assert counts == {
        "changes": 3,
        "anonymized": 2,
        "cited_items": 1,
        "added_ids": 1,
        "cleared_owner": 1,
        "cleared_deadline": 1,
        "quarantined": 0,
    }
    assert audit.parse(SEMANTIC_KEEP_PATCH)[0] == _document()


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (_patch(unresolved=["i9"]), "items"),  # unknown unresolved ref
        (_patch(unresolved=["i0", "i0"]), "items"),  # duplicate unresolved ref
        (_patch(_change("i0"), unresolved=["i0"]), "items"),  # change/unresolved conflict
        (_patch(unresolved=[0]), "items"),  # wrong type
        (_patch(unresolved=["i0.o"]), "items"),  # a text ref is not an item ref
        (_patch(_change("i9")), "items"),  # unknown ref
        (_patch(_change("i0"), _change("i0", "i0.a1")), "items"),  # duplicate ref
        (_patch(_change("i1", "i0.a1")), "items"),  # cross-origin text ref
        (_patch(_change("i0", "i0.a9")), "items"),  # unknown text ref
        (_patch(_change("i0", ids=["f" * 32])), "source_ids"),
        (_patch(_change("i0", ids=[ID[0], ID[0]])), "source_ids"),
        (_patch(_change("i0", owner=True)), "action_fields"),
        (json.dumps({"audit_version": 1, "changes": []}), "fields"),
        (json.dumps({"audit_version": 2, "changes": [], "unresolved_refs": []}), "version"),
        (
            json.dumps(
                {
                    "audit_version": 1,
                    "changes": [{**_change("i0"), "text": "x"}],
                    "unresolved_refs": [],
                }
            ),
            "fields",
        ),  # no free-form prose field
        (
            json.dumps(
                {
                    "audit_version": 1,
                    "changes": [
                        {
                            **{k: v for k, v in _change("i0").items() if k != "add_source_ids"},
                            "source_ids": [ID[1]],
                        }
                    ],
                    "unresolved_refs": [],
                }
            ),
            "fields",
        ),  # a full replacement citation list is not part of the contract
        ("not json", "json"),
    ],
)
def test_invalid_patches_fail_closed(patch, reason):
    with pytest.raises(nd.NotesDocumentError) as failure:
        _audit().parse(patch)
    assert failure.value.reason_code == reason


@pytest.mark.parametrize("language", ["en", "vi"])
def test_prompt_states_the_speaker_and_modifier_binding_rules(language):
    prompt = _audit().build_prompt(output_language=language, budget=nd.notes_budget(*GEMINI))
    markers = {
        "en": (
            "does not identify who made a statement",
            "self-introduction",
            "cannot be borrowed",
            "never a reason for unresolved_refs",
        ),
        "vi": (
            "không cho biết ai đã phát biểu",
            "tự giới thiệu",
            "Không được mượn",
            "không bao giờ là lý do đưa vào unresolved_refs",
        ),
    }[language]
    for marker in markers:
        assert marker in prompt
    schema = _audit().schema()
    assert schema["properties"]["changes"]["items"]["properties"]["text_ref"]["enum"][:2] == [
        "i0.o",
        "i0.a1",
    ]
    assert set(schema["properties"]["changes"]["items"]["properties"]) == {
        "ref",
        "text_ref",
        "add_source_ids",
        "clear_owner",
        "clear_deadline",
    }


# ----- pipeline -------------------------------------------------------------------------------


def _long_snapshot() -> dict:
    # The name is source-true and cited, but only as a roll-call voter, not as the speaker.
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            _segment(1, 0.0, "Launch plan is approved."),
            _segment(2, 3_601.0, "the roll call: Tarrow yes."),
            _segment(3, 3_605.0, "I think the renewal is still open."),
        ],
    }


def _map_reply(prompt: str) -> str:
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    owned = [s for s in source["segments"] if not s.get("overlap")]
    document = _empty()
    if len(owned) == 1:
        document["key_topics"] = [{"text": "Launch plan approved", "source_ids": [owned[0]["id"]]}]
    else:
        document["key_topics"] = [
            {"text": "Tarrow noted renewal still open", "source_ids": [s["id"] for s in owned]}
        ]
    return json.dumps(document)


def _run(transport, provider=GEMINI, coverage=None):
    return generate_notes_document(
        snapshot=_long_snapshot(),
        output_language="en",
        provider=provider[0],
        model=provider[1],
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=(coverage if coverage is not None else []).append,
    )


def _transport(calls, semantic):
    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        assert kwargs["stage"] == "semantic_audit"
        return semantic(prompt, kwargs)

    return transport


def test_long_gemini_runs_exactly_one_audit_after_reconcile_and_publishes_the_variant():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        item = semantic_payload(prompt)["items"][1]
        return _patch(_change(item["ref"], f"{item['ref']}.a1"))

    result = _run(_transport(calls, semantic), coverage=coverage)

    assert [c["stage"] for c in calls] == ["chunk", "chunk", "reconcile", "semantic_audit"]
    assert [i["text"] for i in result["key_topics"]] == [
        "Launch plan approved",
        "Noted renewal still open",
    ]
    assert coverage[0]["semantic_audit"]["provider_call_index"] == 4
    assert coverage[0]["semantic_audit"]["counts"]["anonymized"] == 1
    assert coverage[0]["provider_calls"] == 4 <= nd.MAX_PROVIDER_CALLS


@pytest.mark.parametrize("provider", [CHATGPT, ("antigravity", "gemini-test")])
def test_other_long_paths_send_no_semantic_audit(provider):
    calls: list[dict] = []
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs["stage"])
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        document = _empty()
        document["key_topics"] = [
            {"text": "Renewal open", "source_ids": [kwargs["allowed_source_ids"][-1]]}
        ]
        return json.dumps(document)

    _run(transport, provider=provider, coverage=coverage)
    assert "semantic_audit" not in calls and "semantic_audit" not in coverage[0]


def test_unresolved_audit_ref_is_published_as_a_quarantined_source_excerpt():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        return _patch(unresolved=[semantic_payload(prompt)["items"][1]["ref"]])

    result = _run(_transport(calls, semantic), coverage=coverage)

    assert [c["stage"] for c in calls] == ["chunk", "chunk", "reconcile", "semantic_audit"]
    assert [i["text"] for i in result["key_topics"]] == ["Launch plan approved"]
    snapshot = _long_snapshot()["segments"]
    assert result["confirmation_items"][0]["source_ids"][:2] == [s["id"] for s in snapshot[1:]]
    text = result["confirmation_items"][0]["text"]
    assert "Tarrow noted" not in text
    assert text == (
        "Needs review: an unverified note was removed. Source excerpt (original language; "
        "output language: English): “the roll call: Tarrow yes.” "
        "“I think the renewal is still open.”"
    )
    assert coverage[0]["semantic_audit"]["counts"]["quarantined"] == 1


def test_invalid_unresolved_audit_uses_the_shared_repair_once_then_fails_unpublished():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        ref = semantic_payload(prompt)["items"][1]["ref"]
        return _patch(unresolved=[ref, ref])

    with pytest.raises(AssertionError) as failure:
        _run(_transport(calls, semantic), coverage=coverage)
    assert [(c["stage"], c["attempt"]) for c in calls[-2:]] == [
        ("semantic_audit", "primary"),
        ("semantic_audit", "repair"),
    ]
    assert failure.value.args[0]["stage"] == "semantic_audit"
    assert failure.value.args[0]["repair_state"] == "already_consumed"
    assert coverage == []


def test_audit_after_a_used_repair_gets_no_second_repair():
    calls: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "chunk":
            return "not json" if len(calls) == 1 else _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        return "not json"

    with pytest.raises(AssertionError) as failure:
        _run(transport)
    assert [c["attempt"] for c in calls].count("repair") == 1
    assert [c["stage"] for c in calls].count("semantic_audit") == 1
    assert failure.value.args[0]["repair_state"] == "already_consumed"


def test_audit_prompt_over_budget_fails_the_task_instead_of_skipping(monkeypatch):
    calls: list[dict] = []

    def too_large(self, **_kwargs):
        raise nd.NotesDocumentError("The notes prompt exceeds the provider request-size guard.")

    monkeypatch.setattr(notes_semantic.SemanticAudit, "build_prompt", too_large)
    with pytest.raises(RuntimeError, match="semantic audit limit"):
        _run(_transport(calls, lambda _p, _k: SEMANTIC_KEEP_PATCH))
    assert "semantic_audit" not in [c["stage"] for c in calls]


# ----- unresolved-ref quarantine ----------------------------------------------------------------


def _quarantine_audit() -> SemanticAudit:
    document = _document()
    document["key_topics"].append({"text": "Tarrow and Metro agreed", "source_ids": [ID[2], ID[0]]})
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE))


_LABEL = (
    "Needs review: an unverified note was removed. Source excerpt (original language; "
    "output language: English): "
)


def test_unresolved_refs_become_literal_source_excerpts_without_shifting_other_items():
    audit = _quarantine_audit()
    original = copy.deepcopy(audit.document)
    text = [segment["text"] for segment in SOURCE["segments"]]
    document, counts = audit.parse(
        _patch(_change("i2", "i2.o", ids=[ID[0]]), unresolved=["i4", "i0", "i3"])
    )
    # i0 (first key topic), i3 (action item), i4 removed; i1/i2 kept in order with i2's change.
    assert document["key_topics"] == [
        {"text": "Grand Valley Metro reviewing stops", "source_ids": [ID[2]]},
        {"text": "Council agreed review", "source_ids": [ID[2], ID[0]]},
    ]
    assert document["action_items"] == []
    assert document["confirmation_items"] == [  # document order, original citations, no metadata
        {"text": _LABEL + f"“{text[1]}”", "source_ids": [ID[1]]},
        {"text": _LABEL + f"“{text[2]}” “{text[0]}”", "source_ids": [ID[2], ID[0]]},
        {"text": _LABEL + f"“{text[3]}”", "source_ids": [ID[3]]},
    ]
    assert counts["quarantined"] == 3 and counts["changes"] == 1
    assert audit.document == original


def test_quarantine_label_follows_the_output_language():
    document, _ = _quarantine_audit().parse(_patch(unresolved=["i0"]), output_language="vi")
    assert document["confirmation_items"][0]["text"] == (
        "Cần xem lại: đã bỏ một ghi chú chưa được kiểm chứng. Trích nguyên văn (ngôn ngữ gốc; "
        f"ngôn ngữ đầu ra: tiếng Việt): “{SOURCE['segments'][1]['text']}”"
    )


def test_quarantine_keeps_a_long_excerpt_whole_or_fails_without_clipping():
    def audit(length: int) -> SemanticAudit:
        source = copy.deepcopy(SOURCE)
        source["segments"][1]["text"] = "x" * length
        return SemanticAudit(source, nd.validate_document(_document(), source))

    document, _ = audit(11_800).parse(_patch(unresolved=["i0"]))
    assert document["confirmation_items"][0]["text"] == _LABEL + "“" + "x" * 11_800 + "”"
    with pytest.raises(nd.NotesDocumentError) as failure:
        audit(11_990).parse(_patch(unresolved=["i0"]))
    assert failure.value.reason_code == "text"


def test_unresolved_item_without_a_cited_source_fails():
    document = _document()
    document["key_topics"].append({"text": "Uncited claim", "source_ids": []})
    audit = SemanticAudit(SOURCE, nd.validate_document(document, SOURCE))
    with pytest.raises(nd.NotesDocumentError):
        audit.parse(_patch(unresolved=["i3"]))


@pytest.mark.parametrize("language", ["en", "vi"])
def test_prompt_explains_the_source_excerpt_quarantine(language):
    prompt = _audit().build_prompt(output_language=language, budget=nd.notes_budget(*GEMINI))
    marker = {"en": "verbatim as a needs-review", "vi": "đăng nguyên văn các segment"}[language]
    assert marker in prompt
    assert nd.PROMPT_VERSION == "notes-hierarchical-v28"


# ----- reconcile reference-evidence wiring -----------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [(GEMINI, True), (CHATGPT, False), (("antigravity", "gemini-test"), False)],
)
def test_reference_evidence_is_enabled_only_with_the_mandatory_gemini_audit(
    monkeypatch, provider, expected
):
    flags: list[bool] = []
    build = ReconcileLedger.build.__func__

    def recording(cls, *args, **kwargs):
        ledger = build(cls, *args, **kwargs)
        flags.append(ledger.reference_evidence)
        return ledger

    monkeypatch.setattr(ReconcileLedger, "build", classmethod(recording))

    def transport(prompt, **kwargs):
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        document = _empty()
        document["key_topics"] = [
            {"text": "Renewal open", "source_ids": [kwargs["allowed_source_ids"][-1]]}
        ]
        return json.dumps(document)

    _run(transport, provider=provider)
    # Registered Gemini and ChatGPT reconcile once; an unregistered model has no reconcile.
    assert flags == ([expected] if provider != ("antigravity", "gemini-test") else [])
