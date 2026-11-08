"""Bounded '.p' variants: one trailing secondary responsibility phrase becomes a fixed marker."""

from __future__ import annotations

import json
import re

import pytest

from asr_studio import notes_document as nd
from asr_studio.notes_semantic import AUDIT_VERSION, SemanticAudit, responsibility_variant

MARK = "[responsibility unclear]"
CORE = "Plan has 40 senior units "
TAIL = " at zero land cost by June 2027"
TEXT = CORE + "developed by Harbor Housing Services and Riverbend Partners" + TAIL


def test_only_the_verb_and_proper_actor_become_the_marker():
    common = frozenset({"housing", "services"})  # the source also writes these lower-case
    assert responsibility_variant(TEXT, common) == CORE + MARK + TAIL
    assert responsibility_variant(TEXT, common, "vi") == CORE + "[vai trò chưa rõ]" + TAIL
    vi = "Kế hoạch có 40 căn hộ phát triển bởi Harbor Housing, không tốn chi phí đất"
    assert responsibility_variant(vi, output_language="vi") == (
        "Kế hoạch có 40 căn hộ [vai trò chưa rõ], không tốn chi phí đất"
    )


@pytest.mark.parametrize(
    "verb",
    "developed built funded operated managed delivered supported sponsored owned led".split(),  # noqa: SIM905
)
def test_each_generic_responsibility_verb_with_or_without_by(verb):
    for by in (" by", ""):
        text = f"Program serves 120 riders {verb}{by} Northgate Transit Alliance, weekdays only"
        variant = responsibility_variant(text)
        assert variant == f"Program serves 120 riders {MARK}, weekdays only"


def _protected_values(text: str) -> list[str]:
    words = r"\b(?:not|never|no|without|if|unless|may|must|will|would|should|zero|June|weekdays)\b"
    return re.findall(rf"\d[\d.,%-]*|{words}", text)


@pytest.mark.parametrize(
    "text",
    [
        TEXT,
        "Phase 2 adds 85 supportive units, 15% below market, funded by Cedar Trust if rates hold",
        "Site holds 3 buildings operated by Lakeside Group; tenants must not exceed 2 per room",
    ],
)
def test_every_digit_date_negation_condition_modality_and_outside_word_is_kept(text):
    variant = responsibility_variant(text)
    assert variant is not None
    start = variant.index(MARK)
    before, after = variant[:start], variant[start + len(MARK) :]
    assert text.startswith(before) and text.endswith(after)  # byte-exact outside the span
    removed = text[len(before) : len(text) - len(after)]
    assert not any(c.isdigit() for c in removed)
    assert _protected_values(variant.replace(MARK, " ")) == _protected_values(text)


@pytest.mark.parametrize(
    "text",
    [
        "Senior units developed by Harbor Housing at zero land cost",  # no quantitative core
        "40 units were developed by Harbor Housing",  # passive main predicate: primary role
        "40 units will be funded by Cedar Trust",  # modality governs the verb
        "40 units not funded by Cedar Trust",  # negated
        "40 units if funded by Cedar Trust",  # conditional
        "40 units haven't funded by Cedar Trust",
        "Plan has 40 units “developed by Harbor Housing”",  # quoted source wording
        'Plan has 40 units "developed by Harbor Housing"',
        "Council approved 40 units funded by Cedar Trust",  # approval
        "Vote 7-2 on 40 units funded by Cedar Trust",  # vote
        "Bill 120315 adds 40 units funded by Cedar Trust",  # main record
        "40 units funded by District 7 Partners",  # numbered actor
        "40 units funded by the city",  # no proper actor
        "40 units funded by May",  # a date word is never an actor
        "40 units built by Harbor Housing and 12 homes funded by Cedar Trust",  # ambiguous
        "Plan has 40 units. Built by Harbor Housing",  # the core is in another sentence
    ],
)
def test_no_variant_for_protected_primary_quoted_or_ambiguous_spans(text):
    assert responsibility_variant(text) is None


def test_actor_phrase_length_is_bounded():
    words = "Alder Birch Cedar Dogwood Elm Fir Ginkgo Hazel Ilex Juniper Kauri Larch Maple Nyssa"
    assert responsibility_variant(f"40 units funded by {' '.join(words.split()[:11])} today")
    assert responsibility_variant(f"40 units funded by {words} today") is None


