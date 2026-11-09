"""Required ``cue_checked`` decisions of a clause-cue reconcile ledger and its targeted repair.

``cue_checked`` records the model's decision for every cued ref; it is decision coverage only,
never proof that an item is true.
"""

from __future__ import annotations

import json

import pytest
from notes_helpers import reconcile_disposition, reconcile_payload

from asr_studio import notes_document as nd
from asr_studio import notes_reconcile as nr
from asr_studio.notes_reconcile import ReconcileLedger

_TEXTS = (
    (
        "The extension is needed because the review has not been completed yet.",
        "Extension requested",
    ),
    (
        "Three departments are preparing the impact report. They will report back in October.",
        "Impact report being prepared by three departments",
    ),
    ("Yes, thank you so much. Okay.", "Meeting closed"),
)
SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": f"s{index + 1}",
            "start_seconds": 5.0 * index,
            "end_seconds": 5.0 * index + 5.0,
            "text": text,
            "speaker_name": f"SPEAKER_{index:02d}",
        }
        for index, (text, _item) in enumerate(_TEXTS)
    ],
}


def _doc(**sections):
    return {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}, **sections}


MAPS = [
    _doc(
        key_topics=[
            {"text": item, "source_ids": [f"s{index + 1}"]}
            for index, (_text, item) in enumerate(_TEXTS)
        ]
    )
]
DECLINE = {"m0": False, "m1": False}


def _ledger(**flags):
    return ReconcileLedger.build(SOURCE, _doc(), MAPS, include_clause_cues=True, **flags)


def _patch(*dispositions, cue_checked=None):
    patch = {"ledger_version": 2, "dispositions": list(dispositions), "additions": []}
    if cue_checked is not None:
        patch["cue_checked"] = cue_checked
    return json.dumps(patch)


def _update_m0():  # adds the cued cause clause with an exact quote
    return reconcile_disposition(
        "m0",
        "update",
        text="Extension requested because review not completed yet",
        source_ids=["s1"],
        evidence=[{"source_id": "s1", "quote": "because the review has not been completed"}],
    )


def _bad_update(ref, sid):  # an invented quote fails only this ref
    return reconcile_disposition(
        ref,
        "update",
        text=dict(MAPS[0]["key_topics"][int(ref[1:])])["text"],
        source_ids=[sid],
        evidence=[{"source_id": sid, "quote": "an invented quote"}],
    )


def _failures(ledger, raw):
    with pytest.raises(nr.ReconcilePatchError) as caught:
        ledger.parse(raw)
    return {failure.ref: failure.message for failure in caught.value.failures}


def test_fixture_cues_exactly_the_first_two_refs():
    assert list(_ledger().clause_cues()) == ["m1", "m0"]  # ranked: date before cause
    assert _ledger()._cued_refs() == ("m0", "m1")  # ledger order


# ----- primary validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cue_checked", "failing"),
    [
        (None, {"m0", "m1"}),  # map missing: every cued ref is undecided
        ({"m0": False}, {"m1"}),  # one key missing
        ({**DECLINE, "m2": False}, {"m2"}),  # extra owned ref without cues
        ({"m0": 0, "m1": False}, {"m0"}),  # not a Boolean
        ({"m0": "false", "m1": None}, {"m0", "m1"}),
        ({"m0": True, "m1": False}, {"m0"}),  # true without an update
    ],
)
def test_missing_extra_non_boolean_or_unsupported_true_decisions_fail_their_refs(
    cue_checked, failing
):
    assert set(_failures(_ledger(), _patch(cue_checked=cue_checked))) == failing


@pytest.mark.parametrize("action", ["confirm", "remove"])
def test_true_requires_an_update_not_another_disposition(action):
    reason = "nonmaterial" if action == "remove" else "none"
    disposition = reconcile_disposition("m0", action, reason=reason)
    failures = _failures(_ledger(), _patch(disposition, cue_checked={"m0": True, "m1": False}))
    assert failures == {"m0": "cue_checked true requires an update of this ref."}


