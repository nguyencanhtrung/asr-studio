"""Partitioned reconcile views: ownership, read-only context and the combined result."""

from __future__ import annotations

import hashlib
import json

import pytest
from notes_helpers import reconcile_disposition

from asr_studio import notes_document as nd
from asr_studio import notes_reconcile as nr
from asr_studio.notes_reconcile import ReconcileLedger, ReconcileResult

_TEXTS = (
    ("We agreed to ship the pilot in 3 regions.", "Avery"),
    ("I will send the audit checklist by tomorrow.", "Rowan"),
    ("Budget review moves to the finance board on Friday.", "Avery"),
    ("The vendor contract renews for 2 years.", "Rowan"),
    ("Training sessions start next month for 40 staff.", "Avery"),
    ("The help desk keeps weekend hours.", "Rowan"),
)
SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": f"s{index + 1}",
            "start_seconds": 4.0 * index,
            "end_seconds": 4.0 * index + 4.0,
            "text": text,
            "speaker_name": speaker,
        }
        for index, (text, speaker) in enumerate(_TEXTS)
    ],
}


def _doc(**sections):
    return {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}, **sections}


def _item(text, *ids):
    return {"text": text, "source_ids": list(ids)}


FINAL = _doc(
    decisions=[_item("Pilot ships in 3 regions.", "s1")],
    action_items=[
        {
            **_item("Send audit checklist.", "s2"),
            "owner": None,
            "deadline_text": None,
            "deadline_date": None,
        }
    ],
)
MAPS = [
    _doc(key_topics=[_item("Budget review at finance board.", "s3")]),
    _doc(
        decisions=[_item("Vendor contract renews for 2 years.", "s4")],
        key_topics=[_item("Training for 40 staff.", "s5"), _item("Help desk weekend hours.", "s6")],
    ),
]


def _ledger(final=FINAL, maps=MAPS, **flags):
    return ReconcileLedger.build(SOURCE, final, maps, **flags)


def _patch(dispositions=(), additions=()):
    return json.dumps(
        {"ledger_version": 2, "dispositions": list(dispositions), "additions": list(additions)}
    )


def _addition(section="key_topics", text="Help desk keeps weekend hours.", sid="s6"):
    quote = SOURCE["segments"][int(sid[1:]) - 1]["text"]
    return {
        "section": section,
        "text": text,
        "source_ids": [sid],
        "evidence": [{"source_id": sid, "quote": quote}],
        "owner": None,
        "deadline_text": None,
    }


def _digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _bytes(document):
    return len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode())


def _ordinary_digests():
    ledger = _ledger()
    strict = _ledger(strict_names=True, max_unit_size=2)
    return {
        "schema": _digest(ledger.schema()),
        "payload": _digest(ledger.prompt_payload()),
        "prompt_en": _digest(ledger.build_prompt()),
        "prompt_vi": _digest(ledger.build_prompt(output_language="vi")),
        "strict_prompt": _digest(strict.build_prompt()),
        "strict_schema": _digest(strict.schema()),
        "parse": _digest(ledger.parse(_patch(additions=[_addition()])).document),
        "repair_prompt": _digest(ledger.build_repair_prompt(ledger.plan_repair(_BAD_UPDATE))),
    }


_BAD_UPDATE = _patch(
    [reconcile_disposition("f0", "update", text="Pilot ships.", source_ids=["s1"])]
)


# Captured from the pre-partition implementation: ordinary ledgers stay byte-identical.
ORDINARY_DIGESTS = {
    "schema": "b625a977ff2116bd17a8bcb462a943df349935dc951d9b4c8d8a187369b71ab7",
    "payload": "a48d61543fb4d0fd6610b8dd67685a5d374296ba7aff72f01b5fdb115cb420da",
    "prompt_en": "a4f1aad043df5c81879d931fdaafd77c9400fc5173f01f42d509ffdbe9068397",
    "prompt_vi": "77718357d673c4d2d0dc558903dd2f041fd0501a3c502ed655d31a2b41468885",
    "strict_prompt": "7312a98a859b597d26fbe4fc14b82c7a2b7a0b3dbec0ec5177644127087548f9",
    "strict_schema": "b573c87cdb7333d9cf87e5f9941fb8fd5a7e78f3b13832fb1e9cbf157a64967e",
    "parse": "24694226dc80e03005ea834790d8e0c6d93ad04d61ebce0b7e945adb29e707ab",
    "repair_prompt": "c8f481d4c0514664241799f464fbd7a71c05152aa81db3a493ea724420dad57b",
}


def test_ordinary_ledger_payload_schema_prompt_and_parse_are_byte_identical():
    assert _ordinary_digests() == ORDINARY_DIGESTS
    payload = _ledger().prompt_payload()
    assert "read_only_items" not in payload
    assert "Split ledger" not in _ledger().build_prompt()


