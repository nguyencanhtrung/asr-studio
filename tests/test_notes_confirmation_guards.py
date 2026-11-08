"""Synthetic confirmation-item guards: uncited official roles, conflicting modified record IDs."""

from __future__ import annotations

import json

import pytest

from asr_studio import notes_document as nd
from asr_studio.notes_semantic import (
    SemanticAudit,
    mark_conflicting_modified_records,
    unverified_roles,
)

R = "[reference unclear]"
NEEDS_REVIEW = "Needs review: an unverified note was removed."
KEEP = json.dumps({"audit_version": 1, "changes": [], "unresolved_refs": []})


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


# ----- official roles -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("Board Chair to meet Ms. Ilves", "Ms. Ilves, I would like to talk after the session."),
        ("Mayor asked for a depot study", "Councilmember Oro asked for a depot study."),
        ("Commissioner Tamm to follow up", "Tamm will follow up next week."),
        ("Thị trưởng đề nghị nghiên cứu", "The council asked for a depot study."),
    ],
)
def test_an_official_role_absent_from_the_cited_pool_is_unverified(claim, cited):
    assert unverified_roles(claim, cited)


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("Board President Ilves to meet", "Thank you, Madam president."),
        ("council member Oro asked", "Councilmember Oro asked for a study."),
        ("Chủ tịch đề nghị nghiên cứu", "Thank you, Mr. Chair. I ask for a study."),
        ("Director of parks to report", "The parks DIRECTOR will report."),
        ("Depot study requested", "SPEAKER_01: I ask for a depot study."),
    ],
)
def test_a_literal_role_cue_or_no_role_passes(claim, cited):
    assert not unverified_roles(claim, cited)


def test_an_uncited_role_quarantines_only_that_confirmation_item():
    source = _source(
        "Ms. Ilves, after the session I would like to speak with you.",  # SPEAKER label only
        "Madam President Ilves opened the depot hearing.",  # the role, but not cited
        "The depot study is pending.",
    )
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [{"text": "Board President opened hearing", "source_ids": [_sid(1)]}]
    document["confirmation_items"] = [
        {"text": "Meeting between Board President and Ms. Ilves", "source_ids": [_sid(0)]},
        {"text": "Depot study pending", "source_ids": [_sid(2)]},
    ]
    document = nd.validate_document(document, source)
    published, counts = SemanticAudit(source, document).parse(KEEP)
    assert published["key_topics"] == document["key_topics"]
    texts = [item["text"] for item in published["confirmation_items"]]
    assert texts[0] == "Depot study pending" and len(texts) == 2
    assert texts[1].startswith(NEEDS_REVIEW) and "President" not in texts[1]
    assert published["confirmation_items"][1]["source_ids"] == [_sid(0)]
    assert counts["quarantined"] == 1 and counts["reference_unclear"] == 0


# ----- modified record IDs --------------------------------------------------------------------

CONFLICT = "Item 4 relating to the depot amending ordinance 431 778 extends it. It amends ordinance 431770."  # noqa: E501


@pytest.mark.parametrize(
    ("claim", "language", "expected"),
    [
        (
            "Bill 207311 amending Ordinance 431778 to extend",
            "en",
            f"Bill 207311 amending Ordinance {R} to extend",
        ),  # noqa: E501
        ("Bill 207311 amending Ordinance 431 770", "en", f"Bill 207311 amending Ordinance {R}"),
        (
            "Dự luật 207311 sửa đổi sắc lệnh 431778",
            "vi",
            "Dự luật 207311 sửa đổi sắc lệnh [tham chiếu chưa rõ]",
        ),  # noqa: E501
    ],
)
def test_close_conflicting_variants_mark_only_the_modified_record_digits(claim, language, expected):
    assert mark_conflicting_modified_records(claim, CONFLICT, language) == (expected, 1)


@pytest.mark.parametrize(
    ("claim", "cited"),
    [
        ("Bill 207311 amending Ordinance 431778", "It amends ordinance 431 778 by six months."),
        (
            "Bill 207311 amending Ordinance 431778",
            "Amending ordinance 431778; repeals resolution 431770.",
        ),  # noqa: E501
        (
            "Bill 207311 amending Ordinance 431778",
            "Amending ordinance 431778; amends ordinance 902115.",
        ),  # noqa: E501
        ("Bill amending Ordinances 431770 and 431778", CONFLICT),
        ("Bill amending Ordinance 431770 and Ordinance 431778", CONFLICT),
        ("Bill amending Ordinance 431770 and 431778", CONFLICT),
        ("Bill amending Ordinance 431770 and431778", CONFLICT),
        ("Bill amending Ordinance 431770, 431778", CONFLICT),
        ("Dự luật sửa đổi sắc lệnh 431770 và 431778", CONFLICT),
        # A different ID family (leading half differs) is an unrelated record, not a variant.
        (
            "Bill 207311 amending Ordinance 431778",
            "Amends ordinance 431778; amends ordinance 439778.",
        ),
        (
            "Bill 207311 amending Ordinance 431778",
            "Amends ordinance 431778; amends ordinance 531778.",
        ),
        ("Ordinance 431778 approved; Ordinance 431770 noted", CONFLICT),
    ],
)
def test_single_sourced_unrelated_or_explicitly_listed_records_are_untouched(claim, cited):
    assert mark_conflicting_modified_records(claim, cited) == (claim, 0)


def test_the_audit_marks_a_confirmation_item_and_counts_it_once():
    source = _source(CONFLICT, "Bill 207311 amending ordinance 431778 passed committee.")
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    main = {"text": "Bill 207311 amending Ordinance 431778 passed", "source_ids": [_sid(0)]}
    document["key_topics"] = [main]
    document["confirmation_items"] = [
        {"text": "Bill 207311 amending Ordinance 431778 pending", "source_ids": [_sid(0)]},
        {"text": "Bill 207311 amending Ordinance 431778 pending vote", "source_ids": [_sid(1)]},
    ]
    document = nd.validate_document(document, source)
    published, counts = SemanticAudit(source, document).parse(KEEP)
    assert published["key_topics"] == [main]
    assert [i["text"] for i in published["confirmation_items"]] == [
        f"Bill 207311 amending Ordinance {R} pending",
        "Bill 207311 amending Ordinance 431778 pending vote",
    ]
    assert counts["reference_unclear"] == 1 and counts["quarantined"] == 0


@pytest.mark.parametrize(
    "tail",
    [
        " to 6 months",
        " and 6-month extension",
        ", 6-month extension",
        " through 6 months",
        " & 6 months",
    ],
)
def test_a_duration_after_the_modified_id_is_not_a_record_list(tail):
    cited = "Bill 403827 amends Ordinance 980210. Bill 403827 amends Ordinance 980216."
    claim = "Bill 403827 amending Ordinance 980210" + tail
    text, marks = mark_conflicting_modified_records(claim, cited)
    assert text == "Bill 403827 amending Ordinance [reference unclear]" + tail
    assert marks == 1
