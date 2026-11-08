"""Registered-Gemini long notes: one sparse immutable-reference source-binding audit pass."""

from __future__ import annotations

import copy
import json
import threading
import time

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


def _audit(language: str = "en") -> SemanticAudit:
    return SemanticAudit(SOURCE, _document(), output_language=language)


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
        "reference_unclear": 0,
        "quantity_unclear": 0,
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
    prompt = _audit(language).build_prompt(
        output_language=language, budget=nd.notes_budget(*GEMINI)
    )
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


def _tarrow_ref(prompt: str) -> str | None:
    """The ref of the attributed item when this audit part owns it."""
    for item in semantic_payload(prompt)["items"]:
        if item["text_refs"][0]["text"].startswith("Tarrow"):
            return item["ref"]
    return None


def test_long_gemini_runs_one_audit_pass_of_two_parts_after_reconcile_and_publishes_variant():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        ref = _tarrow_ref(prompt)
        return _patch(_change(ref, f"{ref}.a1")) if ref else SEMANTIC_KEEP_PATCH

    result = _run(_transport(calls, semantic), coverage=coverage)

    stages = [c["stage"] for c in calls]
    assert stages == ["chunk", "chunk", "reconcile", "semantic_audit", "semantic_audit"]
    assert [i["text"] for i in result["key_topics"]] == [
        "Launch plan approved",
        "Noted renewal still open",
    ]
    audit = coverage[0]["semantic_audit"]
    assert audit["provider_call_index"] == 5 and sorted(audit["provider_call_indices"]) == [4, 5]
    assert audit["part_count"] == 2
    assert audit["counts"]["anonymized"] == 1
    assert coverage[0]["provider_calls"] == 5 <= nd.MAX_PROVIDER_CALLS


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
        ref = _tarrow_ref(prompt)
        return _patch(unresolved=[ref]) if ref else SEMANTIC_KEEP_PATCH

    result = _run(_transport(calls, semantic), coverage=coverage)

    assert [c["stage"] for c in calls].count("semantic_audit") == 2
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
        ref = _tarrow_ref(prompt)
        return _patch(unresolved=[ref, ref]) if ref else SEMANTIC_KEEP_PATCH

    with pytest.raises(AssertionError) as failure:
        _run(_transport(calls, semantic), coverage=coverage)
    audit_calls = [c for c in calls if c["stage"] == "semantic_audit"]
    assert sorted(c["attempt"] for c in audit_calls) == ["primary", "primary", "repair"]
    assert _tarrow_ref(audit_calls[-1]["prompt"])  # the repair is for the failed part only
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
    # Each part sends at most its primary; a failed part stops a not-yet-sent sibling.
    assert 1 <= [c["stage"] for c in calls].count("semantic_audit") <= 2
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


def _quarantine_audit(language: str = "en") -> SemanticAudit:
    document = _document()
    document["key_topics"].append({"text": "Tarrow and Metro agreed", "source_ids": [ID[2], ID[0]]})
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE), output_language=language)


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
    document, _ = _quarantine_audit("vi").parse(_patch(unresolved=["i0"]), output_language="vi")
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
    prompt = _audit(language).build_prompt(
        output_language=language, budget=nd.notes_budget(*GEMINI)
    )
    marker = {"en": "verbatim as a needs-review", "vi": "đăng nguyên văn các segment"}[language]
    assert marker in prompt
    assert nd.PROMPT_VERSION == "notes-hierarchical-v33"


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


# ----- disjoint audit partitions ---------------------------------------------------------------


