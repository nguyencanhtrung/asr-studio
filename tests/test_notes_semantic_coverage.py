"""Audit protocol v2: <= 4 disjoint parts and an explicit Boolean decision for every owned ref."""

from __future__ import annotations

import json

import pytest

from asr_studio import notes_document as nd
from asr_studio import notes_semantic
from asr_studio.notes_semantic import AUDIT_VERSION, MAX_AUDIT_PARTS, SemanticAudit


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
    "segments": [_segment(k, f"the plan item {k} has 3{k} homes.") for k in range(1, 11)],
}
ID = [segment["id"] for segment in SOURCE["segments"]]


def _audit(n: int) -> SemanticAudit:
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": f"Plan item {k} has 3{k} homes", "source_ids": [ID[k % len(ID)]]} for k in range(n)
    ]
    return SemanticAudit(SOURCE, nd.validate_document(document, SOURCE))


def _change(ref: str) -> dict:
    return {
        "ref": ref,
        "text_ref": f"{ref}.o",
        "add_source_ids": [ID[0]],
        "clear_owner": False,
        "clear_deadline": False,
    }


def _patch(refs, changes=(), unresolved=(), **override) -> dict:
    listed = {c["ref"] for c in changes} | set(unresolved)
    patch = {
        "audit_version": AUDIT_VERSION,
        "verified": {ref: ref not in listed for ref in refs},
        "changes": list(changes),
        "unresolved_refs": list(unresolved),
    }
    return {**patch, **override}


def _refs(audit: SemanticAudit) -> list[str]:
    return [item.ref for item in audit.items]


# ----- partitions -----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 7, 9, 10])
def test_partitions_are_min_four_n_balanced_disjoint_global_views(n):
    audit = _audit(n)
    parts = audit.partitions()
    if n < 2:
        assert parts == (audit,)
        return
    sizes = [len(part.items) for part in parts]
    assert len(parts) == min(MAX_AUDIT_PARTS, n) == min(4, n)
    assert max(sizes) - min(sizes) <= 1 and sizes == sorted(sizes, reverse=True)
    assert [ref for part in parts for ref in _refs(part)] == [f"i{k}" for k in range(n)]
    for part in parts:
        assert part.source is audit.source  # full source in every part
        assert set(part.schema()["properties"]["verified"]["required"]) == set(_refs(part))


def test_schema_requires_every_owned_ref_as_a_boolean_and_nothing_else():
    part = _audit(6).partitions()[1]
    schema = part.schema()
    verified = schema["properties"]["verified"]
    assert schema["required"] == ["audit_version", "verified", "changes", "unresolved_refs"]
    assert verified["required"] == _refs(part) and verified["additionalProperties"] is False
    assert verified["properties"] == {ref: {"type": "boolean"} for ref in _refs(part)}
    assert schema["properties"]["audit_version"]["enum"] == [2]


# ----- per-part decision coverage -------------------------------------------------------------


def test_explicit_decisions_parse_and_report_coverage_counts():
    audit = _audit(3)
    raw = json.dumps(_patch(_refs(audit), [_change("i1")], ["i2"]))
    _document, counts = audit.parse(raw)
    assert (counts["reviewed"], counts["model_verified"], counts["changes"]) == (3, 1, 1)
    assert counts["quarantined"] == 1 and counts["responsibility_unclear"] == 0


def _bad_patches(refs: list[str]) -> dict[str, dict]:
    keep = _patch(refs)
    return {
        "missing": {**keep, "verified": {r: True for r in refs[1:]}},
        "unknown": {**keep, "verified": {**keep["verified"], "i99": True}},
        "nonbool_int": {**keep, "verified": {**keep["verified"], refs[0]: 1}},
        "nonbool_str": {**keep, "verified": {**keep["verified"], refs[0]: "true"}},
        "null": {**keep, "verified": {**keep["verified"], refs[0]: None}},
        "not_object": {**keep, "verified": list(refs)},
        "absent": {k: v for k, v in keep.items() if k != "verified"},
        "extra_field": {**keep, "reviewed": True},
        "true_changed": {**_patch(refs, [_change(refs[0])]), "verified": dict.fromkeys(refs, True)},
        "true_unresolved": {
            **_patch(refs, unresolved=[refs[0]]),
            "verified": dict.fromkeys(refs, True),
        },
        "false_unlisted": {**keep, "verified": {**keep["verified"], refs[0]: False}},
        "old_version": {**keep, "audit_version": 1},
    }


