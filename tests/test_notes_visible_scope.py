"""Semantic audit visible scope: each part sees its own cited pool plus bounded context only."""

from __future__ import annotations

import copy
import json

import pytest
from notes_helpers import (
    reconcile_keep_patch,
    semantic_keep_patch,
    semantic_patch,
    semantic_payload,
    semantic_refs,
)
from test_notes_reconcile import _long_transport
from test_notes_reconcile import _run as _long_chatgpt
from test_notes_semantic import _wide_run, _wide_snapshot

from asr_studio import notes_document as nd
from asr_studio import notes_pipeline, notes_semantic
from asr_studio.notes_semantic import MAX_AUDIT_PARTS, MAX_SEMANTIC_OUTPUT_BYTES, SemanticAudit


def _segment(index: int, start: float, text: str) -> dict:
    return {
        "id": f"{index:032x}",
        "start_seconds": start,
        "end_seconds": start + 1,
        "text": text,
        "speaker_name": "SPEAKER_00",
    }


# 0..9 within a few seconds; 4 opens a new agenda item; 8 starts after a 500 s silence.
_TEXTS = [
    "We start with the library plan.",  # 0
    "The library plan adds reading rooms.",  # 1
    "Staff will hire two librarians.",  # 2
    "That finishes the library plan.",  # 3
    "Agenda item 5 is the park budget.",  # 4 header
    "The park budget funds new benches.",  # 5
    "Benches arrive in spring.",  # 6
    "Questions on the park budget?",  # 7
    "After the break we hear public comment.",  # 8 (500 s later)
    "Public comment closes.",  # 9
]
_STARTS = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 522.0, 525.0]
SOURCE = {
    "meeting_date": "2026-01-05",
    "speakers": [],
    "segments": [
        _segment(k + 1, start, text)
        for k, (start, text) in enumerate(zip(_STARTS, _TEXTS, strict=True))
    ],
}
ID = [segment["id"] for segment in SOURCE["segments"]]


def _document(*pools: list[int]) -> dict:
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": f"Topic {k} reviewed", "source_ids": [ID[i] for i in pool]}
        for k, pool in enumerate(pools)
    ]
    return nd.validate_document(document, SOURCE)


def _audit(*pools: list[int]) -> SemanticAudit:
    return SemanticAudit(SOURCE, _document(*pools))


def _change(ref: str, ids: list[str]) -> dict:
    return {
        "ref": ref,
        "text_ref": f"{ref}.o",
        "add_source_ids": ids,
        "clear_owner": False,
        "clear_deadline": False,
    }


def _shown(audit: SemanticAudit) -> list[int]:
    return [ID.index(s["id"]) for s in audit.visible_snapshot()["segments"]]


def test_a_part_sees_its_pool_and_one_bounded_neighbour_per_cited_run():
    assert _shown(_audit([2])) == [1, 2, 3]  # +-1 only, never +-2
    assert _shown(_audit([1, 2])) == [0, 1, 2, 3]  # one run: neighbours at its edges
    assert _shown(_audit([5])) == [4, 5, 6]  # the agenda header opens this item: shown
    assert _shown(_audit([3])) == [2, 3]  # the next segment opens another agenda item
    assert _shown(_audit([4])) == [4, 5]  # a cited header: the earlier agenda stays hidden
    assert _shown(_audit([7])) == [6, 7]  # a 500 s gap is over the 120 s bound
    assert _shown(_audit([8])) == [8, 9]
    assert _shown(_audit([0, 9])) == [0, 1, 8, 9]  # two runs, every own ID kept


def test_every_owned_id_is_shown_with_global_ids_positions_and_omissions():
    audit = _audit([0], [2, 9], [5], [7])
    for part in audit.partitions():
        payload = json.loads(part.build_prompt().split("AUDIT_INPUT_JSON:\n", 1)[1])
        shown = [s["id"] for s in payload["source"]["segments"]]
        own = {i for item in payload["items"] for i in item["source_ids"]}
        assert own <= set(shown)
        assert shown == [ID[p] for p in payload["source_scope"]["shown_positions"]]
        positions = payload["source_scope"]["shown_positions"]
        assert positions == sorted(positions)  # source order, original indices
        omitted = {
            p
            for a, b in payload["source_scope"]["omitted_position_ranges"]
            for p in range(a, b + 1)
        }
        assert omitted == set(range(10)) - set(positions)  # every gap is explicit
        assert payload["source_scope"]["full_segment_count"] == 10
        assert "never infer continuity" in payload["source_scope"]["note"]
        assert part.source is audit.source  # the guards still read the full source
    second = audit.partitions()[1]
    assert second.source_scope()["omitted_position_ranges"] == [[0, 0], [4, 7]]


def test_no_items_show_the_full_source_and_one_item_shows_its_own_scope():
    empty = _audit()
    assert empty.partitions() == (empty,)
    assert _shown(empty) == list(range(10))  # never an empty snapshot
    assert empty.source_scope()["omitted_position_ranges"] == []
    single = _audit([6])
    assert single.partitions() == (single,)
    assert _shown(single) == [5, 6, 7]
    single.parse(semantic_keep_patch(single))


def test_an_unshown_id_is_rejected_even_when_the_full_source_has_it():
    part = _audit([2], [6]).partitions()[0]
    part.validated_patch(semantic_patch(["i0"], [_change("i0", [ID[3]])]))  # shown neighbour
    for unshown in (ID[0], ID[6], ID[9]):
        with pytest.raises(nd.NotesDocumentError) as failure:
            part.validated_patch(semantic_patch(["i0"], [_change("i0", [unshown])]))
        assert failure.value.reason_code == "source_ids"
    assert ID[0] not in json.dumps(part.build_prompt())  # nor offered by the prompt


