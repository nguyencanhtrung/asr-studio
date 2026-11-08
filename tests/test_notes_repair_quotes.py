from __future__ import annotations

import copy
import json

import pytest
from notes_helpers import reconcile_disposition

from asr_studio import notes_document as nd
from asr_studio.notes_reconcile import ReconcileLedger

QUOTE = "the pilot will ship next month."


def _ledger(*, strict=True, texts=None, ids=None):
    texts = texts or [("s1", "The board said the pilot"), ("s2", "will ship next month.")]
    source = {
        "segments": [
            {"id": sid, "text": text, "start_seconds": i, "end_seconds": i + 1}
            for i, (sid, text) in enumerate(texts)
        ]
    }
    document = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
    document["key_topics"] = [
        {"text": "the pilot will ship next month.", "source_ids": ids or ["s1", "s2"]}
    ]
    return ReconcileLedger.build(source, document, [], strict_names=strict)


def _patch(quote=QUOTE, ref="s2", ids=None):
    return {
        "ledger_version": 2,
        "dispositions": [
            reconcile_disposition(
                "f0",
                "update",
                text="the pilot will ship next month.",
                source_ids=ids or ["s1", "s2"],
                evidence=[{"source_id": ref, "quote": quote}],
            )
        ],
        "additions": [],
    }


def _repair(ledger, patch):
    plan = ledger.plan_repair(json.dumps(_patch("words absent from the source")))
    assert plan is not None
    return ledger.parse_repair(json.dumps(patch), plan)


def test_repair_binds_contiguous_quotes_without_changing_the_primary_or_input():
    ledger, patch = _ledger(), _patch()
    original = copy.deepcopy(patch)
    with pytest.raises(nd.NotesDocumentError):
        ledger.parse(json.dumps(patch))  # The primary still requires per-segment quotes.
    result = _repair(ledger, patch)
    assert result.document["key_topics"] == [
        {"text": "the pilot will ship next month.", "source_ids": ["s1", "s2"]}
    ]
    assert patch == original
    rebound = ledger._bind_repair_quotes(patch["dispositions"][0])
    assert rebound["evidence"] == [
        {"source_id": "s1", "quote": "the pilot"},
        {"source_id": "s2", "quote": "will ship next month."},
    ]


def test_non_strict_repair_keeps_its_previous_exact_segment_contract():
    with pytest.raises(nd.NotesDocumentError):
        _repair(_ledger(strict=False), _patch())


@pytest.mark.parametrize(
    "quote,ref,ids",
    [
        ("the pilot will launch next month.", "s2", ["s1", "s2"]),
        (QUOTE, "missing", ["s1", "s2"]),
        (QUOTE, "s2", ["s2"]),
        (QUOTE, "s3", ["s1", "s2", "s3"]),
    ],
)
def test_repair_cannot_rewrite_words_add_sources_or_ignore_the_declared_source(quote, ref, ids):
    ledger = _ledger(
        texts=[
            ("s1", "The board said the pilot"),
            ("s2", "will ship next month."),
            ("s3", "Other topic."),
        ]
    )
    with pytest.raises(nd.NotesDocumentError):
        _repair(ledger, _patch(quote, ref, ids))


def test_repair_cannot_bridge_a_segment_that_was_not_cited():
    ledger = _ledger(
        texts=[
            ("s1", "The board said the pilot"),
            ("gap", "A distinct statement."),
            ("s2", "will ship next month."),
        ]
    )
    with pytest.raises(nd.NotesDocumentError):
        _repair(ledger, _patch())


def test_ambiguous_cross_boundary_quote_is_not_bound():
    ledger = _ledger(
        texts=[
            ("s1", "the pilot"),
            ("s2", "will ship next month. the pilot"),
            ("s3", "will ship next month."),
        ]
    )
    with pytest.raises(nd.NotesDocumentError):
        _repair(ledger, _patch(ids=["s1", "s2", "s3"]))


def test_quote_binding_keeps_the_existing_evidence_count_limit():
    patch = _patch()
    patch["dispositions"][0]["evidence"][:0] = [{"source_id": "s1", "quote": "pilot"}] * 7
    with pytest.raises(nd.NotesDocumentError):
        _repair(_ledger(), patch)


def test_quote_binding_keeps_the_minimum_fragment_length():
    ledger = _ledger(texts=[("s1", "The start of"), ("s2", "the pilot proposal.")])
    with pytest.raises(nd.NotesDocumentError):
        _repair(ledger, _patch("of the pilot proposal."))


def test_quote_binding_keeps_the_raw_quote_limit_before_nfc_normalization():
    part = "e\u0301" * 130
    ledger = _ledger(texts=[("s1", part), ("s2", part)])
    with pytest.raises(nd.NotesDocumentError):
        _repair(ledger, _patch(part + " " + part))
