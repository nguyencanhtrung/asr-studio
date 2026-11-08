"""Semantic audit detail uncertainty: code-set quantity markers and legal-reference variants.

Every source, value and noun here is synthetic.
"""

from __future__ import annotations

import copy
import json

import pytest
from notes_helpers import (
    reconcile_keep_patch,
    semantic_keep_patch,
    semantic_patch,
    semantic_payload,
    semantic_refs,
)

from asr_studio import notes_document as nd
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.notes_semantic import (
    MAX_VARIANTS_PER_ITEM,
    SemanticAudit,
    mark_unclear_quantities,
    reference_variant,
)

Q = "[quantity unclear]"
R = "[reference unclear]"
FLEET = "We bought 243, four and five-seat vehicles for the depot."


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


def _source(*texts: str) -> dict:
    return {
        "meeting_date": "2026-03-04",
        "speakers": [],
        "segments": [_segment(i + 1, i * 4.0, text) for i, text in enumerate(texts)],
    }


def _ids(source: dict) -> list[str]:
    return [segment["id"] for segment in source["segments"]]


def _change(ref: str, text_ref: str, ids=()) -> dict:
    return {
        "ref": ref,
        "text_ref": text_ref,
        "add_source_ids": list(ids),
        "clear_owner": False,
        "clear_deadline": False,
    }


def _patch(*changes: dict, unresolved=(), refs=("i0",)) -> str:
    return semantic_patch(refs, changes, unresolved)


def _texts(audit: SemanticAudit) -> dict[str, str]:
    return {
        t["text_ref"]: t["text"] for i in audit.prompt_payload()["items"] for t in i["text_refs"]
    }


# ----- quantity marker ------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["243", "240", "24"])
def test_literal_rounded_and_prefix_totals_of_a_joined_size_list_are_marked(value):
    text, marks = mark_unclear_quantities(f"Depot adds {value} electric vehicles", FLEET)
    assert (text, marks) == (f"Depot adds {Q} electric vehicles", 1)


@pytest.mark.parametrize(
    ("cited", "claim"),
    [
        ("Our total is 1,303, four and five bedroom homes.", "1,303 homes"),  # thousands comma
        ("They hold 562, three and four room suites today.", "560 suites"),  # digits list
        ("These are 871, 2 and 3 bedroom flats.", "87 flats"),
    ],
)
def test_the_guard_is_generic_over_values_units_and_families(cited, claim):
    text, marks = mark_unclear_quantities(f"Plan: {claim}.", cited)
    assert marks == 1 and Q in text and claim.split()[0] not in text


@pytest.mark.parametrize("value", ["243", "240", "24"])
@pytest.mark.parametrize("language,marker", [("en", Q), ("vi", "[số lượng chưa rõ]")])
def test_repeated_ambiguous_counts_are_all_marked(value, language, marker):
    text, marks = mark_unclear_quantities(
        f"{value} vehicles and {value} vehicles; {value} electric vehicles", FLEET, language
    )
    assert (text, marks) == (
        f"{marker} vehicles and {marker} vehicles; {marker} electric vehicles",
        3,
    )


def test_marker_follows_the_output_language():
    text, marks = mark_unclear_quantities("Kho thêm 243 xe điện", FLEET, "vi")
    assert (text, marks) == ("Kho thêm [số lượng chưa rõ] xe điện", 1)


@pytest.mark.parametrize(
    "explicit",
    [
        "In total 240 vehicles will be delivered.",
        "That makes a total of 240 for the depot.",
        "The depot will run 240 shuttle vehicles.",
    ],
)
def test_a_separately_cited_unambiguous_count_overrides_the_guard(explicit):
    text, marks = mark_unclear_quantities(
        "Depot adds 240 vehicles; 243 vehicles listed", f"{FLEET} {explicit}"
    )
    assert text == f"Depot adds 240 vehicles; {Q} vehicles listed" and marks == 1