@pytest.mark.parametrize("cue_checked", [[], "m0", {"m0": False, "m1": False, "zz": False}])
def test_a_non_object_map_or_a_key_outside_the_ledger_is_a_global_failure(cue_checked):
    ledger = _ledger()
    raw = _patch(cue_checked=cue_checked)
    with pytest.raises(nd.NotesDocumentError, match="cue_checked map is invalid"):
        ledger.parse(raw)
    assert ledger.plan_repair(raw) is None  # the bounded generic repair handles it


def test_a_read_only_ref_of_another_view_cannot_be_decided():
    first, *_rest = _ledger().partitions()
    assert first.refs == ("m0",)
    with pytest.raises(nd.NotesDocumentError, match="cue_checked map is invalid"):
        first.parse(_patch(cue_checked={"m0": False, "m1": False}))


def test_false_allows_unlisted_or_any_disposition_and_true_with_an_update_applies():
    ledger = _ledger()
    declined = ledger.parse(_patch(cue_checked=DECLINE))
    assert declined.counts["cue_checked"] == {"checked": 2, "accepted": 0, "declined": 2}
    removed = ledger.parse(
        _patch(reconcile_disposition("m1", "remove", reason="nonmaterial"), cue_checked=DECLINE)
    )
    assert removed.counts["map"]["remove"] == 1
    accepted = ledger.parse(_patch(_update_m0(), cue_checked={"m0": True, "m1": False}))
    assert accepted.counts["cue_checked"] == {"checked": 2, "accepted": 1, "declined": 1}
    assert accepted.document["key_topics"][0]["text"].endswith("review not completed yet")


def test_view_cue_counts_sum_and_are_checked_on_combine():
    ledger = _ledger()
    views = ledger.partitions()
    assert [view._cued_refs() for view in views] == [("m0",), ("m1",), ()]
    patches = [
        _patch(_update_m0(), cue_checked={"m0": True}),
        _patch(cue_checked={"m1": False}),
        _patch(),
    ]
    parts = [(view, view.parse(patch)) for view, patch in zip(views, patches, strict=True)]
    combined = ledger.combine_results(parts)
    assert combined.counts["cue_checked"] == {"checked": 2, "accepted": 1, "declined": 1}
    view, result = parts[1]
    forged = {**result.counts, "cue_checked": {"checked": 0, "accepted": 0, "declined": 0}}
    with pytest.raises(nd.NotesDocumentError, match="cue counts"):
        ledger.combine_results(
            [parts[0], (view, nr.ReconcileResult(result.document, forged)), parts[2]]
        )


# ----- targeted repair ------------------------------------------------------------------------


def test_a_normal_disposition_repair_retains_every_accepted_cue_decision():
    ledger = _ledger()
    primary = _patch(_bad_update("m2", "s3"), cue_checked=DECLINE)
    repair = ledger.plan_repair(primary)
    assert repair.refs == ("m2",) and dict(repair.cue_checked) == DECLINE
    # m2 has no cue: the repair sees no cue, no cue rule and no cue_checked schema.
    assert "cue_checked" not in ledger.schema(repair)["properties"]
    prompt = ledger.build_repair_prompt(repair)
    assert "unstated_clauses" not in prompt.split("REPAIR_INPUT_JSON:\n", 1)[1]
    assert nr._REPAIR_CUE_RULE["en"] not in prompt
    with pytest.raises(nd.NotesDocumentError, match="invalid fields"):
        ledger.parse_repair(_patch(reconcile_disposition("m2", "confirm"), cue_checked={}), repair)
    result = ledger.parse_repair(_patch(reconcile_disposition("m2", "confirm")), repair)
    assert result.counts["cue_checked"] == {"checked": 2, "accepted": 0, "declined": 2}


