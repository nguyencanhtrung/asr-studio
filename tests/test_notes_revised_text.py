"""Synthetic contract of the optional bounded revised_text in the final semantic audit."""

from __future__ import annotations

import json

import pytest
from notes_helpers import semantic_patch

from asr_studio import notes_document as nd
from asr_studio.notes_semantic import SemanticAudit

_EMPTY = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _seg(i: int, text: str) -> dict:
    return {
        "id": f"{i:032x}",
        "start_seconds": i * 5.0,
        "end_seconds": i * 5.0 + 1,
        "text": text,
        "speaker_name": "SPEAKER_00",
    }


SOURCE = {
    "meeting_date": "2026-01-05",
    "speakers": [],
    "segments": [
        _seg(1, "The panel voted 5 to 0 to approve the budget amendment if funded."),
        _seg(2, "The report went to the policy office, then to the engagement team."),
        _seg(3, "Rae will send the notice, and the vendor will not renew."),
        _seg(4, "The board met on Tuesday."),
        _seg(5, "Tarrow noted the renewal must not slip past 6 months."),
    ],
}
ID = [s["id"] for s in SOURCE["segments"]]


def _document() -> dict:
    document = json.loads(json.dumps(_EMPTY))
    document["decisions"] = [
        {"text": "Panel approved the budget amendment", "source_ids": [ID[0]]},
        {"text": "The report went to the policy office", "source_ids": [ID[1]]},
    ]
    document["key_topics"] = [
        {
            "text": "Rae sends the notice and the vendor renews the 9 contracts",
            "source_ids": [ID[2]],
        },
        {"text": "The board met on Tuesday", "source_ids": [ID[3]]},
        {"text": "Tarrow noted renewal must not slip past 6 months", "source_ids": [ID[4]]},
    ]
    return document


def _audit(allow: bool = True) -> SemanticAudit:
    return SemanticAudit(SOURCE, _document(), allow_revisions=allow)


def _change(ref: str, revised=..., text_ref: str | None = None, ids=(), owner=False) -> dict:
    change = {
        "ref": ref,
        "text_ref": text_ref or f"{ref}.o",
        "add_source_ids": list(ids),
        "clear_owner": owner,
        "clear_deadline": False,
    }
    if revised is not ...:
        change["revised_text"] = revised
    return change


def _parse(audit: SemanticAudit, *changes, unresolved=()):
    refs = [item.ref for item in audit.items]
    return audit.parse(semantic_patch(refs, changes, unresolved))


def _texts(document: dict) -> list[str]:
    return [e["text"] for section, _ in nd.SECTIONS for e in document[section]]


def test_default_contract_is_unchanged_and_rejects_revised_text():
    audit = _audit(False)
    properties = audit.schema()["properties"]["changes"]["items"]["properties"]
    assert "revised_text" not in properties
    assert "revised_text" not in audit.build_prompt(
        budget=nd.notes_budget("antigravity", "gemini-3.8-flash")
    )
    with pytest.raises(nd.NotesDocumentError):
        _parse(audit, _change("i3", "Panel approved"))
    _document_out, counts = _parse(audit, _change("i3"))  # a legacy change still works
    assert "model_revised" not in counts


@pytest.mark.parametrize("language", ["en", "vi"])
def test_enabled_schema_is_nullable_and_prompt_says_it_is_no_proof(language):
    audit = SemanticAudit(SOURCE, _document(), output_language=language, allow_revisions=True)
    schema = audit.schema()["properties"]["changes"]["items"]
    assert schema["properties"]["revised_text"] == {"type": ["string", "null"]}
    assert "revised_text" in schema["required"]
    prompt = audit.build_prompt(
        output_language=language, budget=nd.notes_budget("antigravity", "gemini-3.8-flash")
    )
    assert ("does not prove meaning" if language == "en" else "không chứng minh nghĩa") in prompt


def test_legacy_absent_and_null_mean_no_revision():
    _doc, counts = _parse(_audit(), _change("i3"), _change("i4", None))
    assert counts["model_revised"] == 0
    assert counts["changes"] == 2


def test_tally_added_with_own_citation_and_owner_kept_or_cleared():
    document = _document()
    document["action_items"] = [
        {
            "text": "Send the notice",
            "source_ids": [ID[2]],
            "owner": "Rae",
            "deadline_text": None,
            "deadline_date": None,
        }
    ]
    audit = SemanticAudit(SOURCE, document, allow_revisions=True)
    refs = [i.ref for i in audit.items]
    new = "Panel approved the budget amendment 5 to 0 if funded"
    out, counts = audit.parse(
        semantic_patch(
            refs,
            [_change("i3", new, ids=[]), _change("i5", "Send the notice", owner=True)],
        )
    )
    assert new in _texts(out)
    assert counts["model_revised"] == 2 and counts["cleared_owner"] == 1
    assert out["action_items"][0]["owner"] is None
    kept, _ = audit.parse(semantic_patch(refs, [_change("i5", "Send the notice")]))
    assert kept["action_items"][0]["owner"] == "Rae"


def test_compound_claim_partially_corrected_keeps_the_supported_core():
    out, counts = _parse(
        _audit(), _change("i0", "Rae sends the notice and the vendor will not renew")
    )
    assert "Rae sends the notice and the vendor will not renew" in _texts(out)
    assert counts["model_revised"] == 1 and counts["quarantined"] == 0


