"""Synthetic v38 guards: "compare" and "affirm" are speech-attribution verbs in every tense."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_keep_patch

from asr_studio import notes_context
from asr_studio import notes_document as nd
from asr_studio.notes_context import ATTRIBUTION_FORMS, unverified_speakers
from asr_studio.notes_semantic import SemanticAudit, anonymous_variants, attributed_speakers

COMMON = frozenset({"councilmember", "council", "the"})
NEEDS_REVIEW = "Needs review: an unverified note was removed."
# Uncited elsewhere in the source, so the name counts as a person (an honorific).
HANDOFF = "Thank you. Councilmember Okafor."
CLAIM = "The harbor walk resembles the old ferry campaign in scale."


def _sid(index: int) -> str:
    return f"{index:032x}"


def _source(*texts: str) -> dict:
    return {
        "meeting_date": "2026-05-06",
        "speakers": [],
        "segments": [
            {
                "id": _sid(i),
                "start_seconds": i * 4.0,
                "end_seconds": i * 4.0 + 3,
                "text": text,
                "speaker_name": "SPEAKER_00",
            }
            for i, text in enumerate(texts)
        ],
    }


def _unverified(text: str, source: dict, cited: list[int]) -> list[str]:
    segments = source["segments"]
    position = {segment["id"]: index for index, segment in enumerate(segments)}
    item = {"text": text, "source_ids": [_sid(i) for i in cited]}
    return unverified_speakers(item, attributed_speakers(text, COMMON), segments, position)


def test_compare_and_affirm_join_every_tense_and_the_legacy_anchor_is_unchanged():
    assert {
        *("compare", "compares", "compared", "comparing"),
        *("affirm", "affirms", "affirmed", "affirming"),
    } <= ATTRIBUTION_FORMS
    # The generic (ChatGPT) context anchor keeps its original verb list byte for byte.
    assert notes_context._ATTRIBUTION_VERBS == (
        "said|says|stated|asked|requested|urged|noted|proposed|argued|supported|opposed|described|"
        "raised|expressed|recommended|thanked|emphasized|questioned|suggested|called|warned"
    )


@pytest.mark.parametrize(
    ("text", "variant"),
    [
        ("Okafor compared harbor walk to ferry campaign", "Compared harbor walk to ferry campaign"),
        ("Okafor compares harbor walk to ferry campaign", "Compares harbor walk to ferry campaign"),
        (
            "Okafor comparing harbor walk to ferry campaign",
            "Comparing harbor walk to ferry campaign",
        ),
        (
            "Brennick affirmed office role standing with tenants",
            "Affirmed office role standing with tenants",
        ),
        ("Councilmember Brennick affirms office role", "Affirms office role"),
    ],
)
def test_a_leading_compare_or_affirm_names_the_speaker_and_has_a_literal_suffix(text, variant):
    assert attributed_speakers(text, COMMON)[0][0] in {"Okafor", "Brennick"}
    assert anonymous_variants(text, COMMON)[0] == variant


def test_an_unproven_person_is_flagged_and_local_proof_in_any_tense_clears_it():
    text = "Okafor compared harbor walk to ferry campaign"
    source = _source(
        HANDOFF,  # 0: uncited handoff
        CLAIM,  # 1
        "My name is Dee Okafor, and I compare this walk to the ferry campaign.",  # 2
        "Okafor would compare the walk to the ferry campaign.",  # 3: modal report
        "As Okafor is comparing, the walk resembles the ferry campaign.",  # 4: -ing report
    )
    assert _unverified(text, source, [1]) == ["Okafor"]  # the handoff is not cited
    assert _unverified(text, source, [0, 1]) == []  # cited handoff before the turn
    assert _unverified(text, source, [1, 2]) == []  # cited self-introduction
    assert _unverified(text, source, [3]) == []  # cited modal report
    assert _unverified(text, source, [4]) == []  # cited -ing report
    # A different person's self-introduction proves nothing about Okafor.
    other = _source(HANDOFF, CLAIM, "My name is Lu Brennick, and I affirm this walk.")
    assert _unverified(text, other, [1, 2]) == ["Okafor"]


def test_the_audit_drops_an_unproven_compare_or_affirm_attribution_and_keeps_proven_ones():
    source = _source(
        HANDOFF,  # 0
        CLAIM,  # 1
        "Thank you, Councilmember Brennick.",  # 2: uncited, makes Brennick a person
        "Our office will always stand with tenants.",  # 3
        "Hello, I am Lu Brennick, and I affirm that our office stands with tenants.",  # 4
    )
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": "Okafor compared harbor walk to ferry campaign", "source_ids": [_sid(1)]},
        {"text": "Brennick affirmed office stands with tenants", "source_ids": [_sid(3)]},
        {"text": "Brennick affirmed office stands with tenants", "source_ids": [_sid(4)]},
    ]
    document = nd.validate_document(document, source)
    published, counts = SemanticAudit(source, document).parse(
        semantic_keep_patch(SemanticAudit(source, document))
    )
    assert [item["text"] for item in published["key_topics"]] == [
        "Compared harbor walk to ferry campaign",
        "Affirmed office stands with tenants",
        "Brennick affirmed office stands with tenants",  # cited self-introduction
    ]
    assert counts["anonymized"] == 2 and counts["quarantined"] == 0


def test_an_unproven_compare_attribution_without_a_safe_suffix_is_quarantined():
    # The only suffix still names an unproven person (absent from the source): quarantine.
    source = _source("Thank you. Councilmember Okafor.", "The walk resembles the ferry campaign.")
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": "Okafor: Brennick affirmed walk resembles ferry campaign", "source_ids": [_sid(1)]}
    ]
    document = nd.validate_document(document, source)
    audit = SemanticAudit(source, document)
    published, counts = audit.parse(semantic_keep_patch(audit))
    assert published["key_topics"] == []
    texts = [item["text"] for item in published["confirmation_items"]]
    assert len(texts) == 1 and texts[0].startswith(NEEDS_REVIEW)
    assert counts["quarantined"] == 1


# ----- a leading attributed name absent from the whole source is unverified -------------------


@pytest.mark.parametrize(
    "text",
    [
        "Okafor compared harbor walk to ferry campaign",  # untitled person, never in the source
        "Lindqvist Trust affirmed support for the harbor walk",  # invented organization
        "Okafor urged the harbor walk",  # absent name, existing verb
    ],
)
def test_an_untitled_name_absent_from_the_whole_source_is_unverified(text):
    source = _source("Thank you. Councilmember. Oh, Kafir.", CLAIM)  # an ASR alias never proves
    assert _unverified(text, source, [1]) == [attributed_speakers(text, COMMON)[0][0]]


def test_a_same_name_substring_is_no_exact_occurrence():
    source = _source("Okaforson Holdings and Okafor-ish plans.", CLAIM)
    assert _unverified("Okafo compared harbor walk to ferry campaign", source, [1]) == ["Okafo"]


def test_an_untitled_organization_the_source_states_stays_out_of_person_scope():
    source = _source("Harbor Trust compares the walk to the ferry campaign.", CLAIM)
    assert _unverified("Harbor Trust compared walk to ferry campaign", source, [1]) == []
    assert _unverified("Harbor Trust compared walk to ferry campaign", source, [0]) == []


def test_a_titled_person_with_only_uncited_recognition_stays_unverified():
    source = _source(HANDOFF, CLAIM)
    assert _unverified("Councilmember Okafor compared walk to ferry campaign", source, [1]) == [
        "Okafor"
    ]
    assert _unverified("Councilmember Okafor compared walk to ferry campaign", source, [0, 1]) == []


def test_the_audit_anonymizes_an_absent_untitled_attribution_and_keeps_a_stated_organization():
    source = _source(
        "Thank you. Councilmember. Oh, Kafir.",  # 0: uncited ASR alias
        CLAIM,  # 1
        "Our office will always stand with tenants.",  # 2
        "Harbor Trust compares the walk to the ferry campaign.",  # 3
    )
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": "Okafor compared harbor walk to ferry campaign", "source_ids": [_sid(1)]},
        {"text": "Okafor affirmed office stands with tenants", "source_ids": [_sid(2)]},
        {"text": "Harbor Trust compared walk to ferry campaign", "source_ids": [_sid(3)]},
    ]
    document = nd.validate_document(document, source)
    audit = SemanticAudit(source, document)
    published, counts = audit.parse(semantic_keep_patch(audit))
    assert [item["text"] for item in published["key_topics"]] == [
        "Compared harbor walk to ferry campaign",
        "Affirmed office stands with tenants",
        "Harbor Trust compared walk to ferry campaign",
    ]
    assert counts["anonymized"] == 2 and counts["quarantined"] == 0