def test_a_failing_cued_ref_is_redecided_with_only_its_own_cues_visible(monkeypatch):
    ledger = _ledger()
    primary = _patch(_bad_update("m0", "s1"), cue_checked={"m0": True, "m1": False})
    repair = ledger.plan_repair(primary)
    assert repair.refs == ("m0",) and dict(repair.cue_checked) == {"m1": False}
    assert ledger.schema(repair)["properties"]["cue_checked"]["required"] == ["m0"]
    checked = []
    real_check = nd._check_prompt
    monkeypatch.setattr(
        nd,
        "_check_prompt",
        lambda prompt, **kwargs: (
            checked.append(kwargs["response_schema"]) or real_check(prompt, **kwargs)
        ),
    )
    prompt = ledger.build_repair_prompt(repair)
    assert checked == [ledger.schema(repair)]  # the budget counts the repair's own schema
    payload = json.JSONDecoder().raw_decode(prompt.split("REPAIR_INPUT_JSON:\n", 1)[1])[0]
    (failure,) = payload["failures"]
    assert failure["attention"]["unstated_clauses"] == ledger.clause_cues()["m0"]
    assert {s["id"] for s in payload["source"]["segments"]} == {"s1"}  # m1's cue is invisible
    assert "October" not in prompt.split("REPAIR_INPUT_JSON:\n", 1)[1]
    assert prompt.count(nr._CLAUSE_CUE_RULE["en"]) == 1
    assert prompt.count(nr._REPAIR_CUE_RULE["en"]) == 1
    # The repair may decide only its failing cued refs, and must decide each of them.
    for decisions in (None, {}, {"m0": True, "m1": False}):
        with pytest.raises(nd.NotesDocumentError, match="exactly its failing cued"):
            ledger.parse_repair(_patch(_update_m0(), cue_checked=decisions), repair)
    with pytest.raises(nr.ReconcilePatchError, match="requires an update"):
        ledger.parse_repair(
            _patch(reconcile_disposition("m0", "confirm"), cue_checked={"m0": True}), repair
        )
    result = ledger.parse_repair(_patch(_update_m0(), cue_checked={"m0": True}), repair)
    assert result.counts["cue_checked"] == {"checked": 2, "accepted": 1, "declined": 1}


def test_missing_cue_decisions_are_recoverable_by_the_one_repair():
    ledger = _ledger()
    repair = ledger.plan_repair(_patch())  # no cue_checked at all
    assert repair.refs == ("m0", "m1") and dict(repair.cue_checked) == {}
    assert ledger.schema(repair)["properties"]["cue_checked"]["required"] == ["m0", "m1"]
    fixed = _patch(
        _update_m0(),
        reconcile_disposition("m1", "confirm"),
        cue_checked={"m0": True, "m1": False},
    )
    result = ledger.parse_repair(fixed, repair)
    assert result.counts["cue_checked"] == {"checked": 2, "accepted": 1, "declined": 1}


def test_flag_off_repair_prompt_and_schema_are_unchanged():
    off = ReconcileLedger.build(SOURCE, _doc(), MAPS)
    repair = off.plan_repair(_patch(_bad_update("m0", "s1")))
    assert off.schema(repair) == off.schema() and "cue_checked" not in off.schema()["properties"]
    prompt = off.build_repair_prompt(repair)
    assert "unstated_clauses" not in prompt and "cue_checked" not in prompt
    assert nr._CORRECTION_RULE["en"] not in prompt


# ----- a synthetic proposed -> recorded status update through the real parser -----------------

_STATUS_SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": "s1",
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "text": "The committee recommends confirming the appointment of the director.",
            "speaker_name": "SPEAKER_00",
        },
        {
            "id": "s2",
            "start_seconds": 60.0,
            "end_seconds": 65.0,
            "text": "The motion carries and the appointment is confirmed.",
            "speaker_name": "SPEAKER_01",
        },
    ],
}
_STALE = "Committee recommended director appointment; council vote pending"
_OUTCOME_EVIDENCE = [
    {"source_id": "s1", "quote": "The committee recommends confirming the appointment"},
    {"source_id": "s2", "quote": "the appointment is confirmed"},
]


