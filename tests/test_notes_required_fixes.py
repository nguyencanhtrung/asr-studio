"""R1 revised prose keeps ambiguity marks; R2 every listed record of a group is bound or reviewed.

All transcripts are synthetic.
"""

from __future__ import annotations

import json

import pytest
from notes_helpers import semantic_keep_patch, semantic_patch

from asr_studio import notes_context as nc
from asr_studio import notes_document as nd
from asr_studio.notes_context import link_antecedent_context
from asr_studio.notes_semantic import SemanticAudit

EMPTY = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
MARKERS = {"en": "[quantity unclear]", "vi": "[số lượng chưa rõ]"}


def _sid(i: int) -> str:
    return f"{i:032x}"


def _seg(i: int, text: str, gap: float = 10.0) -> dict:
    return {
        "id": _sid(i),
        "start_seconds": i * gap,
        "end_seconds": i * gap + 1,
        "text": text,
        "speaker_name": "SPEAKER_00",
    }


# ----- R1 ------------------------------------------------------------------------------------

AMBIGUOUS = "We bought 243, four and five-seat vehicles for the depot."
TOTAL = "The fleet total is 243 vehicles."


def _fleet(language: str = "en", extra: str | None = None) -> SemanticAudit:
    segments = [_seg(1, AMBIGUOUS)] + ([_seg(2, extra)] if extra else [])
    source = {"meeting_date": None, "speakers": [], "segments": segments}
    document = json.loads(json.dumps(EMPTY))
    document["key_topics"] = [{"text": "Depot adds 243 electric vehicles", "source_ids": [_sid(1)]}]
    return SemanticAudit(source, document, output_language=language, allow_revisions=True)


def _revise(audit: SemanticAudit, text: str, ids=(), language: str = "en"):
    change = {
        "ref": "i0",
        "text_ref": "i0.o",
        "add_source_ids": list(ids),
        "clear_owner": False,
        "clear_deadline": False,
        "revised_text": text,
    }
    return audit.parse(semantic_patch(["i0"], [change]), output_language=language)


@pytest.mark.parametrize("language", ["en", "vi"])
def test_revised_prose_cannot_restore_the_ambiguous_count(language):
    marker = MARKERS[language]
    published, counts = _revise(
        _fleet(language), "Depot adds 243 electric vehicles", language=language
    )
    assert published["key_topics"][0]["text"] == f"Depot adds {marker} electric vehicles"
    assert counts["model_revised"] == 1
    assert counts["model_verified"] == 0
    assert counts["quantity_unclear"] == 2  # applied events: the constructor's and the re-mark


def test_a_revision_that_keeps_the_marker_is_not_marked_twice():
    audit = _fleet()
    text = f"Depot adds {MARKERS['en']} electric vehicles, new fleet"
    published, counts = _revise(audit, text)
    assert published["key_topics"][0]["text"] == text
    assert counts["quantity_unclear"] == 1


def test_an_unrevised_item_is_counted_once():
    audit = _fleet()
    _published, counts = audit.parse(semantic_keep_patch(audit))
    assert counts["quantity_unclear"] == 1 and counts["model_revised"] == 0


def test_an_added_valid_independent_total_can_justify_the_digits():
    audit = _fleet(extra=TOTAL)
    published, counts = _revise(audit, "Depot adds 243 electric vehicles", ids=[_sid(2)])
    assert published["key_topics"][0]["text"] == "Depot adds 243 electric vehicles"
    assert counts["quantity_unclear"] == 1  # only the constructor's mark; no re-mark


def test_a_total_that_is_not_added_does_not_justify_the_digits():
    audit = _fleet(extra=TOTAL)
    published, _ = _revise(audit, "Depot adds 243 electric vehicles")
    assert "243" not in published["key_topics"][0]["text"]


# ----- R2 ------------------------------------------------------------------------------------

