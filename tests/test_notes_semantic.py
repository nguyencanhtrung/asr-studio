"""Registered-Gemini long notes: one sparse immutable-reference source-binding audit pass."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest
from notes_helpers import (
    reconcile_keep_patch,
    semantic_keep_patch,
    semantic_patch,
    semantic_payload,
    semantic_refs,
)

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


_REFS = ("i0", "i1", "i2", "i3")  # the items of _document()


def _patch(*changes: dict, unresolved=(), refs=_REFS) -> str:
    return semantic_patch(refs, changes, unresolved)


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
        "responsibility_unclear": 0,
        "core_selected": 0,
        "core_preserved": 0,
        "reviewed": 4,
        "model_verified": 1,  # i2 only: every listed ref is decided false
    }
    assert audit.parse(semantic_keep_patch(audit))[0] == _document()


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
        (_patch(refs=_REFS[:-1]), "items"),  # a missing verified ref
        (json.dumps({**json.loads(_patch()), "unresolved_refs": None}), "fields"),
        (json.dumps({k: v for k, v in json.loads(_patch()).items() if k != "verified"}), "fields"),
        (json.dumps({**json.loads(_patch()), "audit_version": 1}), "version"),
        (
            json.dumps(
                {**json.loads(_patch(_change("i0"))), "changes": [{**_change("i0"), "text": "x"}]}
            ),
            "fields",
        ),  # no free-form prose field
        (
            json.dumps(
                {
                    **json.loads(_patch(_change("i0"))),
                    "changes": [
                        {
                            **{k: v for k, v in _change("i0").items() if k != "add_source_ids"},
                            "source_ids": [ID[1]],
                        }
                    ],
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
        if not ref:
            return semantic_keep_patch(prompt)
        return _patch(_change(ref, f"{ref}.a1"), refs=semantic_refs(prompt))

    result = _run(_transport(calls, semantic), coverage=coverage)

    stages = [c["stage"] for c in calls]
    # two owned reconcile views (v35) before the two semantic-audit parts
    assert stages == [
        "chunk",
        "chunk",
        "reconcile",
        "reconcile",
        "semantic_audit",
        "semantic_audit",
    ]
    assert [i["text"] for i in result["key_topics"]] == [
        "Launch plan approved",
        "Noted renewal still open",
    ]
    audit = coverage[0]["semantic_audit"]
    assert audit["provider_call_index"] == 6 and sorted(audit["provider_call_indices"]) == [5, 6]
    assert audit["part_count"] == 2
    assert audit["counts"]["anonymized"] == 1
    assert coverage[0]["provider_calls"] == 6 <= nd.MAX_PROVIDER_CALLS


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
        return (
            _patch(unresolved=[ref], refs=semantic_refs(prompt))
            if ref
            else semantic_keep_patch(prompt)
        )

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
        if not ref:
            return semantic_keep_patch(prompt)
        return _patch(unresolved=[ref, ref], refs=semantic_refs(prompt))

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
        _run(_transport(calls, lambda p, _k: semantic_keep_patch(p)))
    assert "semantic_audit" not in [c["stage"] for c in calls]


# ----- unresolved-ref quarantine ----------------------------------------------------------------


def _quarantine_audit(language: str = "en") -> SemanticAudit:
    document = _document()
    document["key_topics"].append({"text": "Tarrow and Metro agreed", "source_ids": [ID[2], ID[0]]})
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE), output_language=language)


_QREFS = (*_REFS, "i4")  # the items of _quarantine_audit()

_LABEL = (
    "Needs review: an unverified note was removed. Source excerpt (original language; "
    "output language: English): "
)


def test_unresolved_refs_become_literal_source_excerpts_without_shifting_other_items():
    audit = _quarantine_audit()
    original = copy.deepcopy(audit.document)
    text = [segment["text"] for segment in SOURCE["segments"]]
    document, counts = audit.parse(
        _patch(_change("i2", "i2.o", ids=[ID[0]]), unresolved=["i4", "i0", "i3"], refs=_QREFS)
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
    document, _ = _quarantine_audit("vi").parse(
        _patch(unresolved=["i0"], refs=_QREFS), output_language="vi"
    )
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
        audit.parse(_patch(unresolved=["i3"], refs=_QREFS))


@pytest.mark.parametrize("language", ["en", "vi"])
def test_prompt_explains_the_source_excerpt_quarantine(language):
    prompt = _audit(language).build_prompt(
        output_language=language, budget=nd.notes_budget(*GEMINI)
    )
    marker = {"en": "verbatim as a needs-review", "vi": "đăng nguyên văn các segment"}[language]
    assert marker in prompt
    assert nd.PROMPT_VERSION == "notes-hierarchical-v39"


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
            return semantic_keep_patch(prompt)
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


@pytest.mark.parametrize(
    ("n", "sizes"),
    [
        (0, None),
        (1, None),
        (2, [1, 1]),
        (3, [1, 1, 1]),
        (4, [1] * 4),
        (7, [1] * 7),
        (8, [1] * 8),
        (9, [2] + [1] * 7),
        (17, [3] + [2] * 7),
        (23, [3] * 7 + [2]),
    ],
)
def test_partitions_are_balanced_disjoint_and_cover_every_global_ref_once(n, sizes):
    audit = _n_item_audit(n)
    parts = audit.partitions()
    if sizes is None:
        assert parts == (audit,)
        return
    refs = [[item.ref for item in part.items] for part in parts]
    assert len(parts) == min(notes_semantic.MAX_AUDIT_PARTS, n) == len(sizes)
    assert notes_semantic.MAX_AUDIT_PARTS == 8
    assert [len(r) for r in refs] == sizes  # balanced, larger first
    assert sum(refs, []) == [f"i{k}" for k in range(n)]  # global refs, document order, once
    for part, own in zip(parts, refs, strict=True):
        assert set(part._by_ref) == set(own)
        assert {key.split(".")[0] for key in part._text_refs} == set(own)
        assert part.source is audit.source and part.document is audit.document


def test_part_prompt_and_schema_carry_their_visible_source_and_only_their_own_items():
    audit = _quarantine_audit()
    budget = nd.notes_budget(*GEMINI)
    parts = audit.partitions()
    assert len(parts) == 5  # min(8, 5): one item per part
    full_texts = {i["ref"]: i["text_refs"][0]["text"] for i in audit.prompt_payload()["items"]}
    for part in parts:
        own = {item.ref for item in part.items}
        others = [item for item in audit.items if item.ref not in own]
        payload = semantic_payload(part.build_prompt(budget=budget))
        assert {item["ref"] for item in payload["items"]} == own
        shown = [segment["id"] for segment in payload["source"]["segments"]]
        assert shown == list(part._visible)  # the visible source, global IDs in source order
        cited = {i for item in payload["items"] for i in item["source_ids"]}
        assert cited <= set(shown)  # every own cited ID is shown
        assert part.source is audit.source  # the guards keep the full source
        schema = part.schema()
        change = schema["properties"]["changes"]["items"]["properties"]
        assert set(change["ref"]["enum"]) == own
        assert {r.split(".")[0] for r in change["text_ref"]["enum"]} == own
        assert set(schema["properties"]["unresolved_refs"]["items"]["enum"]) == own
        verified = schema["properties"]["verified"]
        assert set(verified["required"]) == set(verified["properties"]) == own
        assert verified["additionalProperties"] is False
        assert "verified" in schema["required"]
        prompt = part.build_prompt(budget=budget)
        for item in others:
            assert f'"ref":"{item.ref}"' not in prompt
            assert full_texts[item.ref] not in prompt


@pytest.mark.parametrize(
    "patch",
    [
        _patch(_change("i3"), refs=("i0",)),  # another part's ref
        _patch(_change("i0", "i3.o"), refs=("i0",)),  # another part's text ref
        _patch(unresolved=["i4"], refs=("i0",)),  # another part's unresolved ref
        _patch(refs=("i0", "i1")),  # another part's verified ref
        _patch(refs=()),  # an own ref left undecided
    ],
)
def test_a_part_rejects_refs_outside_its_partition(patch):
    first = _quarantine_audit().partitions()[0]
    assert semantic_refs(first) == ["i0"]
    first.validated_patch(_patch(refs=("i0",)))  # the valid base
    with pytest.raises(nd.NotesDocumentError):
        first.validated_patch(patch)


def _part_patches(audit, changes=(), unresolved=()):
    """Each part's validated patch, holding only the changes and unresolved refs it owns."""
    patches = []
    for part in audit.partitions():
        own = set(semantic_refs(part))
        mine = [c for c in changes if c["ref"] in own]
        patch = _patch(
            *mine, unresolved=[r for r in unresolved if r in own], refs=semantic_refs(part)
        )
        patches.append((part, part.validated_patch(patch)))
    return patches