def test_partitions_are_two_balanced_contiguous_views_over_the_full_source():
    ledger = _ledger(strict_names=True, reference_evidence=True, max_unit_size=2)
    views = ledger.partitions()
    assert nr.MAX_RECONCILE_PARTS == 2 and len(views) == 2
    assert [view.refs for view in views] == [("f0", "f1", "m0"), ("m1", "m2", "m3")]
    assert views[0].refs + views[1].refs == ledger.refs  # stable global refs, in order
    for index, view in enumerate(views):
        assert view.source == ledger.source
        assert view.prompt_payload()["source"] == ledger.prompt_payload()["source"]
        assert (view.strict_names, view.reference_evidence, view.max_unit_size) == (True, True, 2)
        assert view.context == views[1 - index].entries  # read-only context: the other entries
        assert view.part == (index, 2)
    assert [view.additions_allowed for view in views] == [True, False]
    odd = _ledger(maps=MAPS[:1]).partitions()
    assert [len(view.entries) for view in odd] == [2, 1]


def test_view_payload_shows_other_entries_read_only_without_refs():
    first, second = _ledger().partitions()
    payload = second.prompt_payload()
    assert [item["ref"] for item in payload["map_items"]] == ["m1", "m2", "m3"]
    assert payload["final_items"] == []
    assert payload["read_only_items"] == [
        {"section": "decisions", **FINAL["decisions"][0]},
        {"section": "action_items", **FINAL["action_items"][0]},
        {"section": "key_topics", **MAPS[0]["key_topics"][0]},
    ]
    for view, phrase in ((first, "additions are allowed only"), (second, "additions must be")):
        prompt = view.build_prompt()
        assert "another request owns read_only_items" in prompt and phrase in prompt
        assert "chỉ đọc" in view.build_prompt(output_language="vi")
    repair = second.plan_repair(_patch([reconcile_disposition("m1", "update", text="x")]))
    assert "read_only_items" in second.build_repair_prompt(repair)


def test_view_merge_targets_are_only_owned_refs():
    final = _doc(key_topics=[_item("Help desk weekend hours.", "s6")])
    views = _ledger(final=final, maps=MAPS).partitions()
    targets = {ref: t for view in views for ref, t in view.merge_targets().items()}
    unsplit = _ledger(final=final, maps=MAPS).merge_targets()
    assert unsplit["m2"] == ["f0"] and targets["m2"] == []  # f0 is in the other view


def test_only_the_first_view_allows_additions():
    first, second = _ledger().partitions()
    schema = lambda view: view.schema()["properties"]["additions"]["maxItems"]  # noqa: E731
    assert (schema(_ledger()), schema(first), schema(second)) == (16, 16, 0)
    assert first.parse(_patch(additions=[_addition()])).counts["additions"] == 1
    with pytest.raises(nd.NotesDocumentError, match="only the first view"):
        second.parse(_patch(additions=[_addition()]))
    assert second.plan_repair(_patch(additions=[_addition()])) is None


def test_later_view_repair_rejects_additions():
    second = _ledger().partitions()[1]
    plan = second.plan_repair(_patch([reconcile_disposition("m1", "update", text="x")]))
    assert plan.refs == ("m1",) and plan.addition_slots == 0
    fixed = reconcile_disposition("m1")
    with pytest.raises(nd.NotesDocumentError, match="only the first view"):
        second.parse_repair(_patch([fixed], [_addition()]), plan)
    assert second.parse_repair(_patch([fixed]), plan).counts["map"]["keep"] == 3


@pytest.mark.parametrize(
    "disposition",
    [
        reconcile_disposition("f0", "remove", reason="unsupported"),  # owned by the first view
        reconcile_disposition("m0", "update", text="Budget review.", source_ids=["s3"]),
    ],
)
def test_a_view_cannot_dispose_a_read_only_ref(disposition):
    second = _ledger().partitions()[1]
    with pytest.raises(nr.ReconcilePatchError, match="read-only ref") as caught:
        second.parse(_patch([disposition]))
    assert [f.ref for f in caught.value.failures] == [disposition["ref"]]


@pytest.mark.parametrize("action", ["merge", "remove"])
def test_a_view_cannot_merge_into_a_read_only_target(action):
    final = _doc(key_topics=[_item("Help desk weekend hours.", "s6")])
    ledger = _ledger(final=final, maps=MAPS)
    reason = "duplicate" if action == "remove" else "none"
    unsplit = _patch([reconcile_disposition("m2", action, target="f0", reason=reason)])
    assert ledger.parse(unsplit).counts["map"][action] == 1
    second = ledger.partitions()[1]
    assert "m2" in second.refs and "f0" not in second.refs
    with pytest.raises(nr.ReconcilePatchError, match="another valid target"):
        second.parse(_patch([reconcile_disposition("m2", action, target="f0", reason=reason)]))


def test_empty_and_single_entry_ledgers_are_their_own_only_view():
    for ledger in (_ledger(final=_doc(), maps=[]), _ledger(final=_doc(), maps=MAPS[:1])):
        (view,) = ledger.partitions()
        assert view is ledger
        result = view.parse(_patch())
        assert ledger.combine_results([(view, result)]) == result
    with pytest.raises(nd.NotesDocumentError, match="partitioned again"):
        _ledger().partitions()[0].partitions()


