"""Opt-in adjacent vote tally provenance for overviews. All transcripts are synthetic."""

import copy

import pytest

from asr_studio.notes_context import MAX_ADDED_PER_ITEM, link_antecedent_context

HDR1 = "Agenda item 1, Council Bill 4101."
HDR2 = "Agenda item 2, Council Bill 4102."
TALLY = "That is five unopposed."
CLOSE = "The bill passes."
CLAIM = "Both bills passed unanimously."


def _run(texts, cited, text=CLAIM, gaps=None, max_refs=200, **options):
    options.setdefault("strict", True)
    options.setdefault("complete_bindings", True)
    starts, t = [], 0.0
    for i in range(len(texts)):
        t += (gaps or {}).get(i, 10.0)
        starts.append(t)
    snap = {
        "segments": [
            {"id": f"{i:032x}", "start_seconds": s, "end_seconds": s + 1, "text": x}
            for i, (s, x) in enumerate(zip(starts, texts, strict=True))
        ]
    }
    doc = {
        "schema_version": 1,
        "overview": [{"text": text, "source_ids": [f"{i:032x}" for i in cited], "owner": None}],
        "key_topics": [],
        "decisions": [],
    }
    before = copy.deepcopy(doc)
    out, _ = link_antecedent_context(doc, snap, max_refs_per_item=max_refs, **options)
    assert doc == before
    return [int(r, 16) for r in out["overview"][0]["source_ids"]]


TWO = [HDR1, "Discussion.", TALLY, CLOSE, HDR2, "More.", "It was seven to zero.", CLOSE]


def test_links_adjacent_tally_for_each_record():
    assert _run(TWO, [0, 3, 4, 7]) == [0, 2, 3, 4, 6, 7]


def test_flag_off_and_non_strict_unchanged():
    assert _run(TWO, [0, 3, 4, 7], complete_bindings=False) == [0, 3, 4, 7]
    assert _run(TWO, [0, 3, 4, 7], strict=False) == [0, 3, 4, 7]


def test_tally_cited_already_or_no_claim_unchanged():
    assert _run(TWO, [0, 2, 3]) == [0, 2, 3]
    assert _run(TWO, [0, 3], text="The council discussed the bill.") == [0, 3]


@pytest.mark.parametrize(
    "texts,cited,gaps",
    [
        (
            ["Roster: Ames, Bell, Cole yes.", CLOSE],
            [1],
            None,
        ),  # no count inferred from names
        ([HDR1, "Five ayes, two nays.", CLOSE], [0, 2], None),  # opposed
        ([HDR1, "It was five to two.", CLOSE], [0, 2], None),
        ([HDR1, "Unanimous, one abstention.", CLOSE], [0, 2], None),
        ([HDR1, "Not unanimous.", CLOSE], [0, 2], None),  # negated
        ([HDR1, "It was not unopposed.", CLOSE], [0, 2], None),
        ([HDR1, "Nine in favor, three opposed.", CLOSE], [0, 2], None),
        ([HDR1, "Nine in favor not opposed, one abstention.", CLOSE], [0, 2], None),
        ([HDR1, "Unanimous.", CLOSE], [0, 2], {2: 121.0}),  # over 120 s
        (
            [
                HDR1,
                "Unanimous.",
                "Discussion goes on.",
            ],
            [0, 2],
            None,
        ),  # non-closing
        ([HDR1, HDR2, "Unanimous.", CLOSE], [0, 3], None),  # newer header intervenes
        ([HDR1, "Next agenda item. Unanimous.", CLOSE], [0, 2], None),  # new agenda
        (["Unanimous.", CLOSE], [1], None),  # no record proof
        ([HDR1, "Council Bill 9999 unanimous.", CLOSE], [0, 2], None),  # other record
        (
            [HDR1 + " Council Bill 4102.", "Unanimous.", CLOSE],
            [0, 2],
            None,
        ),  # ambiguous
        (
            [HDR1, "Council Bill 4101 unanimous.", CLOSE],
            [0, 2],
            None,
        ),  # tally is a header
    ],
)
def test_unsafe_candidates_skipped(texts, cited, gaps):
    assert _run(texts, cited, gaps=gaps) == cited


def test_caps_order_and_dedup():
    texts = [HDR1, TALLY, CLOSE]
    assert _run(texts, [0, 2], max_refs=2) == [0, 2]  # per-item reference cap
    assert _run(texts, [2, 0, 2]) == [0, 1, 2]  # transcript order, no duplicate
    assert MAX_ADDED_PER_ITEM == 12


def test_in_favor_not_opposed_is_a_tally():
    texts = [HDR1, "Nine in favor not opposed.", CLOSE]
    assert _run(texts, [0, 2]) == [0, 1, 2]
    assert _run([HDR1, "Nine in favor and unopposed.", CLOSE], [0, 2]) == [0, 1, 2]


def test_report_style_record_header_and_garbled_tally():
    texts = ["Report of the committee, Council Bill 4101 on zoning.", "Talk.", "Unopposed.", CLOSE]
    assert _run(texts, [0, 3]) == [0, 2, 3]


def _boundary_link(text, source_texts, cited):
    source = {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            {"id": str(i), "start_seconds": 10 * i, "end_seconds": 10 * i + 1, "text": t}
            for i, t in enumerate(source_texts)
        ],
    }
    sections = (
        "overview",
        "key_topics",
        "decisions",
        "open_questions",
        "action_items",
        "confirmation_items",
    )
    document = {"schema_version": 1, **{s: [] for s in sections}}
    document["overview"] = [{"text": text, "source_ids": cited}]
    result, _ = link_antecedent_context(
        document, source, strict=True, complete_bindings=True, max_refs_per_item=200
    )
    return result["overview"][0]["source_ids"]


def test_tally_before_closing_is_not_for_next_record_named_after_it():
    assert _boundary_link(
        "Unanimous passage of Council Bill 4102",
        [
            "Agenda item one Council Bill 4101 is read.",
            "Five in favor, unopposed.",
            "The bill passes. Please read the next Council Bill 4102.",
        ],
        ["2"],
    ) == ["2"]


def test_tally_record_must_match_a_record_explicitly_claimed_by_the_overview():
    assert _boundary_link(
        "Unanimous passage of Council Bill 4102",
        [
            "Agenda item one Council Bill 4101 is read.",
            "Five in favor, unopposed.",
            "The bill passes.",
        ],
        ["0", "2"],
    ) == ["0", "2"]


def test_a_record_named_in_its_own_closing_still_gets_its_tally():
    result = _boundary_link(
        "Unanimous passage of Council Bill 4101",
        [
            "Agenda item one Council Bill 4101 is read.",
            "Five in favor, unopposed.",
            "Council Bill 4101 passes.",
        ],
        ["2"],
    )
    assert "1" in result and "2" in result