TEXT = "Passage land use, zoning Council Bills 119505, 119506, 119489"
FILLER = "The speaker continues on the general budget."


def _pool(headers=(119505, 119506, 119489), gap=10.0, between=None):
    """header, cited filler, per record; `between` is an extra segment before the last cited."""
    texts = []
    for number in headers:
        texts += [f"Agenda item. Council Bill {number} is next." if number else FILLER, FILLER]
    if between:
        texts.insert(len(texts) - 1, between)
    segments = [_seg(i, t, gap) for i, t in enumerate(texts)]
    cited = [i for i, t in enumerate(texts) if t == FILLER]
    return {"segments": segments}, cited


def _link(snapshot, cited, text=TEXT, max_refs=200, section="overview"):
    document = {"schema_version": 1, "overview": [], "key_topics": [], "decisions": []}
    document[section] = [{"text": text, "source_ids": [_sid(i) for i in cited]}]
    linked, _ = link_antecedent_context(
        document, snapshot, max_refs_per_item=max_refs, strict=True, complete_bindings=True
    )
    item = linked[section][0]
    assert item["text"] == text
    return item, {int(i, 16) for i in item["source_ids"]}


def _unbound(snapshot, item):
    position = {s["id"]: i for i, s in enumerate(snapshot["segments"])}
    return nc.unbound_record_group(item, snapshot["segments"], position)


HEADERS = {0, 2, 4}


@pytest.mark.parametrize(
    "text",
    [
        TEXT,
        "Passage of Council Bill 119505, Council Bill 119506 and Council Bill 119489",
        "Passage of Council Bills 119505 & 119506 and 119489",
        "Passage of Council Bills 119505, and 119506, or 119489",
    ],
)
@pytest.mark.parametrize("section", ["overview", "decisions"])
def test_every_listed_record_links_its_own_header(text, section):
    snapshot, cited = _pool()
    item, ids = _link(snapshot, cited, text, section=section)
    assert ids == set(cited) | HEADERS
    assert not _unbound(snapshot, item)


@pytest.mark.parametrize(
    "case",
    ["missing", "hidden", "agenda_cross", "time_gap", "cap"],
)
def test_an_unbound_member_is_flagged_never_silently_partial(case):
    kwargs = {}
    if case == "missing":
        snapshot, cited = _pool(headers=(119505, 119506, 0))
    elif case == "hidden":  # the third header only restates another agenda
        snapshot, cited = _pool(headers=(119505, 119506, 119999))
    elif case == "agenda_cross":  # a different agenda header sits between header and run
        snapshot, cited = _pool(between="Agenda item. Council Bill 119777 is next.")
        assert cited
    elif case == "time_gap":
        snapshot, cited = _pool(gap=1000.0)
    else:
        snapshot, cited = _pool()
        kwargs["max_refs"] = len(cited) + 2  # only two complete links fit
    item, ids = _link(snapshot, cited, **kwargs)
    assert set(item["source_ids"]) >= {_sid(i) for i in cited}  # old IDs kept
    assert _unbound(snapshot, item)


def test_a_direct_typed_list_in_the_cited_pool_proves_every_member():
    snapshot = {"segments": [_seg(0, "The agenda lists Council Bills 119505, 119506 and 119489.")]}
    item, ids = _link(snapshot, [0])
    assert ids == {0} and not _unbound(snapshot, item)


@pytest.mark.parametrize(
    ("text", "members"),
    [
        ("Council Bill 119505 dated 2026", {"119505"}),  # an explicit date word
        ("Council Bill 119505 (2026)", {"119505"}),
        ("Council Bill 119505 of 2026", {"119505"}),
        ("Council Bills 119505 through 119507", {"119505"}),  # a range
        ("Council Bill 119505-119507", {"119505"}),
        ("Council Bill 119505 and Resolution 31800", {"119505", "31800"}),
        ("Council Bills 119505 and 119506", {"119505", "119506"}),
    ],
)
def test_ranges_dates_and_unrelated_numbers_are_not_expanded(text, members):
    assert {number for _f, number, _l in nc._record_members(text)} == members