def _results(ledger, patches=None):
    views = ledger.partitions()
    patches = patches or [_patch()] * len(views)
    return [(view, view.parse(patch)) for view, patch in zip(views, patches, strict=True)]


def test_combined_empty_patches_equal_the_unsplit_result():
    ledger = _ledger()
    combined = ledger.combine_results(_results(ledger))
    assert combined == ledger.parse(_patch())


def test_combine_merges_sections_and_sums_counts():
    ledger = _ledger()
    update = reconcile_disposition(
        "m1",
        "update",
        text="Training starts next month for 40 staff.",
        source_ids=["s5"],
        evidence=[{"source_id": "s5", "quote": "Training sessions start next month for 40 staff."}],
    )
    patches = (
        _patch([reconcile_disposition("m0", "remove", reason="nonmaterial")], [_addition()]),
        _patch([update, reconcile_disposition("m2", "remove", reason="nonmaterial")]),
    )
    combined = ledger.combine_results(_results(ledger, patches))
    document = combined.document
    assert document["schema_version"] == 1
    assert document["decisions"] == FINAL["decisions"] + MAPS[1]["decisions"]
    assert [item["text"] for item in document["key_topics"]] == [
        "Help desk keeps weekend hours.",  # first view's addition
        "Training starts next month for 40 staff.",
    ]
    assert combined.counts == {
        "final": {"keep": 2, "update": 0, "merge": 0, "remove": 0, "confirm": 0},
        "map": {"keep": 1, "update": 1, "merge": 0, "remove": 2, "confirm": 0},
        "additions": 1,
        "default_kept": 3,
    }


def _foreign(ledger):
    first, second = _results(ledger)
    other = _ledger(strict_names=True)
    other_view = other.partitions()[1]
    moved = dict(SOURCE, meeting_date="2026-02-01")
    foreign_source = ReconcileLedger.build(moved, FINAL, MAPS).partitions()[1]
    return {
        "missing": [first],
        "duplicate": [first, first],
        "reordered": [second, first],
        "flags": [first, (other_view, other_view.parse(_patch()))],
        "source": [first, (foreign_source, foreign_source.parse(_patch()))],
        "swapped_result": [(first[0], second[1]), (second[0], first[1])],
        "not_pair": [first, second[1]],
        "unsplit": [(ledger, ledger.parse(_patch())), second],
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "reordered",
        "flags",
        "source",
        "swapped_result",
        "not_pair",
        "unsplit",
    ],
)
def test_combine_rejects_foreign_missing_duplicate_or_reordered_parts(case):
    ledger = _ledger()
    with pytest.raises(nd.NotesDocumentError):
        ledger.combine_results(_foreign(ledger)[case])


def _tampered(ledger, mutate):
    parts = _results(ledger)
    view, result = parts[1]
    document, counts = (
        json.loads(json.dumps(result.document)),
        json.loads(json.dumps(result.counts)),
    )
    mutate(document, counts)
    parts[1] = (view, ReconcileResult(document=document, counts=counts))
    return parts


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d, c: d["key_topics"].append(_item("Extra item.", "s6")), "outside its own scope"),
        (
            lambda d, c: (c.update(additions=1), d["key_topics"].append(_item("Extra.", "s6"))),
            "only the first view",
        ),
        (lambda d, c: c["map"].update(keep=2), "own entries"),
        (lambda d, c: c["final"].update(keep=1), "own entries"),
        (lambda d, c: c.update(default_kept=7), "outside its own scope"),
        (lambda d, c: c.pop("default_kept"), "invalid counts"),
        (lambda d, c: c["map"].update(keep=True), "invalid counts"),
        (lambda d, c: d["key_topics"][0].update(source_ids=["s9"]), "."),
        (lambda d, c: d.update(schema_version=2), "version"),
    ],
)
def test_combine_rejects_out_of_scope_or_invalid_view_results(mutate, match):
    ledger = _ledger()
    with pytest.raises(nd.NotesDocumentError, match=match):
        ledger.combine_results(_tampered(ledger, mutate))


def test_combined_document_is_revalidated_against_output_and_reference_caps(monkeypatch):
    ledger = _ledger()
    parts = _results(ledger)
    sizes = [_bytes(result.document) for _view, result in parts]
    combined = _bytes(ledger.combine_results(parts).document)
    monkeypatch.setattr(nd, "MAX_OUTPUT_BYTES", combined - 1)
    assert max(sizes) < combined - 1
    with pytest.raises(nd.NotesDocumentError, match="output budget"):
        ledger.combine_results(parts)
    monkeypatch.undo()
    capped = _ledger(max_unit_size=nd.MAX_SOURCE_REFS_PER_ITEM)  # one source ref per item
    parts = _results(capped)
    view, result = parts[0]
    document = json.loads(json.dumps(result.document))
    document["decisions"][0]["source_ids"] = ["s1", "s2"]
    with pytest.raises(nd.NotesDocumentError):
        capped.combine_results([(view, ReconcileResult(document, result.counts)), parts[1]])