@pytest.mark.parametrize(
    ("cited", "claim"),
    [
        (FLEET, "243 apartments"),  # different object family
        ("We bought 243, four and six-seat vehicles.", "243 vehicles"),  # not consecutive
        ("We bought 245, four and five-seat vehicles.", "245 vehicles"),  # digit not before 4
        ("We bought 120, one and two-seat vehicles.", "120 vehicles"),  # inferred size 0
        ("These are 230, one and two bedroom apartments.", "230 apartments"),  # inferred size 0
        ("We bought 243, four and five vehicles.", "243 vehicles"),  # no size unit
        ("We bought 243, four and five-seat models.", "243 models"),  # no counted family
        ("We bought 243 vehicles with four and five seats.", "243 vehicles"),  # no joined list
        ("The depot is ready.", "243 vehicles"),  # no cited enumeration
    ],
)
def test_no_edit_outside_the_narrow_joined_list_class(cited, claim):
    assert mark_unclear_quantities(f"Plan: {claim}.", cited) == (f"Plan: {claim}.", 0)


def test_unrelated_and_protected_numbers_in_the_same_text_are_untouched():
    text = (
        "Bill 243 vehicles amendment passed 7-0 on 2024-06-24; 85 vans, 243 vehicles, "
        "24 hours for vehicles, 24% of vehicles, $240 vehicles, 24 days, year 2024, "
        "Resolution 240 vehicles"
    )
    marked, marks = mark_unclear_quantities(text, FLEET)
    assert marks == 1
    assert marked == text.replace("85 vans, 243 vehicles", f"85 vans, {Q} vehicles")


def _fleet_audit(language: str = "en", texts=("Depot adds 243 electric vehicles",)):
    source = _source(FLEET, "Bill 204877 passed seven to zero.", "The depot is ready.")
    document = _empty()
    document["key_topics"] = [{"text": text, "source_ids": [_ids(source)[0]]} for text in texts] + [
        {"text": "Depot ready", "source_ids": [_ids(source)[2]]}
    ]
    document = nd.validate_document(document, source)
    return source, document, SemanticAudit(source, document, output_language=language)


def test_marking_happens_before_refs_so_no_text_ref_can_restore_the_value():
    source, document, audit = _fleet_audit(texts=("Rivera said depot adds 243 vehicles",))
    source_before, document_before = copy.deepcopy(source), copy.deepcopy(document)
    texts = _texts(audit)
    assert texts["i0.o"] == f"Rivera said depot adds {Q} vehicles"
    assert all("243" not in text for text in texts.values()) and "i0.a1" in texts
    for text_ref in ("i0.o", "i0.a1"):
        published, counts = audit.parse(_patch(_change("i0", text_ref), refs=semantic_refs(audit)))
        assert "243" not in json.dumps(published) and counts["quantity_unclear"] == 1
    published, _ = audit.parse(semantic_keep_patch(audit))  # unlisted keeps the marked copy too
    assert published["key_topics"][0]["text"] == texts["i0.o"]
    assert (source, document) == (source_before, document_before)  # inputs are never mutated
    assert audit.source["segments"][0]["text"] == FLEET


def test_quantity_counts_are_reported_once_across_partitions():
    texts = ("Depot adds 243 vehicles", "Plan holds 24 vehicles", "Fleet notes")
    _source_, _document, audit = _fleet_audit(texts=texts)
    parts = audit.partitions()
    assert len(parts) == 4  # one item each: two marked texts, two plain
    patches = [(part, part.validated_patch(semantic_keep_patch(part))) for part in parts]
    published, counts = audit.combine_patches(patches)
    assert counts["quantity_unclear"] == 2
    assert [i["text"] for i in published["key_topics"][:2]] == [
        f"Depot adds {Q} vehicles",
        f"Plan holds {Q} vehicles",
    ]


def test_the_audit_language_is_fixed_by_its_constructor():
    _s, _d, audit = _fleet_audit("vi")
    assert _texts(audit)["i0.o"] == "Depot adds [số lượng chưa rõ] electric vehicles"
    with pytest.raises(nd.NotesDocumentError):
        audit.parse(semantic_keep_patch(audit), output_language="en")
    assert audit.parse(semantic_keep_patch(audit), output_language="vi")[1]["quantity_unclear"] == 1