def _n_item_audit(n: int) -> SemanticAudit:
    document = _empty()
    document["key_topics"] = [
        {"text": f"Topic {k} reviewed", "source_ids": [ID[k % len(ID)]]} for k in range(n)
    ]
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE))


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 7])
def test_partitions_are_balanced_disjoint_and_cover_every_global_ref_once(n):
    audit = _n_item_audit(n)
    parts = audit.partitions()
    if n < 2:
        assert parts == (audit,)
        return
    refs = [[item.ref for item in part.items] for part in parts]
    assert len(parts) == 2 and [len(r) for r in refs] == [(n + 1) // 2, n // 2]
    assert refs[0] + refs[1] == [f"i{k}" for k in range(n)]  # global refs, document order
    for part, own in zip(parts, refs, strict=True):
        assert set(part._by_ref) == set(own)
        assert {key.split(".")[0] for key in part._text_refs} == set(own)
        assert part.source is audit.source and part.document is audit.document


def test_part_prompt_and_schema_carry_the_full_source_but_only_its_own_items():
    audit = _quarantine_audit()
    budget = nd.notes_budget(*GEMINI)
    first, second = audit.partitions()
    full_texts = {i["ref"]: i["text_refs"][0]["text"] for i in audit.prompt_payload()["items"]}
    for part, other in ((first, second), (second, first)):
        payload = semantic_payload(part.build_prompt(budget=budget))
        own = {item.ref for item in part.items}
        assert {item["ref"] for item in payload["items"]} == own
        assert payload["source"] == audit.prompt_payload()["source"]  # full source
        change = part.schema()["properties"]["changes"]["items"]["properties"]
        assert set(change["ref"]["enum"]) == own
        assert {r.split(".")[0] for r in change["text_ref"]["enum"]} == own
        assert set(part.schema()["properties"]["unresolved_refs"]["items"]["enum"]) == own
        prompt = part.build_prompt(budget=budget)
        for item in other.items:
            assert f'"ref":"{item.ref}"' not in prompt
            assert full_texts[item.ref] not in prompt


@pytest.mark.parametrize(
    "patch",
    [
        _patch(_change("i3")),  # another part's ref
        _patch(_change("i0", "i3.o")),  # another part's text ref
        _patch(unresolved=["i4"]),  # another part's unresolved ref
    ],
)
def test_a_part_rejects_refs_outside_its_partition(patch):
    first, _second = _quarantine_audit().partitions()
    with pytest.raises(nd.NotesDocumentError):
        first.validated_patch(patch)


def test_merged_part_patches_equal_one_full_patch_without_index_shift():
    audit = _quarantine_audit()
    first, second = audit.partitions()
    original = copy.deepcopy(audit.document)
    parts = [
        (
            first,
            first.validated_patch(_patch(_change("i2", "i2.o", ids=[ID[0]]), unresolved=["i0"])),
        ),
        (second, second.validated_patch(_patch(unresolved=["i4", "i3"]))),
    ]
    merged = audit.combine_patches(parts)
    expected = audit.parse(
        _patch(_change("i2", "i2.o", ids=[ID[0]]), unresolved=["i4", "i0", "i3"])
    )
    assert merged == expected
    assert merged[1]["quarantined"] == 3 and merged[1]["changes"] == 1
    assert audit.document == original


def test_combine_rejects_overlap_gaps_cross_scope_and_an_oversized_merge(monkeypatch):
    audit = _quarantine_audit()
    first, second = audit.partitions()
    keep = json.loads(SEMANTIC_KEEP_PATCH)
    bad = [
        [(first, keep)],  # a missing part
        [(first, keep), (first, keep)],  # an overlapping part
        [(first, keep), (second, keep), (second, keep)],
        [(first, {**keep, "unresolved_refs": ["i4"]}), (second, keep)],  # cross-part ref
        [(first, {**keep, "changes": [_change("i3")]}), (second, keep)],
    ]
    for parts in bad:
        with pytest.raises(nd.NotesDocumentError):
            audit.combine_patches(parts)
    big = [
        (first, first.validated_patch(_patch(_change("i0", "i0.o", ids=[ID[2]])))),
        (second, keep),
    ]
    monkeypatch.setattr(notes_semantic, "MAX_SEMANTIC_OUTPUT_BYTES", 120)
    with pytest.raises(nd.NotesDocumentError) as failure:
        audit.combine_patches(big)
    assert failure.value.reason_code == "size"


def test_the_two_audit_parts_run_concurrently_and_each_checks_its_items_once():
    calls: list[dict] = []
    coverage: list[dict] = []
    barrier = threading.Barrier(2, timeout=10)  # breaks unless both parts are in flight at once
    in_flight = [0, 0]  # current, maximum
    lock = threading.Lock()

    def semantic(prompt, _kwargs):
        with lock:
            in_flight[0] += 1
            in_flight[1] = max(in_flight)
        barrier.wait()
        with lock:
            in_flight[0] -= 1
        return SEMANTIC_KEEP_PATCH

    _run(_transport(calls, semantic), coverage=coverage)
    audited = [
        item["ref"]
        for call in calls
        if call["stage"] == "semantic_audit"
        for item in semantic_payload(call["prompt"])["items"]
    ]
    assert sorted(audited) == ["i0", "i1"] and in_flight[1] == 2
    assert coverage[0]["semantic_audit"]["part_count"] == 2


def test_one_malformed_part_uses_the_single_shared_repair_and_the_pass_succeeds():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, kwargs):
        ref = _tarrow_ref(prompt)
        if ref and kwargs["attempt"] == "primary":
            return "not json"
        return _patch(_change(ref, f"{ref}.a1")) if ref else SEMANTIC_KEEP_PATCH

    result = _run(_transport(calls, semantic), coverage=coverage)
    assert [c["attempt"] for c in calls if c["stage"] == "semantic_audit"].count("repair") == 1
    assert result["key_topics"][1]["text"] == "Noted renewal still open"
    assert coverage[0]["semantic_audit"]["provider_call_index"] == 6


def test_a_failed_part_stops_the_other_part_from_sending_more_requests():
    calls: list[dict] = []
    coverage: list[dict] = []
    failed = threading.Event()

    def semantic(prompt, kwargs):
        if _tarrow_ref(prompt):
            failed.set()
            raise RuntimeError("provider down")
        failed.wait(10)
        time.sleep(0.2)  # let the failed part mark the task aborted
        return "not json"  # would earn the unused shared repair without the stop

    with pytest.raises(RuntimeError, match="provider down"):
        _run(_transport(calls, semantic), coverage=coverage)
    assert [c["attempt"] for c in calls if c["stage"] == "semantic_audit"] == [
        "primary",
        "primary",
    ]
    assert coverage == []