def test_normalization_without_new_digits_and_missing_relation_with_visible_id():
    revised = "The report went to the policy office, then to the engagement team"
    out, counts = _parse(_audit(), _change("i4", revised, ids=[ID[1]]))
    entry = next(e for e in out["decisions"] if e["text"] == revised)
    assert entry["source_ids"] == [ID[1]]  # an already cited ID is never duplicated
    out, counts = _parse(_audit(), _change("i4", revised, ids=[ID[2]]))
    entry = next(e for e in out["decisions"] if e["text"] == revised)
    assert entry["source_ids"] == [ID[1], ID[2]] and counts["added_ids"] == 1


def test_condition_and_negation_are_not_proven_by_the_filter():
    kept = "Panel approved the budget amendment only if funded"
    dropped = "Panel approved the budget amendment"  # a dropped condition passes the filter
    for text in (kept, dropped):
        out, counts = _parse(_audit(), _change("i3", text))
        assert text in _texts(out) and counts["model_revised"] == 1
    out, _ = _parse(_audit(), _change("i0", "Rae sends the notice and the vendor will renew"))
    assert "the vendor will renew" in " ".join(_texts(out))  # code does not judge negation


@pytest.mark.parametrize(
    "revised",
    [7, ["x"], True, "", "  \n", "x" * 400, "The panel voted 6 to 0", "Panel approved ID 77-A"],
)
def test_wrong_type_blank_oversized_or_new_number_is_rejected(revised):
    with pytest.raises(nd.NotesDocumentError):
        _parse(_audit(), _change("i3", revised))


def test_the_length_bound_is_max_300_or_one_and_a_half_times_the_original():
    ok = "Panel approved the budget amendment " + "a " * 130  # < 300 characters
    assert len(ok) <= 300
    _parse(_audit(), _change("i3", ok))
    with pytest.raises(nd.NotesDocumentError):
        _parse(_audit(), _change("i3", ok + "a " * 30))


def test_a_revision_needs_the_original_text_ref_and_its_own_origin():
    with pytest.raises(nd.NotesDocumentError):
        _parse(_audit(), _change("i3", "Panel approved", text_ref="i4.o"))
    audit = _audit()
    variant = next(r for r, _t in audit.items[2].texts if r != "i2.o")
    with pytest.raises(nd.NotesDocumentError):
        _parse(audit, _change("i2", "Tarrow noted renewal", text_ref=variant))


def test_unknown_id_unresolved_conflict_and_verified_true_are_rejected():
    audit = _audit()
    with pytest.raises(nd.NotesDocumentError):
        _parse(audit, _change("i3", "Panel approved", ids=[f"{99:032x}"]))
    with pytest.raises(nd.NotesDocumentError):
        _parse(audit, _change("i3", "Panel approved"), unresolved=["i3"])
    patch = json.loads(
        semantic_patch([f"i{n}" for n in range(5)], [_change("i3", "Panel approved")])
    )
    patch["verified"]["i3"] = True
    with pytest.raises(nd.NotesDocumentError):
        audit.parse(json.dumps(patch))


def test_a_source_excerpt_label_cannot_be_spoofed_or_revised():
    from asr_studio.notes_semantic import _EXCERPT_LABELS

    with pytest.raises(nd.NotesDocumentError):
        _parse(_audit(), _change("i3", f"{_EXCERPT_LABELS[0]} “Panel approved”"))
    document = _document()
    document["decisions"][0]["text"] = f"{_EXCERPT_LABELS[0]} “The panel voted”"
    with pytest.raises(nd.NotesDocumentError):
        _parse(SemanticAudit(SOURCE, document, allow_revisions=True), _change("i3", "Panel voted"))


def test_original_ids_are_kept_and_unrevised_metadata_is_unchanged():
    out, _ = _parse(_audit(), _change("i4", "The report went to the policy office", ids=[ID[2]]))
    assert out["decisions"][0] == {
        "text": "Panel approved the budget amendment",
        "source_ids": [ID[0]],
    }
    assert out["decisions"][1]["source_ids"][0] == ID[1]


def test_combine_across_partitions_keeps_checks_and_lists_revised_refs():
    audit = _audit()
    views = audit.partitions()
    assert len(views) == 5
    patches = []
    for view in views:
        refs = [i.ref for i in view.items]
        revised = [_change(r, "Panel approved") for r in refs if r == "i3"]
        patches.append((view, json.loads(semantic_patch(refs, revised))))
    out, counts = audit.combine_patches(patches)
    assert counts["model_revised"] == 1 and "Panel approved" in _texts(out)
    assert SemanticAudit.revised_refs([p for _v, p in patches]) == ["i3"]
    # a part naming a ref outside its partition still fails
    refs = [i.ref for i in views[1].items]
    bad = json.loads(semantic_patch(refs, [_change("i3", "Panel approved")]))
    with pytest.raises(nd.NotesDocumentError):
        audit.combine_patches([(views[0], patches[0][1]), (views[1], bad), *patches[2:]])


@pytest.mark.parametrize("language", ["en", "vi"])
def test_only_the_enabled_prompt_lifts_the_no_new_wording_sentence(language):
    from asr_studio.notes_semantic import _INSTRUCTIONS, _REVISION_REWORDING

    budget = nd.notes_budget("antigravity", "gemini-3.8-flash")
    off = SemanticAudit(SOURCE, _document(), output_language=language).build_prompt(
        output_language=language, budget=budget
    )
    on = SemanticAudit(
        SOURCE, _document(), output_language=language, allow_revisions=True
    ).build_prompt(output_language=language, budget=budget)
    for old, new in _REVISION_REWORDING[language]:
        assert old in off and old not in on and new in on
    assert off.startswith(_INSTRUCTIONS[language])