def test_merged_part_patches_equal_one_full_patch_without_index_shift():
    audit = _quarantine_audit()
    original = copy.deepcopy(audit.document)
    # i2's part sees ID[1] (its bounded neighbour), never the unshown ID[0].
    change, unresolved = _change("i2", "i2.o", ids=[ID[1]]), ["i4", "i0", "i3"]
    parts = _part_patches(audit, [change], unresolved)
    assert len(parts) == 5
    merged = audit.combine_patches(parts)
    expected = audit.parse(_patch(change, unresolved=unresolved, refs=_QREFS))
    assert merged == expected
    assert merged[1]["quarantined"] == 3 and merged[1]["changes"] == 1
    assert merged[1]["reviewed"] == 5 and merged[1]["model_verified"] == 1
    assert audit.document == original


def test_combine_rejects_overlap_gaps_cross_scope_and_an_oversized_merge(monkeypatch):
    audit = _quarantine_audit()
    parts = audit.partitions()
    keeps = [json.loads(semantic_keep_patch(part)) for part in parts]
    valid = list(zip(parts, keeps, strict=True))
    audit.combine_patches(valid)  # the valid base
    first, keep = valid[0]
    bad = [
        valid[:-1],  # a missing part
        [*valid, valid[0]],  # an overlapping part
        [*valid[:-1], valid[-2]],  # a repeated part in place of the last
        [(first, {**keep, "unresolved_refs": ["i4"]}), *valid[1:]],  # cross-part ref
        [(first, {**keep, "changes": [_change("i3")]}), *valid[1:]],
        [(first, keeps[1]), (parts[1], keep), *valid[2:]],  # verified from the other view
        [(first, {**keep, "verified": {**keep["verified"], "i2": True}}), *valid[1:]],
    ]
    for parts_ in bad:
        with pytest.raises(nd.NotesDocumentError):
            audit.combine_patches(parts_)
    big = _part_patches(audit, [_change("i0", "i0.o", ids=[ID[2]])])
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
        return semantic_keep_patch(prompt)

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
        if not ref:
            return semantic_keep_patch(prompt)
        return _patch(_change(ref, f"{ref}.a1"), refs=semantic_refs(prompt))

    result = _run(_transport(calls, semantic), coverage=coverage)
    assert [c["attempt"] for c in calls if c["stage"] == "semantic_audit"].count("repair") == 1
    assert result["key_topics"][1]["text"] == "Noted renewal still open"
    assert coverage[0]["semantic_audit"]["provider_call_index"] == 7


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
        _run(_transport(calls, lambda p, _k: semantic_keep_patch(p)))
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
        patches.append(
            (
                part,
                part.validated_patch(
                    _patch(*changes, unresolved=unresolved, refs=semantic_refs(part))
                ),
            )
        )
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