def test_an_over_budget_part_fails_the_task_before_any_audit_request(monkeypatch):
    calls: list[dict] = []
    build = SemanticAudit.build_prompt

    def second_too_large(self, **kwargs):
        if self.items[0].ref != "i0":
            raise nd.NotesDocumentError("The notes prompt exceeds the provider request-size guard.")
        return build(self, **kwargs)

    monkeypatch.setattr(SemanticAudit, "build_prompt", second_too_large)
    with pytest.raises(RuntimeError, match="semantic audit limit"):
        _run(_transport(calls, lambda _p, _k: SEMANTIC_KEEP_PATCH))
    assert "semantic_audit" not in [c["stage"] for c in calls]


# ----- generic source-binding regressions across every section ---------------------------------

_CASE_SOURCE = {
    "meeting_date": "2026-02-02",
    "speakers": [],
    "segments": [
        _segment(1, 0.0, "Today we advance the confirmation of nominee Dana Quill as director."),
        _segment(2, 4.0, "Thank you. Our office will help everyone navigate the new economy."),
        _segment(
            3, 8.0, "I thank Ames, who supported my ordinance, and Brook, who brought this bill."
        ),
        _segment(4, 12.0, "These are 101, two and three bedroom apartments for renters."),
        _segment(5, 16.0, "The plan adds Section 7.20 4.0 24 to the code, which would"),
        _segment(6, 20.0, "come back to the council for final approval later."),
    ],
}
_CID = [segment["id"] for segment in _CASE_SOURCE["segments"]]


def _case_document() -> dict:
    document = _empty()
    document["overview"] = [
        {"text": "Incoming Director Dana Quill vision for the new economy", "source_ids": _CID[:2]}
    ]
    document["key_topics"] = [
        {"text": "Speaker thanked Ames, Brook for sponsoring the bill", "source_ids": [_CID[2]]}
    ]
    document["decisions"] = [
        {"text": "Plan includes 100 1-, 2- and 3-bedroom apartments", "source_ids": [_CID[3]]}
    ]
    document["open_questions"] = [
        {"text": "Whether SMC 7.20.020 needs amendment", "source_ids": [_CID[4]]}
    ]
    document["confirmation_items"] = [
        {"text": "Plan requires final council approval", "source_ids": [_CID[4]]}
    ]
    return nd.validate_document(document, _CASE_SOURCE)


def test_every_section_is_audited_and_unsupported_bindings_become_literal_excerpts():
    audit = SemanticAudit(_CASE_SOURCE, _case_document())
    parts = audit.partitions()
    sections = [item.section for part in parts for item in part.items]
    assert sections == [
        "overview",
        "key_topics",
        "decisions",
        "open_questions",
        "confirmation_items",
    ]
    # The audit's correct answer: no variant removes the nominee-as-speaker, sponsor role,
    # merged ASR count or guessed identifier; the procedural item gains its continuation.
    patches = []
    for part in parts:
        own = {item.ref: item for item in part.items}
        changes = [
            _change(ref, ids=[_CID[5]])
            for ref, item in own.items()
            if item.section == "confirmation_items"
        ]
        unresolved = [ref for ref, item in own.items() if item.section != "confirmation_items"]
        patches.append((part, part.validated_patch(_patch(*changes, unresolved=unresolved))))
    document, counts = audit.combine_patches(patches)
    published = json.dumps(document, ensure_ascii=False)
    for invented in ("Director Dana Quill vision", "sponsoring", "100 1-", "7.20.020"):
        assert invented not in published
    assert counts["quarantined"] == 4 and counts["added_ids"] == 1
    assert document["confirmation_items"][0] == {
        "text": "Plan requires final council approval",
        "source_ids": [_CID[4], _CID[5]],
    }
    excerpts = [item["text"] for item in document["confirmation_items"][1:]]
    assert any("101, two and three bedroom" in text for text in excerpts)
    assert any("Section 7.20 4.0 24" in text for text in excerpts)


@pytest.mark.parametrize("language", ["en", "vi"])
def test_prompt_states_the_generic_section_role_number_and_procedure_rules(language):
    prompt = _audit(language).build_prompt(
        output_language=language, budget=nd.notes_budget(*GEMINI)
    )
    markers = {
        "en": (
            "including overview titles, headings and confirmation_items",
            "nominee, candidate",
            "is not sponsoring",
            "never guess, correct or complete identifier digits",
            "garbled transcribed digits",
            "continues in the next segment",
            "Never supply background legal",
        ),
        "vi": (
            "kể cả tiêu đề tổng quan",
            "người được đề cử",
            "không phải là bảo trợ",
            "không bao giờ đoán",
            "chữ số bị ghi sai",
            "tiếp tục ở segment kế tiếp",
            "giả định pháp lý",
        ),
    }[language]
    for marker in markers:
        assert marker in prompt
