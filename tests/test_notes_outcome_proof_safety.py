"""A record-outcome core is proved only by an asserted outcome of that one record."""

from __future__ import annotations

import pytest

from asr_studio.notes_context import _header_states_record
from asr_studio.notes_outcomes import outcome_core_candidate

HEADER = "Agenda item 1 Council Bill 74010 relating to the harbor fund."


def _segments(*texts: str) -> list[dict]:
    return [
        {
            "id": f"{i + 1:032x}",
            "start_seconds": i * 4.0,
            "end_seconds": i * 4.0 + 1,
            "text": text,
            "speaker_name": "SPEAKER_00",
        }
        for i, text in enumerate(texts)
    ]


def _core(text: str, *texts: str):
    segments = _segments(*texts)
    return outcome_core_candidate(text, segments, range(len(segments)))


# ----- the outcome sentence names only this record ------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "The bill passed: Council Bill 74011.",
        "The bill passed, together with Council Bill 74011.",
        "The bill passed as Bill 74011.",
    ],
)
def test_another_record_in_the_outcome_sentence_blocks(outcome):
    assert _core("Council Bill 74010 passed", HEADER, outcome) is None


def test_a_different_next_agenda_in_a_new_sentence_does_not_block():
    after = "The bill passed. Agenda item 2 Council Bill 74011 relating to parks."
    assert _core("Council Bill 74010 passed", HEADER, after) == (
        "Council Bill 74010 passed",
        (0, 1),
    )
    one_segment = f"{HEADER} The bill passed. Agenda item 2 Council Bill 74011."
    assert _core("Council Bill 74010 passed", one_segment) == ("Council Bill 74010 passed", (0,))


# ----- only an asserted outcome proves it ---------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "Has the bill passed?",
        "The bill passed?",
        "Has the bill passed",  # a question without its mark
        "Somebody claimed the bill passed, but nobody knows whether it did.",
        "I think the bill passed.",
        "Reportedly the bill passed.",
        "The bill passed, but it did not take effect.",
        "The bill passed if the mayor signs it.",
        "The bill passed, I mean it will pass next week.",
        "We hope the bill passed.",
        "The bill would be passed.",
        "The bill passed, but nobody knows whether it did.",
        "The bill passed, reportedly.",
    ],
)
def test_question_doubt_or_condition_is_not_an_outcome(outcome):
    assert _core("Council Bill 74010 passed", HEADER, outcome) is None


@pytest.mark.parametrize(
    "outcome",
    [
        "The bill passed.",
        "Bill passed and the chair will sign it.",
        "Okay, the bill passed.",
        "Council Bill 74010 passed.",
    ],
)
def test_plain_asserted_outcome_proves_it(outcome):
    assert _core("Council Bill 74010 passed", HEADER, outcome) == (
        "Council Bill 74010 passed",
        (0, 1),
    )


def test_a_motion_carrying_then_the_record_outcome_proves_it():
    texts = (
        "Agenda item 2 Appointment 5520 of a board member.",
        "The motion carries, an appointment is confirmed, and we suspend the rules.",
    )
    assert _core("confirmed Appointment 5520", *texts) == ("confirmed Appointment 5520", (0, 1))


# ----- a tally belongs to the outcome's own vote --------------------------------------------


@pytest.mark.parametrize(
    "vote",
    [
        ("The amendment passed, eight in favor unopposed.", "The bill passed."),
        ("On the amendment, eight in favor unopposed.", "The bill passed."),
        ("The amendment passed, eight in favor unopposed, and the bill passed.",),
        ("Eight in favor unopposed.", "The amendment is adopted.", "The bill passed."),
    ],
)
def test_another_vote_tally_is_never_the_record_tally(vote):
    assert _core("Council Bill 74010 passed 8-0", HEADER, *vote) == (
        "Council Bill 74010 passed",
        (0, len(vote)),
    )


