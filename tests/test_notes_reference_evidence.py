"""Registered-Gemini reconcile: an inexact quote may cite its declared segment's whole text."""

from __future__ import annotations

import copy
import json

import pytest
from notes_helpers import reconcile_disposition

from asr_studio import notes_document as nd
from asr_studio.notes_reconcile import ReconcileLedger

SEGMENTS = [
    ("s1", "We agreed to ship the pilot in 3 regions."),
    ("s2", "I will send the audit checklist by tomorrow."),
    ("s3", "y" * 501),
]
TEXT = "Pilot ships in 3 regions."


def _ledger(*, on=True):
    source = {
        "meeting_date": "2026-01-05",
        "segments": [
            {"id": sid, "text": text, "start_seconds": i, "end_seconds": i + 1}
            for i, (sid, text) in enumerate(SEGMENTS)
        ],
    }
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["decisions"] = [{"text": "Pilot ships soon.", "source_ids": ["s1"]}]
    return ReconcileLedger.build(source, document, [], strict_names=True, reference_evidence=on)


def _patch(quote, *, sid="s1", ids=("s1",), text=TEXT, addition=False):
    evidence = [{"source_id": sid, "quote": quote}]
    if addition:
        op = {"section": "key_topics", "text": text, "source_ids": list(ids)}
        op |= {"evidence": evidence, "owner": None, "deadline_text": None}
        return {"ledger_version": 2, "dispositions": [], "additions": [op]}
    update = reconcile_disposition(
        "f0", "update", text=text, source_ids=list(ids), evidence=evidence
    )
    return {"ledger_version": 2, "dispositions": [update], "additions": []}


PARAPHRASE = "agreed to ship pilot in three regions"


@pytest.mark.parametrize("addition", [False, True])
def test_inexact_quote_cites_the_whole_declared_segment_only_when_enabled(addition):
    patch = _patch(PARAPHRASE, addition=addition)
    original = copy.deepcopy(patch)
    exact = _ledger().apply(_patch("ship the pilot in 3 regions", addition=addition))
    assert _ledger().apply(patch).document == exact.document  # no new prose, IDs or metadata
    assert patch == original
    with pytest.raises(nd.NotesDocumentError):
        _ledger(on=False).apply(patch)  # the default contract is unchanged


def test_only_the_quote_changes_and_an_exact_quote_is_untouched():
    segments = _ledger()._segments()
    patch = _patch("ship the pilot")
    assert (
        ReconcileLedger._reference_evidence(patch["dispositions"][0], segments)
        == (patch["dispositions"][0])
    )
    rebound = ReconcileLedger._reference_evidence(_patch(PARAPHRASE)["dispositions"][0], segments)
    assert rebound == {
        **_patch(PARAPHRASE)["dispositions"][0],
        "evidence": [{"source_id": "s1", "quote": SEGMENTS[0][1]}],
    }


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(_patch(PARAPHRASE, sid="s9", ids=("s9",)), id="unknown-id"),
        pytest.param(_patch(PARAPHRASE, sid="s2"), id="uncited-id"),
        pytest.param(_patch("x" * 501), id="oversize-quote"),
        pytest.param(_patch("abc"), id="short-quote"),
        pytest.param(_patch("   abc   "), id="blank-padded-quote"),
        pytest.param(_patch(PARAPHRASE, sid="s3", ids=("s3",)), id="oversize-segment"),
        pytest.param(_patch(7), id="wrong-type"),
        pytest.param(_patch(PARAPHRASE, text="Pilot ships in 4 regions."), id="number-check"),
        pytest.param(
            _patch(PARAPHRASE, text="Pilot ships via Quinn in 3 regions."), id="name-check"
        ),
    ],
)
def test_other_evidence_and_grounding_failures_still_fail(patch):
    with pytest.raises(nd.NotesDocumentError):
        _ledger().apply(patch)


def test_primary_plan_repair_and_repair_use_the_same_rebinding():
    ledger = _ledger()
    raw = json.dumps(_patch(PARAPHRASE))
    assert ledger.plan_repair(raw) is None  # nothing left to repair
    assert ledger.parse(raw).document == ledger.apply(_patch("ship the pilot")).document
    plan = ledger.plan_repair(json.dumps(_patch(PARAPHRASE, text="Pilot ships in 4 regions.")))
    assert plan is not None and [f.ref for f in plan.failures] == ["f0"]
    repaired = ledger.parse_repair(raw, plan)
    assert repaired.document == ledger.parse(raw).document
