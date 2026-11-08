"""Synthetic source-binding guards: approximate scale wording, vote attribution, binding links."""

from __future__ import annotations

import json

import pytest

from asr_studio import notes_document as nd
from asr_studio.notes_context import link_antecedent_context, unverified_vote_names
from asr_studio.notes_semantic import SemanticAudit, mark_approximate_quantities

Q = "[quantity unclear]"
NEEDS_REVIEW = "Needs review: an unverified note was removed."


def _source(*texts: str) -> dict:
    return {
        "meeting_date": "2026-03-04",
        "speakers": [],
        "segments": [
            {
                "id": f"{i:032x}",
                "start_seconds": i * 4.0,
                "end_seconds": i * 4.0 + 3,
                "text": text,
                "speaker_name": "SPEAKER_00",
            }
            for i, text in enumerate(texts)
        ],
    }


def _sid(index: int) -> str:
    return f"{index:032x}"


def _doc(section: str, *items: dict) -> dict:
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document[section] = list(items)
    return document


# ----- approximate scale wording -------------------------------------------------------------

CITED = "The depot sits within a few hundred meters of the old canal lock."


@pytest.mark.parametrize(
    "claim",
    ["depot hundred meters from canal", "depot a hundred meters from canal", "depot 100 meters"],
)
def test_a_definite_count_of_a_vaguely_stated_scale_is_marked(claim):
    text, marks = mark_approximate_quantities(claim, CITED)
    assert marks == 1 and Q in text and "hundred" not in text and "100" not in text


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("depot a few hundred meters from canal", CITED),  # the qualifier is kept
        ("depot several hundred meters away", CITED),
        ("depot hundred visitors", CITED),  # another unit
        ("depot two hundred meters", CITED + " Later: two hundred meters exactly."),
        ("depot hundred meters", "The depot is one hundred meters from the lock."),
    ],
)
def test_qualified_other_unit_or_exact_source_counts_are_untouched(claim, cited):
    assert mark_approximate_quantities(claim, cited) == (claim, 0)


def test_the_audit_marks_the_patched_text_against_its_own_pool():
    source = _source(CITED, "Ms. Oduya: the depot plan.")
    document = nd.validate_document(
        _doc(
            "key_topics",
            {"text": "Oduya: depot hundred meters from canal", "source_ids": [_sid(0)]},
        ),
        source,
    )
    audit = SemanticAudit(source, document)
    patch = {
        "audit_version": 1,
        "changes": [
            {
                "ref": "i0",
                "text_ref": "i0.a1",
                "add_source_ids": [],
                "clear_owner": False,
                "clear_deadline": False,
            }
        ],
        "unresolved_refs": [],
    }
    published, counts = audit.parse(json.dumps(patch))
    assert published["key_topics"][0]["text"] == f"Depot {Q} meters from canal"
    assert counts["quantity_unclear"] == 1


# ----- vote attribution needs a cited local recognition ----------------------------------------

HANDOFF = "Thank you, Councilmember Ames. Councilmember Velto."
STATEMENT = "I will be voting no today because the study is unfinished."
ACK = "Thank you, Councilmember Velto. Any other comments?"


def _names(texts: tuple[str, ...], cited: list[int], claim: str) -> list[str]:
    source = _source(*texts)
    segments = source["segments"]
    position = {segment["id"]: i for i, segment in enumerate(segments)}
    item = {"text": claim, "source_ids": [_sid(i) for i in cited]}
    return unverified_vote_names(item, segments, position)


@pytest.mark.parametrize(
    ("texts", "cited"),
    [
        ((HANDOFF, STATEMENT, "More reasons follow."), [0, 1]),  # cited handoff before the turn
        (("Opening remarks.", STATEMENT, ACK), [1, 2]),  # cited acknowledgment after it
        (("Opening.", "Velto will be voting no on the bill."), [1]),  # third-person report
    ],
)
def test_a_cited_recognition_or_report_verifies_the_named_vote(texts, cited):
    assert _names(texts, cited, "Bill pending; Velto voting no") == []


