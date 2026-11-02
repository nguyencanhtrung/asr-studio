"""Presentation rules leave the snapshot and stored sources unchanged."""

from copy import deepcopy

import pytest

from asr_studio.citation_groups import group_citations


def project(segments, ids=None):
    citations = [
        {
            "section": "Overview",
            "text": "Point",
            "sources": [
                {"id": s["id"], "start_seconds": s["start_seconds"]}
                for s in segments
                if ids is None or s["id"] in ids
            ],
        }
    ]
    before = deepcopy((citations, segments))
    result = group_citations(citations, {"segments": segments})[0]["sources"]
    assert (citations, segments) == before
    assert [m["id"] for g in result for m in g.get("members", [g])] == [
        s["id"] for s in segments if ids is None or s["id"] in ids
    ]
    return result


def segments():
    return [
        dict(id=str(i), sequence=i, speaker_id="A", start_seconds=i * 2, end_seconds=i * 2 + 1)
        for i in range(3)
    ]


def test_basic_and_actual_end_and_uncited_barrier():
    result = project(segments())
    assert len(result) == 1
    assert result[0]["end_seconds"] == 5
    assert len(result[0]["members"]) == 3
    assert len(project(segments(), {"0", "2"})) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"speaker_id": "B"},
        {"speaker_id": None},
        {"start_seconds": 0.5},
        {"end_seconds": 2},
        {"start_seconds": float("inf")},
        {"overlap": True},
        {"multiple_speakers": True},
        {"low_dominance": True},
        {"speaker_mismatch": True},
    ],
)
def test_blocked_member_cannot_bridge(patch):
    items = segments()
    items[1].update(patch)
    assert len(project(items)) == 3


def test_gap_and_duration_boundaries():
    items = segments()
    items[1].update(start_seconds=2.01, end_seconds=3.01)
    assert len(project(items)) == 2
    items = segments()
    items[0].update(start_seconds=0, end_seconds=30)
    items[1].update(start_seconds=30, end_seconds=60)
    items[2].update(start_seconds=60, end_seconds=61)
    assert [len(g["members"]) for g in project(items)] == [2, 1]


def test_missing_sequence_falls_back():
    items = segments()
    del items[1]["sequence"]
    assert len(project(items)) == 3


def test_restored_citation_units_keep_all_members_groupable():
    from asr_studio import notes_document as nd

    items = segments()
    snapshot = {"segments": items}
    for item in items:
        item["text"] = "Evidence."
    doc = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    doc["overview"] = [{"text": "Point", "source_ids": ["unit"]}]
    restored = nd.restore_source_refs(doc, {"unit": tuple(s["id"] for s in items)})
    citations = nd.build_citations(restored, snapshot, output_language="en")
    group = group_citations(citations, snapshot)[0]["sources"][0]
    assert [s["id"] for s in group["members"]] == ["0", "1", "2"]