def _status_ledger(*extra):
    items = [{"text": _STALE, "source_ids": ["s1"]}, *extra]
    return ReconcileLedger.build(
        _STATUS_SOURCE, _doc(), [_doc(decisions=items)], include_clause_cues=True
    )


def _status_patch(ledger, disposition):
    return _patch(disposition, cue_checked=dict.fromkeys(ledger.clause_cues(), False) or None)


def test_a_stale_pending_status_is_updated_with_the_later_cited_outcome():
    ledger = _status_ledger()
    prompt = ledger.build_prompt()
    assert prompt.count(nr._CORRECTION_RULE["en"]) == 1
    assert [s["id"] for s in reconcile_payload(prompt)["source"]["segments"]] == ["s1", "s2"]
    update = reconcile_disposition(
        "m0",
        "update",
        text="Committee recommended director appointment, previously pending; later confirmed",
        source_ids=["s1", "s2"],
        evidence=_OUTCOME_EVIDENCE,
    )
    (decision,) = ledger.parse(_status_patch(ledger, update)).document["decisions"]
    assert decision["text"].endswith("later confirmed") and decision["source_ids"] == ["s1", "s2"]


def test_a_proposed_record_later_rejected_keeps_proposed_as_a_historical_qualification():
    source = {
        "meeting_date": "2026-01-05",
        "segments": [
            {
                "id": "s1",
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "text": "Staff proposed a new parking fee for the downtown garage.",
                "speaker_name": "SPEAKER_00",
            },
            {
                "id": "s2",
                "start_seconds": 90.0,
                "end_seconds": 95.0,
                "text": "The board voted to reject the parking fee.",
                "speaker_name": "SPEAKER_01",
            },
        ],
    }
    maps = [_doc(decisions=[{"text": "Parking fee proposed for garage", "source_ids": ["s1"]}])]
    ledger = ReconcileLedger.build(source, _doc(), maps, include_clause_cues=True)
    update = reconcile_disposition(
        "m0",
        "update",
        text="Parking fee for garage previously proposed; later rejected by board",
        source_ids=["s1", "s2"],
        evidence=[
            {"source_id": "s1", "quote": "Staff proposed a new parking fee"},
            {"source_id": "s2", "quote": "The board voted to reject the parking fee."},
        ],
    )
    (decision,) = ledger.parse(_status_patch(ledger, update)).document["decisions"]
    assert "previously proposed" in decision["text"] and decision["source_ids"] == ["s1", "s2"]
    deleted = dict(update, text="Parking fee for garage rejected by board")
    with pytest.raises(nr.ReconcilePatchError, match="certainty or polarity marker"):
        ledger.parse(_status_patch(ledger, deleted))


def test_the_marker_guard_still_blocks_deleting_the_stale_status_word():
    # Known v40 limit, reported rather than widened: an update keeps every original certainty
    # marker (pending, proposed, may, ...); the prompt asks for "previously pending ...; later".
    ledger = _status_ledger()
    update = reconcile_disposition(
        "m0",
        "update",
        text="Committee recommended director appointment; appointment confirmed",
        source_ids=["s1", "s2"],
        evidence=_OUTCOME_EVIDENCE,
    )
    with pytest.raises(nr.ReconcilePatchError, match="certainty or polarity marker"):
        ledger.parse(_status_patch(ledger, update))


def test_a_stale_pending_item_can_be_removed_as_superseded_by_the_later_outcome():
    ledger = _status_ledger({"text": "Appointment confirmed", "source_ids": ["s2"]})
    remove = reconcile_disposition(
        "m0",
        "remove",
        reason="superseded",
        source_ids=["s2"],
        evidence=[_OUTCOME_EVIDENCE[1]],
    )
    result = ledger.parse(_status_patch(ledger, remove))
    assert [item["text"] for item in result.document["decisions"]] == ["Appointment confirmed"]