# ----- four-part audit pass in the pipeline -----------------------------------------------------


def _wide_snapshot() -> dict:
    texts = ["Launch plan is approved.", "Budget review is next.", "Hiring is paused."]
    texts += ["Vendor contract renews.", "Training starts soon."]
    starts = [0.0, 10.0, 20.0, 3_601.0, 3_610.0]
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            _segment(k + 1, s, t) for k, (s, t) in enumerate(zip(starts, texts, strict=True))
        ],
    }


def _wide_map_reply(prompt: str) -> str:
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    document = _empty()
    document["key_topics"] = [
        {"text": s["text"].rstrip("."), "source_ids": [s["id"]]}
        for s in source["segments"]
        if not s.get("overlap")
    ]
    return json.dumps(document)


def _wide_run(semantic, calls, coverage, reconcile=None, snapshot=None):
    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _wide_map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return (reconcile or (lambda p, _k: reconcile_keep_patch(p)))(prompt, kwargs)
        assert kwargs["stage"] == "semantic_audit"
        return semantic(prompt, kwargs)

    return generate_notes_document(
        snapshot=snapshot or _wide_snapshot(),
        output_language="en",
        provider=GEMINI[0],
        model=GEMINI[1],
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )


def test_every_audit_part_runs_concurrently_over_its_visible_source_and_decides_each_ref_once():
    calls: list[dict] = []
    coverage: list[dict] = []
    barrier = threading.Barrier(5, timeout=10)  # min(8, 5): breaks unless all five are in flight

    def semantic(prompt, kwargs):
        verified = kwargs["response_schema"]["properties"]["verified"]
        assert verified["required"] == semantic_refs(prompt)  # every owned ref is mandatory
        assert verified["additionalProperties"] is False
        barrier.wait()
        return semantic_keep_patch(prompt)

    result = _wide_run(semantic, calls, coverage)
    audits = [c for c in calls if c["stage"] == "semantic_audit"]
    owned = [semantic_refs(c["prompt"]) for c in audits]
    assert len(audits) == 5 and sorted(len(refs) for refs in owned) == [1] * 5
    assert sorted(sum(owned, [])) == [f"i{k}" for k in range(5)]  # disjoint, each once
    texts = [s["text"] for s in _wide_snapshot()["segments"]]
    shown = {}
    for call in audits:
        payload = semantic_payload(call["prompt"])
        segments = payload["source"]["segments"]
        assert list(call["allowed_source_ids"]) == [s["id"] for s in segments]  # citation scope
        positions = [texts.index(s["text"]) for s in segments]
        assert payload["source_scope"]["shown_positions"] == positions  # global order
        assert payload["source_scope"]["full_segment_count"] == 5
        for item in payload["items"]:
            shown[item["text_refs"][0]["text"]] = [texts[p] for p in positions]
    # Own segment plus a neighbour within 120 s; the 3,581 s gap is never crossed.
    first, second, third, fourth, fifth = (text.rstrip(".") for text in texts)
    assert shown == {
        first: texts[:2],
        second: texts[:3],
        third: texts[1:3],
        fourth: texts[3:],
        fifth: texts[3:],
    }
    assert len(result["key_topics"]) == 5
    audit = coverage[0]["semantic_audit"]
    assert audit["part_count"] == 5 and len(audit["provider_call_indices"]) == 5
    assert (audit["counts"]["reviewed"], audit["counts"]["model_verified"]) == (5, 5)
    assert coverage[0]["provider_calls"] == len(calls) <= nd.MAX_PROVIDER_CALLS