@pytest.mark.parametrize(
    ("texts", "cited"),
    [
        ((HANDOFF, STATEMENT), [1]),  # the handoff exists only in the full source
        (("Opening remarks.", STATEMENT, ACK), [1]),  # the acknowledgment is uncited
        ((STATEMENT, "I really thank Councilmember Velto for the work."), [0, 1]),  # thanks
        (("I thank Chair Velto; I will vote no.",), [0]),  # co-occurrence is no binding
        (
            ("Councilmember Velto.", "Thank you. Ames has the floor.", STATEMENT),
            [0, 2],
        ),  # turn ended
    ],
)
def test_uncited_thanks_or_cooccurring_names_never_verify_a_vote(texts, cited):
    assert _names(texts, cited, "Bill pending; Velto voting no") == ["Velto"]


def test_each_named_voter_needs_its_own_recognition():
    texts = (HANDOFF, STATEMENT, ACK, "I will be voting yes on this.", "Thanks, all.")
    assert _names(texts, [0, 1, 3], "Velto voting no, Ames voting yes") == ["Ames"]


def test_no_cited_first_person_statement_is_out_of_scope():
    assert _names(("The chair opened the vote.",), [0], "Velto voting no") == []


def test_an_unverified_vote_attribution_is_quarantined_as_cited_excerpts():
    source = _source(HANDOFF, STATEMENT, "Unrelated topic.")
    item = {"text": "Bill pending; Velto voting no", "source_ids": [_sid(1)]}
    kept = {"text": "Unrelated topic", "source_ids": [_sid(2)]}
    document = nd.validate_document(_doc("key_topics", item, kept), source)
    keep = json.dumps({"audit_version": 1, "changes": [], "unresolved_refs": []})
    published, counts = SemanticAudit(source, document).parse(keep)
    assert [i["text"] for i in published["key_topics"]] == ["Unrelated topic"]
    quarantined = published["confirmation_items"][-1]
    assert quarantined["text"].startswith(NEEDS_REVIEW) and "Velto" not in quarantined["text"]
    assert quarantined["source_ids"] == [_sid(1)] and counts["quarantined"] == 1


# ----- strict binding links --------------------------------------------------------------------


def _linked(texts: tuple[str, ...], cited: list[int], claim: str, *, strict: bool = True):
    source = _source(*texts)
    document = _doc("key_topics", {"text": claim, "source_ids": [_sid(i) for i in cited]})
    linked, _ = link_antecedent_context(document, source, max_refs_per_item=200, strict=strict)
    ids = linked["key_topics"][0]["source_ids"]
    return [int(ref, 16) for ref in ids if ref not in document["key_topics"][0]["source_ids"]]


CUT = (
    "The agency will review the lantern archive and",
    "then catalogue the glassware and draft revised harbour guidelines.",
    "Other business.",
)


def test_a_cut_sentence_links_the_continuation_that_states_missing_item_words():
    claim = "Agency to review lantern archive, catalogue glassware, revise harbour guidelines"
    assert _linked(CUT, [0], claim) == [1]
    assert _linked(CUT, [0], claim, strict=False) == []


def test_a_continuation_without_missing_item_words_or_after_a_full_stop_is_not_linked():
    assert _linked(CUT, [0], "Agency to review lantern archive") == []
    finished = ("The agency will review the lantern archive.", CUT[1])
    assert _linked(finished, [0], "Agency reviews lantern archive and glassware") == []


def test_a_named_turn_links_its_adjacent_floor_handoff_only():
    turn = ("Thank you, Councilman Ames. Customer Velta.", "I remember the old harbour fight.")
    assert _linked(turn, [1], "Councilmember Velto recalls harbour fight") == [0]
    acknowledged = ("Thank you for that.", "I remember the old harbour fight.")
    assert _linked(acknowledged, [1], "Councilmember Velto recalls harbour fight") == []
    assert _linked(turn, [1], "Councilmember Velto recalls harbour fight", strict=False) == []


@pytest.mark.parametrize(
    "claim", ["Depot hundred meters", "Depot a hundred meters", "Depot 100 meters"]
)
def test_a_different_exact_count_does_not_resolve_the_approximate_count(claim):
    text, marks = mark_approximate_quantities(claim, CITED + " Later: two hundred meters exactly.")
    assert marks == 1 and Q in text


@pytest.mark.parametrize("claim", ["Depot hundred meters", "Depot 100 meters"])
def test_an_approximate_numeric_count_is_not_an_exact_override(claim):
    text, marks = mark_approximate_quantities(claim, CITED + " Later: about 100 meters.")
    assert marks == 1 and Q in text


def test_an_exact_numeric_count_supports_its_word_form():
    assert mark_approximate_quantities("Depot hundred meters", CITED + " Exactly 100 meters.") == (
        "Depot hundred meters",
        0,
    )