def test_a_single_unbound_record_is_not_a_group_and_is_unchanged():
    snapshot, cited = _pool(headers=(119505, 0, 0))
    item, _ids = _link(snapshot, cited, "Passage of Council Bill 119506")
    assert not _unbound(snapshot, item)


def test_default_linking_does_not_change_without_complete_bindings():
    snapshot, cited = _pool()
    document = {
        "schema_version": 1,
        "overview": [{"text": TEXT, "source_ids": [_sid(i) for i in cited]}],
    }
    linked, _ = link_antecedent_context(document, snapshot, max_refs_per_item=200, strict=True)
    assert linked == document or {int(i, 16) for i in linked["overview"][0]["source_ids"]} == set(
        cited
    )


def _audit(snapshot, cited, max_refs=None) -> SemanticAudit:
    document = json.loads(json.dumps(EMPTY))
    document["overview"] = [{"text": TEXT, "source_ids": [_sid(i) for i in cited]}]
    document["key_topics"] = [{"text": "Budget talk", "source_ids": [_sid(cited[0])]}]
    source = {"meeting_date": None, "speakers": [], **snapshot}
    return SemanticAudit(source, document)


def _published(audit):
    return audit.parse(semantic_keep_patch(audit))


def test_audit_completes_a_recoverable_group_before_any_quarantine():
    snapshot, cited = _pool()  # only the filler is cited; every header is a visible candidate
    audit = _audit(snapshot, cited)
    published, counts = _published(audit)
    item = published["overview"][0]
    assert item["text"] == TEXT and counts["quarantined"] == 0
    assert {int(i, 16) for i in item["source_ids"]} == set(cited) | HEADERS
    assert counts["added_ids"] == 0  # raw model counts untouched


@pytest.mark.parametrize("headers", [(119505, 119506, 0), (119505, 119506, 119999)])
def test_audit_quarantines_a_group_that_stays_unbound(headers):
    snapshot, cited = _pool(headers=headers)
    audit = _audit(snapshot, cited)
    published, counts = _published(audit)
    assert published["overview"] == [] and counts["quarantined"] == 1
    assert all(TEXT not in i["text"] for i in published["confirmation_items"])


def test_audit_out_of_scope_or_over_cap_member_is_quarantined():
    snapshot, cited = _pool(gap=1000.0)
    audit = _audit(snapshot, cited)
    assert _published(audit)[1]["quarantined"] == 1


def test_a_wrong_family_record_does_not_prove_a_bill():
    snapshot = {
        "segments": [_seg(0, "Resolution 119506 and Council Bills 119505 and 119489 pass.")]
    }
    item = {"text": TEXT, "source_ids": [_sid(0)]}
    assert _unbound(snapshot, item)


def test_an_own_cited_bare_group_proves_every_member():
    snapshot = {"segments": [_seg(0, "Council Bills 119505, 119506 & 119489 pass.")]}
    assert not _unbound(snapshot, {"text": TEXT, "source_ids": [_sid(0)]})


@pytest.mark.parametrize(
    ("text", "members"),
    [
        ("Council Bills 999 and 1000", {"999", "1000"}),  # varied length
        ("Council Bills 7050, 2026", {"7050", "2026"}),  # a year-like value is a member
        ("Council Bills 9401 and Council Bills 9402", {"9401", "9402"}),  # repeated plural
        ("Council Bill 9401, Council Bills 9402 & 9403", {"9401", "9402", "9403"}),
    ],
)
def test_syntax_not_digit_length_or_year_value_decides_members(text, members):
    assert {n for _f, n, _l in nc._record_members(text)} == members


# ----- R2 edges: mixed families, plain quantities, compact labels, combine visibility --------