def test_combine_rejects_a_part_patch_that_appends_another_parts_visible_id():
    audit = _audit([2], [6])
    first, second = audit.partitions()
    keep = json.loads(semantic_keep_patch(second))
    # ID[6] is valid for the whole audit and for the second part, but the first never saw it.
    foreign = json.loads(semantic_patch(["i0"], [_change("i0", [ID[6]])]))
    audit.parse(semantic_patch(["i0", "i1"], [_change("i0", [ID[6]])]))  # globally valid
    with pytest.raises(nd.NotesDocumentError) as failure:
        audit.combine_patches([(first, foreign), (second, keep)])
    assert failure.value.reason_code == "source_ids"
    own = json.loads(semantic_patch(["i0"], [_change("i0", [ID[1]])]))
    document, counts = audit.combine_patches([(first, own), (second, keep)])
    assert document["key_topics"][0]["source_ids"] == [ID[2], ID[1]] and counts["added_ids"] == 1


def test_strict_context_candidates_are_shown_read_only(monkeypatch):
    calls = []

    def linker(document, snapshot, *, max_refs_per_item, strict, complete_bindings):
        calls.append((strict, complete_bindings, len(snapshot["segments"])))
        linked = copy.deepcopy(document)
        document["key_topics"][0]["source_ids"].append("mutated")  # the input is a copy
        linked["key_topics"][0]["source_ids"].append(ID[8])  # a far candidate is shown
        return linked, {}

    monkeypatch.setattr(notes_semantic, "link_antecedent_context", linker)
    audit = _audit([2])
    assert calls == [(True, True, 10)]  # strict, complete bindings, over the full source
    assert _shown(audit) == [1, 2, 3, 8]  # candidates add no neighbour of their own
    assert audit.document["key_topics"][0]["source_ids"] == [ID[2]]  # nothing published
    document, _counts = audit.parse(semantic_keep_patch(audit))
    assert document["key_topics"][0]["source_ids"] == [ID[2]]


def test_an_unknown_owned_id_fails_the_audit():
    document = _document([2])
    document["key_topics"][0]["source_ids"] = ["f" * 32]
    with pytest.raises(nd.NotesDocumentError) as failure:
        SemanticAudit(SOURCE, document)
    assert failure.value.reason_code == "source_ids"


def test_caps_stay_unchanged():
    assert (MAX_AUDIT_PARTS, MAX_SEMANTIC_OUTPUT_BYTES, nd.MAX_PROVIDER_CALLS) == (8, 64_000, 54)


def test_an_unshown_append_takes_the_shared_repair_in_the_pipeline():
    calls: list[dict] = []
    coverage: list[dict] = []
    ids: dict[str, str] = {}

    def semantic(prompt, kwargs):
        payload = semantic_payload(prompt)
        for segment in payload["source"]["segments"]:
            ids[segment["text"]] = segment["id"]
        texts = [item["text_refs"][0]["text"] for item in payload["items"]]
        if texts == ["Hiring is paused"] and kwargs["attempt"] == "primary":
            # A real source ID the part was not shown (3,581 s away): rejected, then repaired.
            far = [c for c in calls if c["stage"] == "chunk"][-1]["prompt"]
            source = json.JSONDecoder().raw_decode(far.split("SOURCE_JSON:\n", 1)[1])[0]
            unshown = next(s["id"] for s in source["segments"] if s["text"].startswith("Training"))
            assert unshown not in kwargs["allowed_source_ids"]
            ref = semantic_refs(prompt)[0]
            return semantic_patch([ref], [_change(ref, [unshown])])
        return semantic_keep_patch(prompt)

    _wide_run(semantic, calls, coverage)
    attempts = [c["attempt"] for c in calls if c["stage"] == "semantic_audit"]
    assert sorted(attempts) == ["primary"] * 5 + ["repair"]
    assert coverage[0]["semantic_audit"]["part_count"] == 5  # min(8, 5)
    assert len(_wide_snapshot()["segments"]) == 5


def test_final_context_links_complete_bindings_only_for_registered_gemini(monkeypatch):
    seen = []
    original = notes_pipeline.link_antecedent_context

    def linker(document, snapshot, **kwargs):
        seen.append(kwargs)
        return original(document, snapshot, **kwargs)

    monkeypatch.setattr(notes_pipeline, "link_antecedent_context", linker)
    _wide_run(lambda prompt, _kwargs: semantic_keep_patch(prompt), [], [])
    assert seen == [{"max_refs_per_item": 200, "strict": True, "complete_bindings": True}]
    seen.clear()
    _long_chatgpt(_long_transport([], lambda prompt, _kwargs: reconcile_keep_patch(prompt)))
    assert seen == [{"max_refs_per_item": 200, "strict": False, "complete_bindings": False}]


def test_own_ids_are_shown_even_when_the_candidate_copy_drops_them(monkeypatch):
    def linker(document, _snapshot, **_kwargs):
        linked = copy.deepcopy(document)
        linked["key_topics"][0]["source_ids"] = [ID[8]]
        return linked, {}

    monkeypatch.setattr(notes_semantic, "link_antecedent_context", linker)
    assert _shown(_audit([2])) == [1, 2, 3, 8]