def test_an_already_qualified_numeric_count_is_preserved():
    assert mark_approximate_quantities("Depot about 100 meters", CITED) == (
        "Depot about 100 meters",
        0,
    )


@pytest.mark.parametrize("gap,expected", [(120, True), (121, False)])
def test_cut_sentence_continuations_respect_the_forward_time_window(gap, expected):
    source = _source("The agency requires a review of the", "regional habitat and access plan.")
    source["segments"][1]["start_seconds"] = source["segments"][0]["end_seconds"] + gap
    source["segments"][1]["end_seconds"] = source["segments"][1]["start_seconds"] + 1
    document = _doc(
        "key_topics",
        {"text": "Agency requires regional habitat and access review", "source_ids": [_sid(0)]},
    )
    linked, _ = link_antecedent_context(document, source, max_refs_per_item=24, strict=True)
    assert (_sid(1) in linked["key_topics"][0]["source_ids"]) is expected


@pytest.mark.parametrize(
    "ack",
    [
        "Thanks to Chair Velto for supporting the project.",
        "Thank you to Chair Velto for supporting the project.",
        "Thank you for Chair Velto's work on this.",
        "Thanks To Chair Velto.",
        "Thank you, Councilmember Ames. Any other comments?",
    ],
)
def test_a_third_party_thanks_does_not_recognize_the_voter(ack):
    assert _names((STATEMENT, ack), [0, 1], "Bill pending; Velto voting no") == ["Velto"]


@pytest.mark.parametrize(
    "ack",
    [
        "Thank you, Councilmember Velto. Any other comments?",
        "Thanks, Velto.",
        "Thank you Councilmember Velto.",
        "Thank you, Chair Velto, that concludes this item.",
    ],
)
def test_only_the_direct_acknowledgment_address_recognizes_the_voter(ack):
    assert _names((STATEMENT, ack), [0, 1], "Bill pending; Velto voting no") == []


@pytest.mark.parametrize("gap,verified", [(180, True), (181, False)])
def test_voter_handoffs_respect_the_backward_time_window(gap, verified):
    source = _source(HANDOFF, STATEMENT)
    source["segments"][1].update(start_seconds=gap, end_seconds=gap + 1)
    segments = source["segments"]
    names = unverified_vote_names(
        {"text": "Velto voting no", "source_ids": [_sid(0), _sid(1)]},
        segments,
        {s["id"]: i for i, s in enumerate(segments)},
    )
    assert names == ([] if verified else ["Velto"])


@pytest.mark.parametrize("distance,verified", [(4, True), (5, False)])
def test_voter_acknowledgments_respect_the_segment_window(distance, verified):
    texts = (STATEMENT, *["The speaker continues."] * (distance - 1), ACK)
    assert _names(texts, [0, distance], "Velto voting no") == ([] if verified else ["Velto"])


@pytest.mark.parametrize("gap,verified", [(120, True), (121, False)])
def test_voter_acknowledgments_respect_the_time_window(gap, verified):
    source = _source(STATEMENT, ACK)
    source["segments"][1].update(start_seconds=3 + gap, end_seconds=4 + gap)
    segments = source["segments"]
    names = unverified_vote_names(
        {"text": "Velto voting no", "source_ids": [_sid(0), _sid(1)]},
        segments,
        {s["id"]: i for i, s in enumerate(segments)},
    )
    assert names == ([] if verified else ["Velto"])


@pytest.mark.parametrize("distance,linked", [(4, True), (5, False)])
def test_cut_sentence_continuations_respect_the_segment_window(distance, linked):
    source = _source(
        "The agency requires review of the",
        *["continuing discussion"] * (distance - 1),
        "regional habitat plan.",
    )
    document = _doc(
        "key_topics", {"text": "Agency requires regional habitat review", "source_ids": [_sid(0)]}
    )
    result, _ = link_antecedent_context(document, source, max_refs_per_item=24, strict=True)
    assert (_sid(distance) in result["key_topics"][0]["source_ids"]) is linked


def test_default_context_does_not_enable_strict_continuation_links():
    source = _source("The agency requires review of the", "regional habitat plan.")
    document = _doc(
        "key_topics", {"text": "Agency requires regional habitat review", "source_ids": [_sid(0)]}
    )
    result, _ = link_antecedent_context(document, source, max_refs_per_item=24)
    assert result == document