def _keep(audit: SemanticAudit) -> dict:
    return {
        "audit_version": 2,
        "verified": {i.ref: True for i in audit.items},
        "changes": [],
        "unresolved_refs": [],
    }


def _decisions(items: list[dict]) -> dict:
    document = json.loads(json.dumps(EMPTY))
    document["decisions"] = items
    return document


def _keep_audit(segments: list[dict], items: list[dict]):
    source = {"meeting_date": None, "segments": segments}
    audit = SemanticAudit(source, _decisions(items), allow_revisions=True)
    return audit.parse(json.dumps(_keep(audit)))


def test_mixed_family_group_with_one_unbound_member_is_quarantined():
    segments = [
        _seg(1, "Agenda item 1. Council Bill 9401 passed."),
        _seg(2, "Agenda item 2. Ordinance 7402 passed."),
    ]
    item = {"text": "Council Bill 9401 and Ordinance 7402 passed.", "source_ids": [_sid(1)]}
    out, counts = _keep_audit(segments, [item])
    assert out["decisions"] == [] and counts["quarantined"] == 1


def test_later_bare_member_inherits_the_latest_explicit_family():
    members = nc._record_groups("Bill 9401, Ordinance 7402 and 7403")[0]
    assert [(f, n) for f, n, _l in members] == [
        ("bill", "9401"),
        ("ordinance", "7402"),
        ("ordinance", "7403"),
    ]


def test_same_value_in_two_families_is_two_records():
    snapshot = {"segments": [_seg(0, "Council Bill 9401 passed.")]}
    item = {"text": "Council Bill 9401 and Ordinance 9401 passed.", "source_ids": [_sid(0)]}
    assert _unbound(snapshot, item)


def test_plain_quantity_is_not_record_proof():
    segments = [
        _seg(1, "Agenda item 1. Council Bill 9401 passed."),
        _seg(2, "9402 residents attended the hearing."),
    ]
    item = {"text": "Council Bills 9401 and 9402 passed.", "source_ids": [_sid(1), _sid(2)]}
    out, counts = _keep_audit(segments, [item])
    assert out["decisions"] == [] and counts["quarantined"] == 1


def test_typed_header_proves_a_bare_member():
    snapshot = {
        "segments": [
            _seg(0, "Agenda item 1. Council Bill 119505 passed."),
            _seg(1, "Agenda item 2. 119506."),
        ]
    }
    item = {"text": "Council Bills 119505 and 119506 passed.", "source_ids": [_sid(0), _sid(1)]}
    assert not _unbound(snapshot, item)


@pytest.mark.parametrize("label", ["CB9401, CB9402", "Bill9401 and Bill9402", "CB 9401 and 9402"])
def test_compact_explicit_labels_are_group_members(label):
    assert {n for _f, n, _l in nc._record_members(f"{label} passed.")} == {"9401", "9402"}
    snapshot = {"segments": [_seg(0, "Council Bill 9401 passed. The bill is law.")]}
    item = {"text": f"{label} passed. The bill is law.", "source_ids": [_sid(0)]}
    assert _unbound(snapshot, item)


@pytest.mark.parametrize("text", ["XCB9401 and 9402", "Bill 9401 dated 2026", "Bill 9401 to 9403"])
def test_compact_label_negative_cases_make_no_group(text):
    assert len(nc._record_members(text)) < 2