# ----- legal reference variants ---------------------------------------------------------------

DECISION = (
    "Bill 204877 passed 7-0, amending Municipal Code 42.13.010 and 42.13.012 to adopt "
    "River District guidelines."
)


def test_reference_variant_replaces_only_the_labelled_dotted_list():
    assert reference_variant(DECISION) == DECISION.replace("42.13.010 and 42.13.012", R)
    assert reference_variant(DECISION, "vi") == DECISION.replace(
        "42.13.010 and 42.13.012", "[tham chiếu chưa rõ]"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Amends Section 5.2 on 2026-03-04", f"Amends Section {R} on 2026-03-04"),
        ("Per § 12.4.1(a), 3.5.7 and 3.5.8 apply", f"Per § {R} apply"),
        ("Bill 204877 updates RCW 46.61.502 by June 30", f"Bill 204877 updates RCW {R} by June 30"),
        ("Điều 12.3 được sửa", f"Điều {R} được sửa"),
        ("Clauses 4.1, 4.2, and 4.3 removed, vote 5-2", f"Clauses {R} removed, vote 5-2"),
    ],
)
def test_reference_variant_covers_generic_labels(text, expected):
    assert reference_variant(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Report dated 2019.06.10 approved",  # date-like, no label
        "Section 2019.06.10 approved",  # 4-digit year lead
        "Section 14.06.2027 approved",  # dotted day/month/year is still a date
        "Section 06.14.2027 approved",  # dotted month/day/year
        "App version 1.2.3 released",  # version, no statutory label
        "Installed XYZ 1.2.3",  # an acronym alone does not prove a legal reference
        "XYZ 1.2.3 released",
        "Bill 204877 upgrades XYZ 1.2.3",  # preserve software version even in a bill summary
        "Budget rose 4.5.6 points",  # unlabelled
        "Resolution 31882 adopted 9-0 on June 3",  # record number, not dotted
        "SMC 7.20 amended",  # acronym needs three components
        "Appointment 1363 confirmed for 2.5 years",
    ],
)
def test_reference_variant_leaves_dates_versions_records_and_unlabelled_numbers(text):
    assert reference_variant(text) is None


def _decision_audit(texts: tuple[str, ...]):
    source = _source(
        "Bill 204877 relating to Municipal Code 42.13 0 10 and 42.13 012.",
        "Seven in favor, none opposed. The bill passed.",
        "Rivera thanked staff.",
    )
    document = _empty()
    document["decisions"] = [{"text": text, "source_ids": _ids(source)[:2]} for text in texts]
    return source, SemanticAudit(source, nd.validate_document(document, source))


def test_model_selected_reference_variant_keeps_the_core_decision():
    source, audit = _decision_audit((DECISION,))
    texts = _texts(audit)
    assert set(texts) == {"i0.o", "i0.r"}
    published, counts = audit.parse(_patch(_change("i0", "i0.r")))
    assert published["decisions"] == [
        {"text": DECISION.replace("42.13.010 and 42.13.012", R), "source_ids": _ids(source)[:2]}
    ]
    assert published["confirmation_items"] == []
    assert (counts["reference_unclear"], counts["anonymized"], counts["quarantined"]) == (1, 0, 0)


def test_combined_actor_and_reference_variant_is_bounded_and_counted_separately():
    text = f"Rivera Lane said {DECISION[0].lower()}{DECISION[1:]}"
    _source_, audit = _decision_audit((text,))
    texts = _texts(audit)
    assert len(texts) <= 1 + MAX_VARIANTS_PER_ITEM
    assert texts["i0.r"] == text.replace("42.13.010 and 42.13.012", R)
    assert texts["i0.a1"].startswith("Said bill 204877 passed 7-0")
    assert texts["i0.a1r"] == texts["i0.a1"].replace("42.13.010 and 42.13.012", R)
    _published, counts = audit.parse(_patch(_change("i0", "i0.a1r")))
    assert (counts["reference_unclear"], counts["anonymized"]) == (1, 1)
    _published, counts = audit.parse(_patch(_change("i0", "i0.a1")))
    assert (counts["reference_unclear"], counts["anonymized"]) == (0, 1)