@pytest.mark.parametrize("case", list(_bad_patches(["i0"])))
def test_every_decision_violation_fails_the_whole_view(case):
    for audit in (_audit(2), _audit(8).partitions()[2]):
        bad = _bad_patches(_refs(audit))[case]
        audit.parse(json.dumps(_patch(_refs(audit))))  # the valid base passes
        with pytest.raises(nd.NotesDocumentError):
            audit.validated_patch(json.dumps(bad))


def test_a_part_rejects_another_parts_decisions():
    first, second = _audit(4).partitions()[:2]
    cross = _patch(_refs(first))
    cross["verified"] = {**cross["verified"], **dict.fromkeys(_refs(second), True)}
    with pytest.raises(nd.NotesDocumentError):
        first.validated_patch(json.dumps(cross))
    swapped = _patch(_refs(second))
    with pytest.raises(nd.NotesDocumentError):
        first.validated_patch(json.dumps(swapped))


# ----- combine --------------------------------------------------------------------------------


def _validated(audit: SemanticAudit) -> list[tuple[SemanticAudit, dict]]:
    return [(p, p.validated_patch(json.dumps(_patch(_refs(p))))) for p in audit.partitions()]


def test_four_complete_parts_merge_once_and_equal_one_full_patch():
    audit = _audit(9)
    parts = audit.partitions()
    assert len(parts) == 4
    patches = []
    for part in parts:
        own = _refs(part)
        patches.append(
            (part, part.validated_patch(json.dumps(_patch(own, [_change(own[0])], [own[-1]]))))
        )
    merged = audit.combine_patches(patches)
    changes = [_change(_refs(p)[0]) for p in parts]
    unresolved = [_refs(p)[-1] for p in parts]
    assert merged == audit.parse(json.dumps(_patch(_refs(audit), changes, unresolved)))
    assert merged[1]["reviewed"] == 9 and merged[1]["model_verified"] == 1


def test_combine_rejects_missing_duplicate_and_cross_scope_parts():
    audit = _audit(8)
    good = _validated(audit)
    a, b, c, d = good
    stolen = dict(a[1], verified={**a[1]["verified"], **b[1]["verified"]})
    shrunk = dict(a[1], verified=dict(list(a[1]["verified"].items())[1:]))
    no_map = {k: v for k, v in a[1].items() if k != "verified"}
    for parts in (
        [a, b, c],  # missing part
        [a, b, c, d, d],  # duplicate part
        [a, a, c, d],  # duplicate instead of one part
        [(a[0], stolen), c, d],  # verified crosses into another scope
        [(a[0], stolen), b, c, d],
        [(a[0], shrunk), b, c, d],  # a part's map misses one of its refs
        [(a[0], no_map), b, c, d],
        [(a[0], b[1]), (b[0], a[1]), c, d],  # patches swapped between views
    ):
        with pytest.raises(nd.NotesDocumentError):
            audit.combine_patches(parts)
    audit.combine_patches(good)  # the valid base passes


def test_merged_verified_map_counts_toward_the_unchanged_merge_cap(monkeypatch):
    audit = _audit(8)
    good = _validated(audit)
    without_map = len(json.dumps({"audit_version": 2, "changes": [], "unresolved_refs": []}))
    monkeypatch.setattr(notes_semantic, "MAX_SEMANTIC_OUTPUT_BYTES", without_map + 20)
    with pytest.raises(nd.NotesDocumentError) as failure:
        audit.combine_patches(good)
    assert failure.value.reason_code == "size"


def test_constants_stay_as_contracted():
    assert (AUDIT_VERSION, MAX_AUDIT_PARTS) == (2, 4)
    assert notes_semantic.MAX_SEMANTIC_OUTPUT_BYTES == 64_000


@pytest.mark.parametrize("language", ["en", "vi"])
def test_instructions_ask_for_atomic_bindings_and_every_decision(language):
    text = notes_semantic._INSTRUCTIONS[language]
    for token in ("verified", "'.p'", "unresolved_refs", "Boolean"):
        assert token in text