# ----- audit text refs and counts -------------------------------------------------------------


def _segment(index: int, text: str) -> dict:
    return {
        "id": f"{index:032x}",
        "start_seconds": float(index),
        "end_seconds": index + 0.5,
        "text": text,
        "speaker_name": "SPEAKER_00",
    }


SOURCE = {
    "meeting_date": "2026-03-02",
    "speakers": [],
    "segments": [
        _segment(1, "the plan has 40 senior units with Harbor Housing support at zero land cost."),
        _segment(2, "per section 23.45.010 the plan adds 12 homes; Cedar Trust is a partner."),
        # Uncited: the name exists in the source but is never treated as a person (v38).
        _segment(3, "The Tarrow annex quote is closed."),
    ],
}
ID = [segment["id"] for segment in SOURCE["segments"]]
LEGAL = "Under Section 23.45.010 plan adds 12 homes funded by Cedar Trust, near transit"
ANON = "Tarrow noted plan has 40 units developed by Harbor Housing at zero land cost"


def _audit(language: str = "en") -> SemanticAudit:
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": LEGAL, "source_ids": [ID[1]]},
        {"text": ANON, "source_ids": [ID[0]]},
        {"text": "Quote: “plan has 40 units developed by Harbor Housing”", "source_ids": [ID[0]]},
        {"text": "Tarrow noted " + LEGAL, "source_ids": [ID[1]]},
    ]
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE), output_language=language)


def test_audit_offers_p_and_legal_or_anonymous_combinations_with_a_cap():
    audit = _audit()
    texts = {item.ref: dict(item.texts) for item in audit.items}
    assert list(texts["i0"]) == ["i0.o", "i0.r", "i0.p", "i0.pr"]
    assert texts["i0"]["i0.p"] == f"Under Section 23.45.010 plan adds 12 homes {MARK}, near transit"
    assert texts["i0"]["i0.pr"] == (
        f"Under Section [reference unclear] plan adds 12 homes {MARK}, near transit"
    )
    assert list(texts["i1"])[-2:] == ["i1.p", "i1.a1p"]
    assert texts["i1"]["i1.a1p"] == f"Noted plan has 40 units {MARK} at zero land cost"
    assert not any("p" in ref.split(".")[1] for ref in texts["i2"])  # a quote is never masked
    # o, r, a1, a1r could each take a '.p'; only the first two responsibility options are kept.
    assert [r for r in texts["i3"] if "p" in r.split(".")[1]] == ["i3.p", "i3.pr"]
    for item in audit.items:
        assert sum("p" in ref.split(".")[1] for ref, _ in item.texts) <= 2


def _parse(audit: SemanticAudit, choices: dict[str, str]) -> dict[str, int]:
    refs = [item.ref for item in audit.items]
    changes = [
        {
            "ref": ref,
            "text_ref": text_ref,
            "add_source_ids": [],
            "clear_owner": False,
            "clear_deadline": False,
        }
        for ref, text_ref in choices.items()
    ]
    patch = {
        "audit_version": AUDIT_VERSION,
        "verified": {ref: ref not in choices for ref in refs},
        "changes": changes,
        "unresolved_refs": [],
    }
    return audit.parse(json.dumps(patch))[1]


@pytest.mark.parametrize(
    ("choices", "expected"),
    [
        ({"i0": "i0.p"}, (1, 0, 0)),
        ({"i0": "i0.pr"}, (1, 1, 0)),
        ({"i0": "i0.r"}, (0, 1, 0)),
        ({"i1": "i1.a1p"}, (1, 0, 1)),
        ({"i0": "i0.pr", "i1": "i1.a1p"}, (2, 1, 1)),
    ],
)
def test_counts_keep_responsibility_legal_and_anonymous_separate(choices, expected):
    counts = _parse(_audit(), choices)
    assert (
        counts["responsibility_unclear"],
        counts["reference_unclear"],
        counts["anonymized"],
    ) == (expected)


def test_marker_follows_the_output_language():
    audit = _audit("vi")
    texts = dict(audit.items[0].texts)
    assert "[vai trò chưa rõ]" in texts["i0.p"] and "[tham chiếu chưa rõ]" in texts["i0.pr"]
