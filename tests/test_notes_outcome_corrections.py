"""A record-outcome core is this meeting's final outcome: never corrected, preliminary or listed."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_patch

from asr_studio import notes_document as nd
from asr_studio.notes_context import _header_states_record
from asr_studio.notes_outcomes import outcome_core_candidate
from asr_studio.notes_semantic import SemanticAudit

HEADER = "Agenda item 1 Council Bill 74010 relating to the harbor fund."
CORE = ("Council Bill 74010 passed", (0, 1))


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


# ----- A: a later correction or deferral in the same agenda withdraws the outcome -----------


@pytest.mark.parametrize(
    "after",
    [
        ("The bill passed.", "Sorry, I misspoke, the bill was held over."),
        ("The bill passed. I misspoke, it was referred back to committee.",),
        ("The bill passed. Let me correct that: the bill was held.",),
        ("The bill passed. Scratch that, it was continued.",),
        ("The bill passed. Actually it was tabled.",),
        ("The bill passed.", "Correction, the bill did not pass."),
        ("The bill passed.", "Pardon the interruption.", "Council Bill 74010 was deferred."),
        ("The bill passed.", "I move to reconsider."),
    ],
)
def test_a_later_correction_or_deferral_withdraws_the_outcome(after):
    assert _core("Council Bill 74010 passed", HEADER, *after) is None


def test_the_correction_scope_ends_at_the_next_agenda():
    next_agenda = "Agenda item 2 Council Bill 74011 relating to parks. The bill was held over."
    assert _core("Council Bill 74010 passed", HEADER, "The bill passed.", next_agenda) == CORE
    same_segment = "The bill passed. Please read the next agenda item. Sorry, it was tabled."
    assert _core("Council Bill 74010 passed", HEADER, same_segment) == CORE


@pytest.mark.parametrize(
    "after",
    [
        "Thank you all, and sorry for the delay.",
        "Actually, I want to thank the staff for their continued work.",
        "The chair will sign it.",
        "Next we will consider the master plan workflow.",
    ],
)
def test_unrelated_later_discussion_keeps_the_outcome(after):
    assert _core("Council Bill 74010 passed", HEADER, "The bill passed.", after) == CORE


def test_the_latest_current_outcome_after_an_earlier_one_is_kept():
    history = "The bill passed committee last month."
    assert _core("Council Bill 74010 passed", HEADER, history, "Today, the bill passed.") == (
        "Council Bill 74010 passed",
        (0, 2),
    )


# ----- B: committee, preliminary and earlier passage is no final procedural outcome ---------


@pytest.mark.parametrize(
    "after",
    [
        ("This bill passed committee, five in favor, none opposed.", "We will vote next week."),
        ("The bill passed out of committee unanimously.",),
        ("As you know, the bill passed last month.",),
        ("The bill passed last year.",),
        ("The bill passed committee and will go to council next week.",),
        ("The bill passed on first reading only; final vote later.",),
        ("The bill passes to full council with a do pass recommendation.",),
        ("The bill passed.", "We will vote on it again next week."),
    ],
)
def test_preliminary_earlier_or_future_passage_is_no_core(after):
    assert _core("Council Bill 74010 passed", HEADER, *after) is None
    assert _core("Council Bill 74010 passed 5-0", HEADER, *after) is None


def test_a_committee_tally_never_joins_the_final_outcome():
    texts = (HEADER, "In committee, five in favor, none opposed.", "The bill passed.")
    assert _core("Council Bill 74010 passed 5-0", *texts) == ("Council Bill 74010 passed", (0, 2))


# ----- C: an unlabelled list or range of any length is ambiguous ----------------------------


@pytest.mark.parametrize(
    ("header", "family", "number"),
    [
        ("Agenda item 1 Council Bill 74010 and 2026", "bill", "74010"),
        ("Agenda item 1 Council Bill 74010, 2019", "bill", "74010"),
        ("Agenda item 1 Appointment 345 through 2020", "appointment", "345"),
        ("Agenda item 1 Appointment 1900 and 7402", "appointment", "1900"),
        ("Agenda item 1 Appointments 7401, and 7402", "appointment", "7401"),
        ("Agenda item 1 Appointments 7401 as well as 7402", "appointment", "7401"),
        ("Agenda item 1 Appointments 7401/7402", "appointment", "7401"),
        ("Agenda item 1 Appointment 7401 and 7", "appointment", "7401"),
    ],
)
def test_any_unlabelled_sibling_number_is_a_list(header, family, number):
    assert not _header_states_record(family, number, header)
    verb, noun = ("passed", "The bill") if family == "bill" else ("confirmed", "An appointment")
    item = f"{'Council Bill' if family == 'bill' else 'Appointment'} {number} {verb}"
    assert _core(item, header, f"{noun} is {verb}.") is None


def test_a_mention_never_stands_in_for_an_ambiguous_header():
    header = "Agenda item 1 Appointments 7401 and 7402"
    texts = (header, "Appointment 7401 is next.", "An appointment is confirmed.")
    assert _core("Appointment 7401 confirmed", *texts) is None


@pytest.mark.parametrize(
    "header",
    [
        "Agenda item 1 Council Bill 74010 dated May 3, 2026",
        "Agenda item 1 Council Bill 74010 of 2026",
        "Agenda item 1 Council Bill 74010 (2026)",
    ],
)
def test_a_labelled_date_is_no_sibling(header):
    assert _header_states_record("bill", "74010", header)
    assert _core("Council Bill 74010 passed", header, "The bill passed.") == CORE


# ----- end to end: an unresolved decision never republishes a withdrawn outcome -------------


@pytest.mark.parametrize(
    "texts",
    [
        (
            "Agenda item 3 Council Bill 204611 relating to the harbor fund.",
            "This bill passed committee, five in favor, none opposed.",
            "We will vote on it next week.",
        ),
        (
            "Agenda item 3 Council Bill 204611 relating to the harbor fund.",
            "The bill passed.",
            "Sorry, I misspoke, the bill was held over to next week.",
        ),
        ("Agenda item 3 Council Bills 204611 and 2019 relating to fees.", "The bill passed."),
    ],
)
def test_unresolved_decision_publishes_no_withdrawn_core(texts):
    source = {"meeting_date": "2026-03-04", "speakers": [], "segments": _segments(*texts)}
    ids = [s["id"] for s in source["segments"]]
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["decisions"] = [
        {"text": "Council Bill 204611 passed 5-0 to fund repairs", "source_ids": ids[1:]}
    ]
    audit = SemanticAudit(source, nd.validate_document(document, source))
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert published["decisions"] == []
    assert (counts["core_preserved"], counts["quarantined"]) == (0, 1)
