"""Exact record-outcome core of a decision: literal record, outcome and directly stated tally."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_patch

from asr_studio import notes_document as nd
from asr_studio.notes_outcomes import outcome_core_candidate
from asr_studio.notes_semantic import SemanticAudit, reference_variant


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


def _core(text: str, texts: tuple[str, ...], pool=None, **kwargs):
    segments = _segments(*texts)
    return outcome_core_candidate(
        text, segments, range(len(segments)) if pool is None else pool, **kwargs
    )


VOTE = (
    "Agenda item 3 Council Bill 204611 relating to the harbor fund.",
    "Please call the roll on the passage of the bill.",
    "Avery, Brook, Cole, Dane, Ellis, Ford.",
    "Six in favor, not opposed.",
    "The bill passed and the chair will sign it.",
)


def test_core_keeps_record_outcome_and_direct_unopposed_tally_only():
    text = "passed Council Bill 204611 to fund harbor repairs, 6 in favor, 0 opposed"
    assert _core(text, VOTE) == ("passed Council Bill 204611, 6 in favor, 0 opposed", (0, 3, 4))
    # The same tally as "6-0" and a spaced group spelling of the record in the header.
    spaced = (VOTE[0].replace("204611", "204 611"), *VOTE[1:])
    assert _core("CB 204611 for harbor repairs passed 6-0", spaced) == (
        "CB 204611 passed 6-0",
        (0, 3, 4),
    )


@pytest.mark.parametrize(
    "tally_text",
    [
        "Avery aye, Brook aye, Cole aye, Dane aye, Ellis aye, Ford aye.",  # a roll call only
        "Six in favor, one opposed.",  # a different split
        "Six in favor.",  # the absent number is never inferred
    ],
)
def test_a_tally_not_directly_stated_as_the_same_split_is_left_out(tally_text):
    texts = (*VOTE[:3], tally_text, VOTE[4])
    assert _core("Council Bill 204611 passed 6-0", texts) == (
        "Council Bill 204611 passed",
        (0, 4),
    )


def test_two_tally_statements_in_the_span_are_ambiguous():
    texts = (*VOTE[:3], "Six in favor, not opposed.", "Five in favor, unopposed.", VOTE[4])
    assert _core("Council Bill 204611 passed 6-0", texts)[0] == "Council Bill 204611 passed"


@pytest.mark.parametrize(
    "text",
    [
        "Council Bill 204611 not passed",
        "Council Bill 204611 will be passed next week",
        "Council Bill 204611 passed if amended",
        "committee recommends Council Bill 204611 be passed",
        "Council Bill 204611 and Council Bill 204612 passed",  # primary record ambiguity
        "Council Bill 204611 passed and adopted",  # two outcomes
        "Appointments 3301 through 3304 confirmed",  # a list of records
        "Council Bill 204611 confirmed",  # not an outcome of that record family
        "harbor fund discussion passed to committee",  # no typed record
    ],
)
def test_item_without_one_unconditional_record_outcome_has_no_core(text):
    assert _core(text, VOTE) is None


def test_source_must_state_the_same_record_and_an_unqualified_outcome():
    assert _core("Council Bill 204612 passed", VOTE) is None  # wrong ID, close digits
    recommend = (*VOTE[:4], "The committee recommends the bill passed.")
    assert _core("Council Bill 204611 passed", recommend) is None
    failed = (*VOTE, "Correction: the bill failed.")
    assert _core("Council Bill 204611 passed", failed) is None
    assert _core("Council Bill 204611 passed", VOTE, pool=[1, 2, 3, 4]) is None  # record uncited
    assert _core("Council Bill 204611 passed", VOTE, pool=[0, 1, 2, 3]) is None  # outcome uncited


@pytest.mark.parametrize(
    "between",
    [
        "Agenda item 4 Resolution 31002 relating to parks.",  # another agenda header
        "Please read the next agenda item.",  # a topic break
        "This also repeals Ordinance 125001.",  # a competing labelled record
    ],
)
def test_core_never_crosses_another_agenda_or_record(between):
    texts = (*VOTE[:4], between, VOTE[4])
    assert _core("Council Bill 204611 passed", texts) is None


def test_a_break_after_the_outcome_in_the_same_segment_does_not_block():
    texts = (*VOTE[:4], "The bill passed. Please read the next agenda item.")
    assert _core("Council Bill 204611 passed", texts) == ("Council Bill 204611 passed", (0, 4))


def test_header_is_preferred_over_a_same_record_motion_mention():
    texts = (
        "Agenda item 1 Appointment 7781 of a director. The committee recommends the "
        "appointment be confirmed.",
        "I move to amend appointment 7781 to add a letter. Second.",
        "Those in favor of confirming the appointment as amended, please vote.",
        "The motion carries and the appointment is confirmed.",
    )
    core = _core("confirmed Appointment 7781 of Ms. Vale as Director as amended", texts)
    # The vote call right before the outcome states "as amended", so the qualifier is kept.
    assert core == ("confirmed Appointment 7781 as amended", (0, 2, 3))
    unqualified = (*texts[:2], "Those in favor, please vote.", texts[3])
    assert _core("confirmed Appointment 7781 as amended", unqualified) == (
        "confirmed Appointment 7781",
        (0, 3),
    )
    # The recommendation in the header is not itself an outcome.
    assert _core("confirmed Appointment 7781", texts[:2]) is None


def test_a_record_list_has_no_core_even_when_the_source_states_it():
    texts = (
        "Agenda item 6 Appointments 3301 through 3304 to the parks board.",
        "The appointments are confirmed.",
    )
    assert _core("Appointments 3301 through 3304 confirmed", texts) is None
    assert _core("Appointment 3301 and 3302 confirmed", texts) is None


def test_core_ids_must_fit_the_reference_cap():
    text = "Council Bill 204611 passed 6-0"
    assert _core(text, VOTE, cited=[4], max_refs=2) is None
    assert _core(text, VOTE, cited=[4], max_refs=3) is not None


def test_acronym_code_after_a_record_or_amend_cue_masks_only_code_numbers():
    text = "CB 204489 allowing expansion and amending SMC 23.50.012 and 23.69.024 passed 9-0"
    assert reference_variant(text) == text.replace("23.50.012 and 23.69.024", "[reference unclear]")
    labelled = "CB 204489 updates SMC 23.50.012 for expansion, passed 9-0"
    assert reference_variant(labelled) == labelled.replace("23.50.012", "[reference unclear]")
    assert reference_variant("Rotation SMC 23.50.012 noted") is None  # no statutory cue


# ----- audit integration ---------------------------------------------------------------------


def _audit(text: str, section: str = "decisions", texts=VOTE, cited=(3, 4)):
    source = {"meeting_date": "2026-03-04", "speakers": [], "segments": _segments(*texts)}
    ids = [s["id"] for s in source["segments"]]
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document[section] = [{"text": text, "source_ids": [ids[i] for i in cited]}]
    return ids, SemanticAudit(source, nd.validate_document(document, source))


def _texts(audit):
    return {
        t["text_ref"]: t["text"] for i in audit.prompt_payload()["items"] for t in i["text_refs"]
    }


def test_unresolved_decision_with_a_core_candidate_is_still_quarantined():
    ids, audit = _audit("passed Council Bill 204611 to fund harbor repairs, 6 in favor, 0 opposed")
    assert _texts(audit)["i0.c"] == "passed Council Bill 204611, 6 in favor, 0 opposed"
    assert ids[0] in audit._visible  # the candidate's header is a strict candidate, shown
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert published["decisions"] == []
    assert (counts["core_preserved"], counts["quarantined"], counts["model_verified"]) == (0, 1, 0)


def test_model_selected_core_adds_its_supporting_ids():
    ids, audit = _audit("Council Bill 204611 for harbor repairs passed 6-0")
    change = {
        "ref": "i0",
        "text_ref": "i0.c",
        "add_source_ids": [],
        "clear_owner": False,
        "clear_deadline": False,
    }
    published, counts = audit.parse(semantic_patch(["i0"], [change]))
    assert published["decisions"][0] == {
        "text": "Council Bill 204611 passed 6-0",
        "source_ids": [ids[3], ids[4], ids[0]],
    }
    assert (counts["core_selected"], counts["core_preserved"]) == (1, 0)


def test_unproven_or_non_decision_items_are_still_quarantined():
    _ids, audit = _audit("Council Bill 204611 passed if amended")
    assert "i0.c" not in _texts(audit)
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert published["decisions"] == [] and counts["quarantined"] == 1
    _ids, audit = _audit("Council Bill 204611 passed 6-0", section="key_topics")
    assert "i0.c" not in _texts(audit)
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert (counts["core_preserved"], counts["quarantined"]) == (0, 1)


def test_core_never_reaches_beyond_another_agenda_in_the_audit():
    texts = (*VOTE[:3], "Agenda item 4 Resolution 31002 relating to parks.", *VOTE[3:])
    _ids, audit = _audit("Council Bill 204611 passed 6-0", texts=texts, cited=(4, 5))
    assert "i0.c" not in _texts(audit)
    _published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert (counts["core_preserved"], counts["quarantined"]) == (0, 1)