def _many_snapshot(n: int) -> dict:
    texts = [f"Agenda point {k} is recorded." for k in range(n)]
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        # Past one hour, like _wide_snapshot, so the registered long path runs.
        "segments": [
            _segment(k + 1, 10.0 * k + (3_600.0 if k >= n // 2 else 0.0), t)
            for k, t in enumerate(texts)
        ],
    }


def test_seventeen_items_run_as_eight_balanced_parts_with_one_shared_repair():
    calls: list[dict] = []
    coverage: list[dict] = []
    barrier = threading.Barrier(8, timeout=10)  # breaks unless all eight parts are in flight

    def semantic(prompt, kwargs):
        if kwargs["attempt"] == "primary":
            barrier.wait()
            if "i16" in semantic_refs(prompt):
                return _without_one_decision(prompt)  # earns the run's one repair
        return semantic_keep_patch(prompt)

    result = _wide_run(semantic, calls, coverage, snapshot=_many_snapshot(17))
    audits = [c for c in calls if c["stage"] == "semantic_audit"]
    primaries = [semantic_refs(c["prompt"]) for c in audits if c["attempt"] == "primary"]
    assert len(primaries) == 8
    assert sorted(len(refs) for refs in primaries) == [2] * 7 + [3]  # balanced, min(8, 17)
    assert sorted(sum(primaries, []), key=lambda r: int(r[1:])) == [f"i{k}" for k in range(17)]
    assert [c["attempt"] for c in audits].count("repair") == 1  # one global repair
    assert [c["attempt"] for c in calls].count("repair") == 1
    audit = coverage[0]["semantic_audit"]
    assert audit["part_count"] == 8 and len(audit["provider_call_indices"]) == 8
    assert (audit["counts"]["reviewed"], audit["counts"]["model_verified"]) == (17, 17)
    assert len(result["key_topics"]) == 17
    assert coverage[0]["provider_calls"] == len(calls) <= nd.MAX_PROVIDER_CALLS


def test_one_failed_part_of_eight_fails_the_task_unpublished():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        if "i9" in semantic_refs(prompt):
            return _without_one_decision(prompt)  # stays undecided after the repair
        return semantic_keep_patch(prompt)

    with pytest.raises((AssertionError, RuntimeError)):
        _wide_run(semantic, calls, coverage, snapshot=_many_snapshot(17))
    assert [c["attempt"] for c in calls].count("repair") == 1
    assert coverage == []


def _without_one_decision(prompt: str) -> str:
    patch = json.loads(semantic_keep_patch(prompt))
    patch["verified"].pop(semantic_refs(prompt)[0])
    return json.dumps(patch)


def test_an_undecided_ref_takes_the_shared_repair_once_and_then_publishes():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, kwargs):
        if "i4" in semantic_refs(prompt) and kwargs["attempt"] == "primary":
            return _without_one_decision(prompt)
        return semantic_keep_patch(prompt)

    _wide_run(semantic, calls, coverage)
    attempts = [c["attempt"] for c in calls if c["stage"] == "semantic_audit"]
    assert sorted(attempts) == ["primary"] * 5 + ["repair"]
    assert coverage[0]["semantic_audit"]["part_count"] == 5


