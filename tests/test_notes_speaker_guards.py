"""Synthetic v34 guards: attributed speakers, group actors and partial-progress completions."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_keep_patch

from asr_studio import notes_document as nd
from asr_studio.notes_context import ATTRIBUTION_FORMS, unverified_speakers
from asr_studio.notes_semantic import (
    SemanticAudit,
    anonymous_variants,
    attributed_speakers,
    strengthened_completion,
    unverified_group_actor,
)

NEEDS_REVIEW = "Needs review: an unverified note was removed."
COMMON = frozenset({"councilmember", "council", "the"})


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
                "speaker_name": "Vale" if i == 2 else "SPEAKER_00",  # a label never identifies
            }
            for i, text in enumerate(texts)
        ],
    }


def _sid(index: int) -> str:
    return f"{index:032x}"


def _unverified(text: str, source: dict, cited: list[int]) -> list[str]:
    segments = source["segments"]
    position = {segment["id"]: index for index, segment in enumerate(segments)}
    item = {"text": text, "source_ids": [_sid(i) for i in cited]}
    return unverified_speakers(item, attributed_speakers(text, COMMON), segments, position)


# ----- attribution morphology and literal anonymous variants ----------------------------------


def test_attribution_forms_cover_every_tense():
    assert {"states", "stated", "urges", "urged", "testifies", "says", "arguing"} <= (
        ATTRIBUTION_FORMS
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Councilmember Vale states intention to vote yes", "States intention to vote yes"),
        ("Councilmember Vale urges more housing density", "Urges more housing density"),
        ("Vale stated the depot is late", "Stated the depot is late"),
    ],
)
def test_a_leading_titled_attribution_has_a_literal_suffix_variant(text, expected):
    assert anonymous_variants(text, COMMON)[0] == expected
    assert attributed_speakers(text, COMMON)[0][0] == "Vale"


def test_speech_nouns_and_attribution_nouns_name_the_right_person():
    assert attributed_speakers("Ana Vale testimony: depot is late", COMMON) == [("Vale", False)]
    assert attributed_speakers("Whether Ana Vale comments are on topic", COMMON) == [
        ("Vale", False)
    ]
    assert attributed_speakers("Argument: depot is late", COMMON) == []
    assert attributed_speakers("Council goal: open the depot", COMMON) == []


# ----- speaker identity needs local recognition in the item's own pool ------------------------

THANKS_LATER = (
    "Any other comments? I think the next one comes from a skater.",  # malformed recognition
    "Thank you, Mr. President. I will be voting yes to give the depot more time.",
    "We voted yes last year to fund the study.",
    "Roll call. Vale aye. Ro aye. Nine in favor.",  # a roll call
    "I really want to thank Councilmember Vale for bringing us across the finish line.",
)


@pytest.mark.parametrize("cited", [[1, 2, 4], [1, 2, 3], [1, 2, 3, 4]])
def test_unrelated_thanks_or_roll_call_in_the_pool_never_identifies_the_speaker(cited):
    text = "Councilmember Vale states intention to vote yes to give the depot more time"
    assert _unverified(text, _source(*THANKS_LATER), cited) == ["Vale"]


@pytest.mark.parametrize(
    ("texts", "cited"),
    [
        (("Thank you. Councilmember Vale.", "I urge more density near the depot."), [0, 1]),
        (("I urge more density near the depot.", "Thank you, Councilmember Vale."), [0, 1]),
        (("Hello. My name is Ana Vale, and I urge more density near the depot.",), [0]),
        (("Thank you. Good afternoon. Ana Vale, Acme here on behalf of the depot.",), [0]),
        (("As Councilmember Vale said, we need more density near the depot.",), [0]),
        (("I thank Vale, who urged more density near the depot.",), [0]),
    ],
)
def test_a_local_handoff_acknowledgment_intro_or_direct_report_identifies_the_speaker(texts, cited):
    assert _unverified("Councilmember Vale urges more density", _source(*texts), cited) == []


@pytest.mark.parametrize(
    ("texts", "cited"),
    [
        (("I urge more density near the depot.", "Thank you, Councilmember Ro."), [0, 1]),
        (("Councilmember Ro.", "I urge more density near the depot."), [0, 1]),
        (("Thank you, Councilmember Vale.", "Ro here. I urge more density."), [1]),  # uncited
        (("I urge density.", "Speaker", "I urge more density near the depot."), [2]),  # label
    ],
)
def test_thanks_or_handoff_to_someone_else_and_speaker_labels_do_not_identify(texts, cited):
    assert _unverified("Councilmember Vale urges more density", _source(*texts), cited) == ["Vale"]


def test_a_call_order_name_needs_recognition_but_an_untitled_organization_is_out_of_scope():
    source = _source(
        "We'll start off with Ana Vale, followed by Ben Ro.",
        "Go ahead, Mr. Vail. You have the floor.",  # inexact handoff: never matched
        "Thank you. Harbor Trust keeps the city rundown.",  # an organization the source states
        "Excuse me. Is there an agenda item or specific work?",
    )
    assert _unverified("Inquiry whether Ana Vale comments address an agenda item", source, [0, 3])
    assert _unverified("Inquiry whether Ana Vale comments address an agenda item", source, [3])
    assert not _unverified("Harbor Trust urges more density", source, [2])


# ----- group actors ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("Council goal: finalize designations by year end", "With the goal of finalizing it."),
        ("Committees' goals: finish the review", "The councilmember wants to finish."),
        ("The Board will finish the review", "We will finish the review."),
        ("Hội đồng mục tiêu: hoàn tất chỉ định", "Với mục tiêu hoàn tất trong năm."),
    ],
)
def test_a_leading_group_actor_absent_from_the_pool_is_unverified(claim, cited):
    assert unverified_group_actor(claim, cited)


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("Council goal: finalize designations", "Specific expectations that the Council have."),
        ("Council goals: finish the review", "the council's goals include the review"),
        ("Committee recommendation to pass", "The committees recommend the bill pass."),
        ("Council Bill 207311 extends the district", "Bill 207311 extends the district."),
        ("Councilmember goal: finalize designations", "With the goal of finalizing it."),
        ("Goal for the council: finalize designations", "With the goal of finalizing it."),
    ],
)
def test_a_cited_group_a_record_label_or_a_later_common_noun_passes(claim, cited):
    assert unverified_group_actor(claim, cited) is None


# ----- partial progress is not completion -----------------------------------------------------

PARTIAL = "The consultant will complete much of the work needed to develop a recommendation."


@pytest.mark.parametrize(
    ("claim", "cited", "strengthened"),
    [
        ("Consultant completion of recommendation by June", PARTIAL, True),
        ("Consultant to complete the recommendation by June", PARTIAL, True),
        ("Consultant to complete the recommendation by June", "It will complete it.", False),
        ("Consultant to complete much of the work by June", PARTIAL, False),
        ("Consultant to complete the recommendation", "A recommendation is due in June.", False),
        ("Consultant completion of recommendation", f"{PARTIAL} They will complete it.", False),
        ("Tư vấn hoàn thành kiến nghị", "Tư vấn sẽ hoàn thành một phần công việc.", True),
        ("Tư vấn hoàn thành kiến nghị", "Tư vấn sẽ hoàn thành kiến nghị.", False),
    ],
)
def test_completion_is_unverified_only_when_every_cited_completion_is_partial(
    claim, cited, strengthened
):
    assert strengthened_completion(claim, cited) is strengthened


# ----- the patch keeps useful summaries and publishes only safe metadata ----------------------


def test_the_audit_anonymizes_quarantines_and_keeps_supported_items():
    source = _source(
        "Thank you, Mr. President. I will be voting yes to give the depot more time.",  # 0
        "Thank you, Councilmember Ro. Councilmember Tamm.",  # 1
        "I support the depot study and urge a vote.",  # 2
        "We'll start off with Ana Vale, followed by Ben Ro.",  # 3
        "Is there an agenda item? The inquiry is whether it is specific work.",  # 4
        "Legacy program with the goal of finalizing designations by the end of this year.",  # 5
        PARTIAL + " by the end of June.",  # 6
        "I really want to thank Councilmember Vale for the depot.",  # 7
    )
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {
            "text": "Councilmember Vale states intention to vote yes",
            "source_ids": [_sid(0), _sid(7)],
        },
        {"text": "Councilmember Tamm urges a depot vote", "source_ids": [_sid(1), _sid(2)]},
    ]
    document["open_questions"] = [
        {
            "text": "Inquiry whether Ana Vale comments address an agenda item",
            "source_ids": [_sid(3), _sid(4)],
        }
    ]
    document["action_items"] = [
        {
            "text": "Consultant to complete the recommendation",
            "source_ids": [_sid(6)],
            "owner": "Consultant",
            "deadline_text": "end of June",
            "deadline_date": None,
        }
    ]
    document["confirmation_items"] = [
        {"text": "Council goal: finalize designations by year end", "source_ids": [_sid(5)]},
        {"text": "Designations finalized by year end", "source_ids": [_sid(5)]},
    ]
    document = nd.validate_document(document, source)
    audit = SemanticAudit(source, document)
    published, counts = audit.parse(semantic_keep_patch(audit))
    assert [item["text"] for item in published["key_topics"]] == [
        "States intention to vote yes",
        "Councilmember Tamm urges a depot vote",
    ]
    assert published["key_topics"][0]["source_ids"] == [_sid(0), _sid(7)]
    assert published["open_questions"] == [] and published["action_items"] == []
    texts = [item["text"] for item in published["confirmation_items"]]
    assert texts[0] == "Designations finalized by year end"
    assert len(texts) == 4 and all(text.startswith(NEEDS_REVIEW) for text in texts[1:])
    assert not any("Council goal" in text or "Ana Vale comments" in text for text in texts)
    assert all(set(item) == {"text", "source_ids"} for item in published["confirmation_items"])
    assert counts["anonymized"] == 1 and counts["quarantined"] == 3
