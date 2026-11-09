"""Synthetic v42 guard: an action owner needs local provenance in its own cited turn."""

from __future__ import annotations

import copy

import pytest
from notes_helpers import semantic_keep_patch

from asr_studio import notes_document as nd
from asr_studio.notes_context import unverified_owner
from asr_studio.notes_semantic import SemanticAudit


def _source(*turns: tuple[str, str]) -> dict:
    """(label, text) segments, four seconds apart; labels are generic and never identify."""
    return {
        "meeting_date": "2026-03-04",
        "speakers": [],
        "segments": [
            {
                "id": f"{i:032x}",
                "start_seconds": i * 4.0,
                "end_seconds": i * 4.0 + 3,
                "text": text,
                "speaker_id": label,
                "speaker_name": label,
            }
            for i, (label, text) in enumerate(turns)
        ],
    }


def _sid(index: int) -> str:
    return f"{index:032x}"


def _unverified(source: dict, owner: str, cited: list[int], text: str = "Follow up") -> bool:
    segments = source["segments"]
    position = {segment["id"]: index for index, segment in enumerate(segments)}
    item = {"text": text, "source_ids": [_sid(i) for i in cited], "owner": owner}
    return unverified_owner(item, segments, position)


A, B, C = "SPEAKER_01", "SPEAKER_03", "SPEAKER_00"

# A chair introduces themself, other turns intervene, the same generic label later commits.
REMOTE = _source(
    (A, "Good afternoon. I'm Dana Brook, chair of the panel. Please call the roll."),
    (C, "Present."),
    (A, "Thank you. Next speaker, please."),
    (B, "Hello. For the record, my name is Rae Vint, owner of Vint Hauling."),
    (B, "I hope somebody reaches out to me about the contracts."),
    (A, "Ms. Vint, I'd like to speak with you after the hearing today."),
    (A, "If you do not have time, I'll follow up with you."),
)


def test_remote_self_introduction_with_the_same_generic_label_is_no_owner_proof():
    text = "Speak with Rae Vint regarding contracting concerns"
    assert _unverified(REMOTE, "Dana Brook", [0, 3, 5, 6], text)
    # Without the commitment turn, the intro and the counterpart turn alone do support it.
    assert not _unverified(REMOTE, "Dana Brook", [0, 3], text)


def test_label_continuity_across_a_different_turn_never_joins_turns():
    source = _source(
        (A, "I'm Dana Brook."),
        (C, "Thank you."),
        (A, "I will send the report Friday."),
    )
    assert _unverified(source, "Dana Brook", [0, 2])
    # A first-person introduction reads as a vocative but never hands the floor to a next turn.
    assert _unverified(
        _source((A, "I'm Dana Brook."), (B, "I will send it.")), "Dana Brook", [0, 1]
    )
    contiguous = _source((A, "I'm Dana Brook."), (A, "I will send the report Friday."))
    assert not _unverified(contiguous, "Dana Brook", [0, 1])


def test_a_long_gap_or_agenda_break_ends_the_turn():
    gap = _source((A, "I'm Dana Brook."), *[(A, "Some remarks.")] * 20, (A, "I will send it."))
    assert _unverified(gap, "Dana Brook", [0, 21])
    agenda = _source((A, "I'm Dana Brook."), (A, "Next agenda item. I will send it."))
    assert _unverified(agenda, "Dana Brook", [0, 1])


def test_roster_and_role_mentions_are_not_identity():
    source = _source(
        (C, "Roll call: Brook present, Mr. Tull present."),
        (A, "I will circulate the draft."),
    )
    assert _unverified(source, "Mr. Tull", [0, 1])
    assert _unverified(source, "Tull", [0, 1])  # a person by honorific elsewhere


@pytest.mark.parametrize(
    ("text", "cited"),
    [
        ("Mr. Tull will circulate the draft by Friday.", [0]),  # explicit named assignment
        ("We asked Mr. Tull to circulate the draft.", [0]),
        ("Tull said he would circulate the draft.", [0]),  # direct reported statement
    ],
)
def test_explicit_assignment_or_report_keeps_the_owner(text, cited):
    source = _source((C, text), (A, "Agreed."), (C, "Mr. Tull is here."))
    assert not _unverified(source, "Mr. Tull", cited)


def test_adjacent_self_introduction_or_cited_handoff_inside_the_own_turn_keeps_the_owner():
    source = _source(
        (C, "Councilmember Tull."),
        (A, "Thank you. I'll circulate the draft."),
        (A, "My name is Ivo Tull and I will report back."),
    )
    assert not _unverified(source, "Councilmember Tull", [0, 1])  # cited handoff
    assert _unverified(source, "Councilmember Tull", [1])  # uncited handoff is no proof
    assert not _unverified(source, "Ivo Tull", [1, 2])  # intro inside the same turn
    # A cited "Thank you, <name>" after the turn proves it; the witness turn is no commitment.
    thanked = _source((A, "I'll circulate the draft."), (C, "Thank you, Councilmember Tull."))
    assert not _unverified(thanked, "Councilmember Tull", [0, 1])
    assert _unverified(thanked, "Councilmember Tull", [0])  # uncited acknowledgment


def test_group_owner_needs_its_own_cited_group():
    source = _source((C, "The finance committee will review the lease."), (A, "Noted."))
    assert not _unverified(source, "Finance Committee", [0])
    assert _unverified(source, "City Council", [0])  # "council" uncited
    assert _unverified(source, "Finance Committee", [1])


def test_out_of_scope_owner_text_is_kept():
    source = _source((C, "Staff will follow up."), (A, "Okonjo yes."))
    assert not _unverified(source, "Staff", [1])
    assert not _unverified(source, "Okonjo", [0])  # a plain roster word, not a person
    assert not _unverified(source, "", [0])


def _document(source: dict, owner: str, cited: list[int]) -> dict:
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["action_items"] = [
        {
            "text": "Speak with Rae Vint regarding contracting concerns",
            "source_ids": [_sid(i) for i in cited],
            "owner": owner,
            "deadline_text": "after today's hearing",
            "deadline_date": None,
        }
    ]
    return nd.validate_document(document, source)


def test_audit_clears_only_the_owner_and_counts_the_guard_separately():
    document = _document(REMOTE, "Dana Brook", [0, 3, 5, 6])
    audit = SemanticAudit(REMOTE, document)
    published, counts = audit.parse(semantic_keep_patch(audit))
    expected = copy.deepcopy(document["action_items"][0])
    expected["owner"] = None
    assert published["action_items"] == [expected]  # text, deadline and citations unchanged
    assert published["confirmation_items"] == []  # never quarantined
    assert counts["owner_unverified"] == 1
    assert counts["model_verified"] == 1 and counts["cleared_owner"] == 0  # raw booleans only


def test_audit_keeps_a_locally_proved_owner():
    source = _source((A, "I'm Dana Brook."), (A, "I will speak with Rae Vint after the hearing."))
    document = _document(source, "Dana Brook", [0, 1])
    audit = SemanticAudit(source, document)
    published, counts = audit.parse(semantic_keep_patch(audit))
    assert published["action_items"] == document["action_items"]
    assert counts["owner_unverified"] == 0