def test_a_part_that_stays_undecided_fails_the_whole_task_unpublished():
    calls: list[dict] = []
    coverage: list[dict] = []

    def semantic(prompt, _kwargs):
        return (
            _without_one_decision(prompt)
            if "i4" in semantic_refs(prompt)
            else (semantic_keep_patch(prompt))
        )

    with pytest.raises(AssertionError) as failure:
        _wide_run(semantic, calls, coverage)
    assert failure.value.args[0]["stage"] == "semantic_audit"
    assert [c["attempt"] for c in calls].count("repair") == 1
    assert coverage == []  # no part's verdict is published alone


def test_the_single_repair_is_shared_between_reconcile_and_the_audit_parts():
    calls: list[dict] = []
    coverage: list[dict] = []
    reconciles: list[str] = []

    def reconcile(prompt, kwargs):
        reconciles.append(kwargs["attempt"])
        if len(reconciles) == 1:
            return "not json"  # spends the run's one repair
        return reconcile_keep_patch(prompt)

    def semantic(prompt, _kwargs):
        return (
            _without_one_decision(prompt)
            if "i0" in semantic_refs(prompt)
            else (semantic_keep_patch(prompt))
        )

    # The undecided part fails without a second repair; a sibling may instead see the stop.
    with pytest.raises((AssertionError, RuntimeError)) as failure:
        _wide_run(semantic, calls, coverage, reconcile=reconcile)
    assert "repair" in reconciles
    assert [c["attempt"] for c in calls].count("repair") == 1
    if isinstance(failure.value, AssertionError):
        assert failure.value.args[0]["repair_state"] == "already_consumed"
    assert coverage == []


def test_a_failed_part_stops_its_siblings_within_the_call_cap():
    calls: list[dict] = []
    coverage: list[dict] = []
    failed = threading.Event()
    started = threading.Barrier(5, timeout=10)  # all five primaries are in flight first

    def semantic(prompt, kwargs):
        if kwargs["attempt"] == "primary":
            started.wait()
        if "i0" in semantic_refs(prompt):
            failed.set()
            raise RuntimeError("provider down")
        failed.wait(10)
        time.sleep(0.2)
        return "not json"  # would earn the unused shared repair without the stop

    with pytest.raises(RuntimeError, match="provider down"):
        _wide_run(semantic, calls, coverage)
    attempts = [c["attempt"] for c in calls if c["stage"] == "semantic_audit"]
    assert attempts == ["primary"] * 5  # no sibling sends the repair after the failure
    assert len(calls) <= nd.MAX_PROVIDER_CALLS and coverage == []