def test_combine_keeps_the_part_view_visibility():
    texts = (
        ["Agenda item 1. Council Bill 9401."]
        + ["Background."] * 4
        + ["Bill passed."]
        + ["Background."] * 4
        + ["Agenda item 2. Council Bill 9403."]
        + ["Background."] * 4
        + ["Bill passed.", "Closing."]
    )
    segments = [_seg(i + 1, t) for i, t in enumerate(texts)]
    group = "Council Bills 9401 and 9403 passed."
    items = [
        {"text": group, "source_ids": [_sid(1), _sid(6)]},
        {"text": "Closing remarks.", "source_ids": [_sid(16)]},
        {"text": "Council Bill 9403 passed.", "source_ids": [_sid(11), _sid(16)]},
        *({"text": "Background.", "source_ids": [_sid(8)]} for _ in range(6)),
    ]
    audit = SemanticAudit(
        {"meeting_date": None, "segments": segments}, _decisions(items), allow_revisions=True
    )
    views = audit.partitions()
    patch = _keep(views[0])
    patch["verified"]["i0"] = False
    patch["changes"] = [
        {
            "ref": "i0",
            "text_ref": "i0.o",
            "add_source_ids": [_sid(16)],
            "clear_owner": False,
            "clear_deadline": False,
            "revised_text": None,
        }
    ]
    assert _sid(11) not in views[0]._visible and _sid(11) in audit._visible
    parts = [(views[0], patch), *((v, _keep(v)) for v in views[1:])]
    combined, counts = audit.combine_patches(parts)
    assert all(i["text"] != group for i in combined["decisions"]) and counts["quarantined"] >= 1


def test_repeated_six_digit_value_typed_as_another_family_is_not_a_bill():
    snapshot = {
        "segments": [
            _seg(0, "Agenda Item 1. Council Bill 119505 passed."),
            _seg(1, "Agenda Item 2. Ordinance 119506, 119506 passed."),
        ]
    }
    item = {"text": "Council Bills 119505 and 119506 passed.", "source_ids": [_sid(0), _sid(1)]}
    assert _unbound(snapshot, item)
    # The correct family also typed for that value proves it.
    snapshot["segments"][1]["text"] = "Agenda Item 2. Ordinance 119506, Council Bill 119506 passed."
    assert not _unbound(snapshot, item)


# ----- R2 short members + completion branch --------------------------------------------------

SHORT = "Passage of Council Bills 9401 and 14"
PAD = "Discussion continued."


def test_absent_short_group_member_is_quarantined():
    segments = [_seg(1, "Agenda item 1. Council Bill 9401 passed.")]
    out, counts = _keep_audit(segments, [{"text": SHORT, "source_ids": [_sid(1)]}])
    assert out["decisions"] == [] and counts["quarantined"] == 1


def test_cited_typed_short_group_member_is_published():
    segments = [_seg(1, "Agenda item 1. Council Bill 9401 and Council Bill 14 passed.")]
    out, counts = _keep_audit(segments, [{"text": SHORT, "source_ids": [_sid(1)]}])
    assert len(out["decisions"]) == 1 and counts["quarantined"] == 0


def test_recoverable_header_links_short_group_member_then_publishes():
    texts = [
        "Agenda item. Council Bill 9401 is next.",
        PAD,
        "Agenda item. Council Bill 14 is next.",
        PAD,
    ]
    segments = [_seg(i, t) for i, t in enumerate(texts)]
    item = {"text": SHORT, "source_ids": [_sid(1), _sid(3)]}
    assert _unbound({"segments": segments}, item)
    out, counts = _keep_audit(segments, [item])
    assert len(out["decisions"]) == 1 and counts["quarantined"] == 0
    assert _sid(2) in out["decisions"][0]["source_ids"]


def test_plain_quantity_with_member_number_gets_typed_header_and_group_is_kept():
    segments = [
        _seg(0, "Agenda item. Council Bill 119505 is next."),
        _seg(1, PAD),
        _seg(2, "Agenda item. Council Bill 119506 is next."),
        _seg(3, "119506 residents spoke."),
    ]
    item = {"text": "Council Bills 119505 and 119506 passed.", "source_ids": [_sid(1), _sid(3)]}
    assert {2} in nc._completion_links(item, [1, 3], segments, overview=False)
    out, counts = _keep_audit(segments, [item])
    assert len(out["decisions"]) == 1 and counts["quarantined"] == 0
    assert _sid(2) in out["decisions"][0]["source_ids"]