@pytest.mark.parametrize(
    ("vote", "tally_at"),
    [
        (("Eight in favor, unopposed.", "The bill passed."), 1),
        (("The bill passed eight in favor, none opposed.",), 1),
        (("Eight in favor not opposed. The bill passed.",), 1),
    ],
)
def test_the_adjacent_undifferentiated_tally_joins(vote, tally_at):
    core = _core("Council Bill 74010 passed 8-0", HEADER, *vote)
    assert core == ("Council Bill 74010 passed 8-0", tuple(sorted({0, tally_at, len(vote)})))


# ----- an ambiguous primary header is no record statement -----------------------------------


@pytest.mark.parametrize(
    ("header", "family", "number"),
    [
        ("Agenda Item 1 Appointments 7401 and 7402", "appointment", "7401"),
        ("Agenda item 3 Bills 312 through 315", "bill", "312"),
        ("Agenda item 3 Bill 312-313 relating to parks", "bill", "312"),
        ("Agenda item 4 Resolution 4410, 4411", "resolution", "4410"),
        ("Agenda item 5 Council Bill 74010 and 74011", "bill", "74010"),
        ("Agenda item 6 Appointment 2025, 2026", "appointment", "2025"),
        # A year-like unlabelled sibling is a list too, whatever the record number's length.
        ("Agenda item 1 Council Bill 74010, 2026 session", "bill", "74010"),
    ],
)
def test_unlabelled_same_family_list_is_ambiguous(header, family, number):
    assert not _header_states_record(family, number, header)
    verb = {"appointment": "confirmed", "resolution": "adopted"}.get(family, "passed")
    noun = {"bill": "bill"}.get(family, family)
    item = f"{family.title()} {number} {verb}"
    article = "An" if noun[0] in "aeiou" else "The"
    assert _core(item, header, f"{article} {noun} is {verb}.") is None


@pytest.mark.parametrize(
    ("header", "family", "number"),
    [
        ("Agenda item 1 Council Bill 74010 of 2026", "bill", "74010"),
        ("Agenda item 1 Council Bill 74010 (2026)", "bill", "74010"),
        ("Agenda item 1 Council Bill 74010, adopted in 2026", "bill", "74010"),
        ("Agenda item 1 Council Bill 119 538 amending Resolution 3201 and 3202", "bill", "119538"),
        ("Agenda item 1 Appointment 1 345 of a director", "appointment", "1345"),
        ("Agenda item 1 Bill 74010 dated March 3, 2026", "bill", "74010"),
    ],
)
def test_secondary_references_dates_and_spaced_groups_are_not_lists(header, family, number):
    assert _header_states_record(family, number, header)


def test_a_direct_later_identity_and_outcome_still_proves_it():
    header = "Agenda Item 1 Appointments 7401 and 7402"
    assert _core("Appointment 7401 confirmed", header, "Appointment 7401 is confirmed.") == (
        "Appointment 7401 confirmed",
        (1,),
    )


# ----- harmless label and date words in the item --------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        "passed Council Bill No. 74010",
        "Council Bill 74010 amending the Comprehensive Plan passed",
        "Council Bill 74010 passed on May 3",
        "passed Council Bill 74010 in May",
    ],
)
def test_labels_and_dates_do_not_block_a_proven_core(item):
    core = _core(item, HEADER, "The bill passed.")
    assert core is not None and core[1] == (0, 1)
    assert "Plan" not in core[0] and "May" not in core[0]  # never part of the core


@pytest.mark.parametrize(
    "item",
    [
        "Council Bill 74010 no longer passed",
        "Council Bill 74010 may be passed",
        "Council Bill 74010 is planned to be passed",
        "Council Bill 74010 passed only if amended",
        "Council Bill 74010 passed?",
        "Council Bill 74010 will be passed in May",
    ],
)
def test_genuine_negation_modality_condition_or_question_still_blocks(item):
    assert _core(item, HEADER, "The bill passed.") is None