def test_items_without_a_labelled_reference_keep_their_variant_labels():
    _source_, audit = _decision_audit(("Rivera Lane said the bill passed 7-0",))
    assert set(_texts(audit)) == {"i0.o", "i0.a1"}


def test_no_text_ref_outside_the_code_derived_set_is_accepted():
    _source_, audit = _decision_audit((DECISION,))
    for text_ref in ("i0.a1", "i0.r2", "i0.x"):
        with pytest.raises(nd.NotesDocumentError):
            audit.parse(_patch(_change("i0", text_ref)))


# ----- pipeline wiring ------------------------------------------------------------------------


def _long_snapshot() -> dict:
    return {
        "meeting_date": "2026-03-04",
        "speakers": [],
        "segments": [
            _segment(1, 0.0, "Bill 204877 relating to Municipal Code 42.13 0 10. Passed 7-0."),
            _segment(2, 3_601.0, FLEET),
        ],
    }


def _map_reply(prompt: str) -> str:
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    owned = [s for s in source["segments"] if not s.get("overlap")]
    document = _empty()
    if "204877" in owned[0]["text"]:
        text = "Bill 204877 passed 7-0, amending Municipal Code 42.13.010"
        document["decisions"] = [{"text": text, "source_ids": [owned[0]["id"]]}]
    else:
        text = "Depot adds 243 vehicles"
        document["key_topics"] = [{"text": text, "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


@pytest.mark.parametrize("language", ["en", "vi"])
def test_long_gemini_run_passes_its_language_and_reports_detail_counts(language):
    coverage: list[dict] = []
    prompts: list[str] = []

    def transport(prompt, **kwargs):
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        assert kwargs["stage"] == "semantic_audit"
        prompts.append(prompt)
        changes = [
            _change(item["ref"], f"{item['ref']}.r")
            for item in semantic_payload(prompt)["items"]
            if any(t["text_ref"].endswith(".r") for t in item["text_refs"])
        ]
        return _patch(*changes, refs=semantic_refs(prompt))

    result = generate_notes_document(
        snapshot=_long_snapshot(),
        output_language=language,
        provider="antigravity",
        model="gemini-3.8-flash",
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )
    q, r = {"en": (Q, R), "vi": ("[số lượng chưa rõ]", "[tham chiếu chưa rõ]")}[language]
    assert result["key_topics"][0]["text"] == f"Depot adds {q} vehicles"
    assert result["decisions"][0]["text"] == f"Bill 204877 passed 7-0, amending Municipal Code {r}"
    assert result["confirmation_items"] == []
    counts = coverage[0]["semantic_audit"]["counts"]
    assert (counts["quantity_unclear"], counts["reference_unclear"]) == (1, 1)
    assert coverage[0]["semantic_audit"]["part_count"] == 2 and len(prompts) == 2
    assert all("243" not in json.dumps(semantic_payload(p)["items"]) for p in prompts)


@pytest.mark.parametrize(
    "label",
    [
        "Source excerpt (original language; output language: English):",
        "Trích nguyên văn (ngôn ngữ gốc; ngôn ngữ đầu ra: tiếng Việt):",
    ],
)
def test_labelled_literal_source_excerpts_are_never_marked_or_varied(label):
    source = _source(FLEET, "It amends Section 42.13.010 today.")
    quote = (
        f"Needs review: an unverified note was removed. {label} “{FLEET}” “{_texts_of(source)[1]}”"
    )
    document = _empty()
    document["confirmation_items"] = [{"text": quote, "source_ids": _ids(source)}]
    audit = SemanticAudit(source, nd.validate_document(document, source))
    assert _texts(audit) == {"i0.o": quote} and audit.quantity_marks == 0


def _texts_of(source: dict) -> list[str]:
    return [segment["text"] for segment in source["segments"]]
