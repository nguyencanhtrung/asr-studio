"""A speaker's own first-person work is never published as a nominee's credential."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_patch

from asr_studio import notes_document as nd
from asr_studio.notes_semantic import SemanticAudit, speaker_work_as_credential

ITEM = (
    "Dana Reyes credentials include Coastline Energy, the Harbor Equity Board, "
    "and transit fare restructuring work"
)
CITED = (
    "Dana Reyes is a program lead at Coastline Energy. She serves on the Harbor Equity Board. "
    "And if the efforts we initiated in my committee last year to restructure transit fares "
    "end up in the right spot, that is good."
)


def test_first_person_owned_work_in_a_credential_list_is_flagged():
    assert speaker_work_as_credential(ITEM, CITED)


@pytest.mark.parametrize(
    "cited",
    [
        # The nominee's own work, stated in the third person.
        CITED.replace("the efforts we initiated in my committee", "the work she led")
        .replace("And if", "And")
        .replace("end up", "ended up"),
        # Negated first person is no ownership evidence.
        CITED.replace("the efforts we initiated", "the efforts we never initiated"),
        # The pool also states the same work as the nominee's own: cited, so kept.
        CITED + " Dana Reyes led the transit fare restructuring.",
        # A clause about the nominee with an incidental "our work" stays the nominee's.
        "Dana Reyes brings years of experience where she has worked on transit fare "
        "restructuring, similar to our work with the Harbor Equity Board.",
        # Unrelated first-person talk next to a third-person transit fare credential.
        "Dana Reyes led transit fare restructuring at Coastline Energy. She serves on the "
        "Harbor Equity Board. We initiated a parks survey in my committee.",
    ],
)
def test_third_person_negated_or_unrelated_first_person_keeps_the_item(cited):
    assert not speaker_work_as_credential(ITEM, cited)


def test_only_credential_assertions_without_first_person_are_checked():
    assert not speaker_work_as_credential("Transit fare restructuring work continues", CITED)
    assert not speaker_work_as_credential(
        "Our credentials include transit fare restructuring work", CITED
    )


def test_audit_quarantines_the_misattributed_credential_and_keeps_a_cited_one():
    texts = CITED.replace(". ", ".\n").splitlines()  # one sentence per segment
    source = {
        "meeting_date": "2026-03-04",
        "speakers": [],
        "segments": [
            {
                "id": f"{i + 1:032x}",
                "start_seconds": i * 4.0,
                "end_seconds": i * 4.0 + 1,
                "text": text,
                "speaker_name": "SPEAKER_00",
            }
            for i, text in enumerate(texts)
        ],
    }
    ids = [s["id"] for s in source["segments"]]
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": ITEM, "source_ids": ids},
        {
            "text": "Dana Reyes credentials include Coastline Energy and the Harbor Equity Board",
            "source_ids": ids[:2],
        },
    ]
    audit = SemanticAudit(source, nd.validate_document(document, source))
    keep = semantic_patch(["i0", "i1"])
    published, counts = audit.parse(keep)
    assert [item["text"] for item in published["key_topics"]] == [document["key_topics"][1]["text"]]
    assert counts["quarantined"] == 1
    assert published["confirmation_items"][0]["source_ids"] == ids