# ----- pipeline: the one shared repair ------------------------------------------------------


def _snapshot(count):
    # One quarter-hour fact part per segment; every map item carries a clause cue.
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            {
                "id": f"{index + 1:032x}",
                "start_seconds": 1_000.0 * index,
                "end_seconds": 1_000.0 * index + 1,
                "text": f"Topic {index} plan waits. The decision is delayed because costs rose.",
                "speaker_name": "Morgan",
            }
            for index in range(count)
        ],
    }


def _map_reply(prompt):
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    owned = [s for s in source["segments"] if not s.get("overlap")]
    document = _doc()
    first = owned[0]["text"].split(". ")[0].split(" is ")[0]
    document["key_topics"] = [{"text": first, "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


def _run(omit_cues_in, count=5):
    """A primary omits the cue decisions of the refs in ``omit_cues_in``."""
    from notes_helpers import reconcile_keep_patch, reconcile_repair_reply, semantic_keep_patch

    from asr_studio.notes_pipeline import generate_notes_document

    calls, coverage = [], []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return semantic_keep_patch(prompt)
        if kwargs["attempt"] == "repair":
            return reconcile_repair_reply(prompt)
        patch = json.loads(reconcile_keep_patch(prompt))
        decisions = {k: v for k, v in patch["cue_checked"].items() if k not in omit_cues_in}
        patch["cue_checked"] = decisions
        if not decisions:
            patch.pop("cue_checked")
        return json.dumps(patch)

    error = None
    try:
        generate_notes_document(
            snapshot=_snapshot(count),
            output_language="en",
            provider="antigravity",
            model="gemini-3.8-flash",
            transport=transport,
            invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
            limit_error=RuntimeError,
            on_coverage=coverage.append,
        )
    except AssertionError as failure:
        error = failure.args[0]
    except RuntimeError as failure:  # a sibling view's failure aborted this request
        error = str(failure)
    return calls, coverage, error


def test_missing_cue_decisions_of_one_view_recover_under_the_shared_repair():
    calls, coverage, error = _run({"m0"})  # views m0+m1, m2, m3, m4
    assert error is None
    repairs = [c for c in calls if c["stage"] == "reconcile" and c["attempt"] == "repair"]
    (repair,) = repairs
    # The repair schema and prompt decide only that view's failing cued ref.
    assert repair["response_schema"]["properties"]["cue_checked"]["required"] == ["m0"]
    payload = json.JSONDecoder().raw_decode(repair["prompt"].split("REPAIR_INPUT_JSON:\n", 1)[1])[0]
    assert [f["ref"] for f in payload["failures"]] == ["m0"]
    assert "unstated_clauses" in payload["failures"][0]["attention"]
    assert "m1" not in json.dumps(payload["failures"])  # m1's accepted decision stays unseen
    primaries = [c for c in calls if c["stage"] == "reconcile" and c["attempt"] == "primary"]
    required = sorted(
        c["response_schema"]["properties"]["cue_checked"]["required"] for c in primaries
    )
    assert required == [["m0", "m1"], ["m2"], ["m3"], ["m4"]]
    counts = coverage[0]["source_audit"]["counts"]["cue_checked"]
    assert counts == {"checked": 5, "accepted": 0, "declined": 5}  # decisions, not truth


def test_two_views_missing_cue_decisions_recover_under_one_pooled_repair():
    calls, coverage, error = _run({"m0", "m2"})  # two different views fail
    assert error is None  # v41: both failed views share the one repair call
    (repair,) = [c for c in calls if c["stage"] == "reconcile" and c["attempt"] == "repair"]
    parts = repair["response_schema"]["properties"]
    assert list(parts) == ["part_0", "part_1"]
    assert [parts[key]["properties"]["cue_checked"]["required"] for key in parts] == [
        ["m0"],
        ["m2"],
    ]
    counts = coverage[0]["source_audit"]["counts"]["cue_checked"]
    assert counts == {"checked": 5, "accepted": 0, "declined": 5}
