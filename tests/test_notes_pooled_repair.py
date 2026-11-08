"""v41: the failed views of a split Gemini reconcile share the task's one repair call."""

from __future__ import annotations

import json
import threading

import pytest
from notes_helpers import (
    reconcile_keep_patch,
    reconcile_payload,
    reconcile_repair_reply,
    semantic_keep_patch,
)

from asr_studio import notes_document as nd
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.notes_reconcile import (
    BUNDLE_PART_MARKER,
    MAX_RECONCILE_OUTPUT_BYTES,
    ReconcileLedger,
    ReconcileRepairBundle,
)

GEMINI = ("antigravity", "gemini-3.8-flash")


def _empty() -> dict:
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _snapshot(count: int) -> dict:
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


def _map_reply(prompt: str) -> str:
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    owned = [s for s in source["segments"] if not s.get("overlap")]
    document = _empty()
    first = owned[0]["text"].split(". ")[0].split(" is ")[0]
    document["key_topics"] = [{"text": first, "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


def _owned(prompt: str) -> list[str]:
    payload = reconcile_payload(prompt)
    return [item["ref"] for item in payload["final_items"] + payload["map_items"]]


def omit_cues(*refs: str):
    """A primary that leaves out the cue decisions of ``refs``: a targeted, per-ref failure."""

    def primary(prompt: str) -> str:
        patch = json.loads(reconcile_keep_patch(prompt))
        patch["cue_checked"] = {k: v for k, v in patch["cue_checked"].items() if k not in refs}
        if not patch["cue_checked"]:
            patch.pop("cue_checked")
        return json.dumps(patch)

    return primary


def _run(primary=reconcile_keep_patch, repair=reconcile_repair_reply, *, count=5, chunk=None):
    calls: list[dict] = []
    coverage: list[dict] = []
    lock = threading.Lock()

    def transport(prompt, **kwargs):
        with lock:
            calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return chunk(prompt, **kwargs) if chunk else _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return semantic_keep_patch(prompt)
        assert kwargs["stage"] == "reconcile"
        return repair(prompt) if kwargs["attempt"] == "repair" else primary(prompt)

    error = None
    try:
        generate_notes_document(
            snapshot=_snapshot(count),
            output_language="en",
            provider=GEMINI[0],
            model=GEMINI[1],
            transport=transport,
            invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
            limit_error=RuntimeError,
            on_coverage=coverage.append,
        )
    except AssertionError as failure:
        error = failure.args[0]
    except RuntimeError as failure:  # a bounded limit
        error = str(failure)
    return calls, coverage, error


def _reconciles(calls, attempt):
    return [c for c in calls if c["stage"] == "reconcile" and c["attempt"] == attempt]


def _bundle_parts(prompt: str) -> dict[str, str]:
    return dict(part.split(":\n", 1) for part in prompt.split(BUNDLE_PART_MARKER)[1:])


def _repair_payload(prompt: str) -> dict:
    return json.JSONDecoder().raw_decode(prompt.split("REPAIR_INPUT_JSON:\n", 1)[1])[0]


def _assert_aborted(calls, coverage):
    assert "semantic_audit" not in {c["stage"] for c in calls} and coverage == []


# ----- success ----------------------------------------------------------------------------------


def test_two_failed_views_are_repaired_in_one_shared_call():
    calls, coverage, error = _run(omit_cues("m0", "m2"))  # views m0+m1, m2, m3, m4
    assert error is None
    assert len(_reconciles(calls, "primary")) == 4
    (repair,) = _reconciles(calls, "repair")
    # Only the two failed views, each with its own scope, schema and retained decisions.
    schema = repair["response_schema"]
    assert schema["required"] == ["part_0", "part_1"] and not schema["additionalProperties"]
    assert schema["properties"]["part_0"]["properties"]["cue_checked"]["required"] == ["m0"]
    assert schema["properties"]["part_1"]["properties"]["cue_checked"]["required"] == ["m2"]
    assert schema["properties"]["part_0"]["properties"]["additions"]["maxItems"] > 0
    assert schema["properties"]["part_1"]["properties"]["additions"]["maxItems"] == 0
    parts = _bundle_parts(repair["prompt"])
    assert list(parts) == ["part_0", "part_1"]
    assert [[f["ref"] for f in _repair_payload(p)["failures"]] for p in parts.values()] == [
        ["m0"],
        ["m2"],
    ]
    assert "m1" not in json.dumps(_repair_payload(parts["part_0"])["failures"])
    for refs in ("m3", "m4"):  # accepted views are never resent
        assert refs not in repair["prompt"].split(BUNDLE_PART_MARKER, 1)[1]
    audit = coverage[0]["source_audit"]
    repair_index = repair["provider_call_index"]
    primary_index = {
        _owned(c["prompt"])[0]: c["provider_call_index"] for c in _reconciles(calls, "primary")
    }
    # One shared call is the accepted call of each view it repaired.
    assert audit["provider_call_indices"] == [
        repair_index,
        repair_index,
        primary_index["m3"],
        primary_index["m4"],
    ]
    assert audit["provider_call_index"] == repair_index and audit["part_count"] == 4
    # m1's retained decision and the repaired m0/m2 decisions; every cue decided once.
    assert audit["counts"]["cue_checked"] == {"checked": 5, "accepted": 0, "declined": 5}
    assert audit["counts"]["map"]["confirm"] == 2


def test_one_failed_view_is_sent_exactly_as_its_own_repair():
    calls, coverage, error = _run(omit_cues("m3"))
    assert error is None
    (repair,) = _reconciles(calls, "repair")
    assert BUNDLE_PART_MARKER not in repair["prompt"]
    assert repair["response_schema"]["properties"]["cue_checked"]["required"] == ["m3"]
    assert [f["ref"] for f in _repair_payload(repair["prompt"])["failures"]] == ["m3"]
    assert (
        coverage[0]["source_audit"]["provider_call_indices"][2] == (repair["provider_call_index"])
    )


def test_targeted_and_generic_parts_share_one_call():
    def primary(prompt):
        return "not json" if _owned(prompt) == ["m3"] else omit_cues("m0")(prompt)

    calls, coverage, error = _run(primary)
    assert error is None
    (repair,) = _reconciles(calls, "repair")
    parts = _bundle_parts(repair["prompt"])
    assert list(parts) == ["part_0", "part_2"]
    assert "REPAIR_INPUT_JSON:\n" in parts["part_0"]
    # The generic part is the bounded full-view repair: its own ledger plus the invalid output.
    assert "REPAIR_INPUT_JSON:\n" not in parts["part_2"]
    assert _owned(parts["part_2"]) == ["m3"] and parts["part_2"].rstrip().endswith("not json")
    generic = repair["response_schema"]["properties"]["part_2"]
    assert generic["properties"]["cue_checked"]["required"] == ["m3"]
    assert coverage[0]["source_audit"]["counts"]["cue_checked"]["checked"] == 5


def test_bundle_prompt_states_the_exact_keys_in_both_languages():
    ledger = ReconcileLedger.build(
        _snapshot(5), _empty(), [_doc_for(i) for i in range(5)], include_clause_cues=True
    )
    views = ledger.partitions()
    bundle = ReconcileRepairBundle.plan(ledger, [(views[0], "x"), (views[3], "y")])
    for language in ("en", "vi"):
        prompt = bundle.prompt(output_language=language, budget=nd.notes_budget(*GEMINI))
        assert '["part_0", "part_3"]' in prompt.split(BUNDLE_PART_MARKER, 1)[0]
        assert list(_bundle_parts(prompt)) == ["part_0", "part_3"]


def _doc_for(index: int) -> dict:
    document = _empty()
    document["key_topics"] = [
        {"text": f"Topic {index} plan waits", "source_ids": [f"{index + 1:032x}"]}
    ]
    return document


@pytest.mark.parametrize(
    "failed",
    [
        "empty",
        "duplicate",
        "reordered",
        "foreign",
    ],
)
def test_bundle_plan_rejects_bad_view_identity(failed):
    ledger = ReconcileLedger.build(_snapshot(5), _empty(), [_doc_for(i) for i in range(5)])
    other = ReconcileLedger.build(_snapshot(6), _empty(), [_doc_for(i) for i in range(6)])
    views = ledger.partitions()
    cases = {
        "empty": [],
        "duplicate": [(views[1], "x"), (views[1], "x")],
        "reordered": [(views[2], "x"), (views[1], "x")],
        "foreign": [(other.partitions()[1], "x")],
    }
    with pytest.raises(nd.NotesDocumentError):
        ReconcileRepairBundle.plan(ledger, cases[failed])


# ----- abort: nothing published -----------------------------------------------------------------


def _bundle_reply(mutate):
    def repair(prompt):
        return mutate(json.loads(reconcile_repair_reply(prompt)), prompt)

    return repair


def _raw_pairs(pairs):
    return "{" + ",".join(f"{json.dumps(k)}:{json.dumps(v)}" for k, v in pairs) + "}"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r, _p: json.dumps({"part_0": r["part_0"]}), id="missing"),
        pytest.param(lambda r, _p: json.dumps({**r, "part_3": r["part_0"]}), id="extra"),
        pytest.param(
            lambda r, _p: json.dumps({"part_0": r["part_0"], "part_9": r["part_1"]}),
            id="foreign",
        ),
        pytest.param(
            lambda r, _p: _raw_pairs([("part_0", r["part_0"]), ("part_0", r["part_0"])]),
            id="duplicate",
        ),
        pytest.param(
            lambda r, _p: _raw_pairs([("part_1", r["part_1"]), ("part_0", r["part_0"])]),
            id="reordered",
        ),
        pytest.param(lambda r, _p: json.dumps([r["part_0"], r["part_1"]]), id="not-object"),
        pytest.param(lambda r, _p: "not json", id="not-json"),
        pytest.param(  # a valid part beside one wrong part fails the whole bundle
            lambda r, _p: json.dumps({**r, "part_1": {**r["part_1"], "cue_checked": {}}}),
            id="missing-cue",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {**r, "part_1": {**r["part_1"], "cue_checked": {"m2": False, "m3": False}}}
            ),
            id="cue-of-accepted-view",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {
                    **r,
                    "part_1": {
                        **r["part_1"],
                        "dispositions": [{**r["part_1"]["dispositions"][0], "ref": "m3"}],
                    },
                }
            ),
            id="ref-of-accepted-view",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {**r, "part_0": r["part_1"], "part_1": r["part_0"]}  # keys right, bodies swapped
            ),
            id="swapped-bodies",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {
                    **r,
                    "part_1": {
                        **r["part_1"],
                        "dispositions": [
                            {
                                **r["part_1"]["dispositions"][0],
                                "action": "update",
                                "text": "Topic 2 plan waits for Avery",  # absent name
                                "source_ids": [f"{3:032x}"],
                            }
                        ],
                    },
                }
            ),
            id="unsupported-name",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {
                    **r,
                    "part_1": {
                        **r["part_1"],
                        "dispositions": [
                            {
                                **r["part_1"]["dispositions"][0],
                                "action": "update",
                                "text": "Topic 2 plan waits",
                                "source_ids": [f"{5:032x}"],  # a segment outside the item
                            }
                        ],
                    },
                }
            ),
            id="foreign-source",
        ),
        pytest.param(
            lambda r, _p: json.dumps(
                {
                    **r,
                    "part_1": {
                        **r["part_1"],
                        "additions": [
                            {"section": "key_topics", "text": "Topic 2", "source_ids": []}
                        ],
                    },
                }
            ),
            id="later-view-addition",
        ),
    ],
)
def test_an_invalid_bundle_repair_aborts_with_no_notes(mutate):
    calls, coverage, error = _run(omit_cues("m0", "m2"), _bundle_reply(mutate))
    assert error is not None
    assert error["stage"] == "reconcile" and error["attempt"] == "repair"
    assert error["repair_state"] == "already_consumed"
    assert len(_reconciles(calls, "repair")) == 1  # no recursive repair
    _assert_aborted(calls, coverage)


def test_an_unrepairable_oversized_generic_part_fails_before_any_repair_call():
    def primary(prompt):
        if _owned(prompt) == ["m3"]:
            return "x" * (nd.MAX_REPAIR_OUTPUT_BYTES + 1)  # above the raw repair cap
        return omit_cues("m0")(prompt)

    calls, coverage, error = _run(primary)
    assert error["stage"] == "reconcile" and error["repair_state"] == "unavailable"
    assert _reconciles(calls, "repair") == []
    _assert_aborted(calls, coverage)


def test_an_over_budget_bundle_fails_before_inference(monkeypatch):
    real = nd._check_prompt

    def check(prompt, **kwargs):
        if prompt.startswith("BUNDLED REPAIR"):
            raise nd.NotesDocumentError("The notes prompt exceeds the safe model context budget.")
        return real(prompt, **kwargs)

    monkeypatch.setattr(nd, "_check_prompt", check)
    calls, coverage, error = _run(omit_cues("m0", "m2"))
    assert error["repair_state"] == "unavailable" and _reconciles(calls, "repair") == []
    _assert_aborted(calls, coverage)


def test_a_repair_consumed_by_a_map_still_fails_the_first_invalid_view():
    used = threading.Event()

    def chunk(prompt, **kwargs):
        if kwargs["attempt"] == "primary" and not used.is_set():
            used.set()
            return "not json"  # consumes the task's one repair
        return _map_reply(prompt)

    calls, coverage, error = _run(omit_cues("m0", "m2"), chunk=chunk)
    # The first invalid view fails at once, as before v41; the abort may stop a sibling first.
    assert error == "The notes request was stopped after a failed request." or (
        error["stage"] == "reconcile"
        and error["attempt"] == "primary"
        and error["repair_state"] == "already_consumed"
    )
    assert [c for c in calls if c["attempt"] == "repair"] == [
        c for c in calls if c["stage"] == "chunk" and c["attempt"] == "repair"
    ]
    _assert_aborted(calls, coverage)


@pytest.mark.parametrize("code", [None, "provider_output_too_large"])
def test_a_transport_error_sends_no_repair_or_recovery_and_publishes_nothing(code):
    calls: list[dict] = []
    coverage: list[dict] = []
    m0_done = threading.Event()

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if _owned(prompt)[0] == "m0":
            m0_done.set()
            return omit_cues("m0")(prompt)  # invalid, deferred first
        assert m0_done.wait(10)
        if _owned(prompt) == ["m3"]:
            error = ConnectionError("provider failed")
            error.code = code  # an over-cap answer gets no recovery on an internal schema
            raise error
        return reconcile_keep_patch(prompt)

    with pytest.raises(ConnectionError):  # raised as is; never pooled or retried
        generate_notes_document(
            snapshot=_snapshot(5),
            output_language="en",
            provider=GEMINI[0],
            model=GEMINI[1],
            transport=transport,
            invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
            limit_error=RuntimeError,
            on_coverage=coverage.append,
        )
    assert all(c["attempt"] == "primary" for c in calls) and coverage == []
    assert "semantic_audit" not in {c["stage"] for c in calls}


# ----- caps -------------------------------------------------------------------------------------


def test_max_chunk_run_pools_two_failures_under_every_cap(monkeypatch):
    count = nd.MAX_NOTES_CHUNKS
    calls, coverage, error = _run(omit_cues("m0", "m23"), count=count)
    assert error is None
    attempts = [(c["stage"], c["attempt"]) for c in calls]
    assert attempts.count(("chunk", "primary")) == count
    assert attempts.count(("reconcile", "primary")) == 4
    assert attempts.count(("reconcile", "repair")) == 1
    assert [a for a in attempts if a[1] != "primary"] == [("reconcile", "repair")]
    assert len(calls) <= nd.MAX_PROVIDER_CALLS
    (repair,) = _reconciles(calls, "repair")
    assert repair["max_output_bytes"] == 64_000  # the one whole-reconcile cap, not per part
    assert list(_bundle_parts(repair["prompt"])) == ["part_0", "part_3"]
    assert coverage[0]["source_audit"]["part_count"] == 4

    # The shared repair is one more call; with no room it fails before inference.
    monkeypatch.setattr(nd, "MAX_PROVIDER_CALLS", count + 4)
    calls, coverage, error = _run(omit_cues("m0", "m23"), count=count)
    assert "request limit" in error and len(calls) == count + 4
    assert _reconciles(calls, "repair") == [] and coverage == []


# ----- whole-bundle raw output cap ---------------------------------------------------------------


def _padded(reply: str, total: int) -> str:
    """The same JSON, inflated to ``total`` UTF-8 bytes by interior whitespace."""
    assert reply.startswith("{") and len(reply.encode()) <= total
    return "{" + " " * (total - len(reply.encode())) + reply[1:]


@pytest.mark.parametrize("extra", [0, 1], ids=["at-cap", "over-cap"])
def test_primary_reconcile_response_obeys_the_raw_cap(extra):
    def primary(prompt):
        return _padded(reconcile_keep_patch(prompt), MAX_RECONCILE_OUTPUT_BYTES + extra)

    calls, coverage, error = _run(primary)
    if extra == 0:
        assert error is None and coverage
    else:
        assert error is not None, "An oversized primary response was accepted."
        assert error["validation_reason"] == "size"
        assert error["repair_state"] == "unavailable"  # raw is over the bounded repair cap
        assert not _reconciles(calls, "repair")
        _assert_aborted(calls, coverage)


@pytest.mark.parametrize("omitted", [("m0", "m2"), ("m3",)], ids=["two-parts", "one-part"])
def test_a_repair_exactly_at_the_cap_is_accepted(omitted):
    def repair(prompt):
        return _padded(reconcile_repair_reply(prompt), MAX_RECONCILE_OUTPUT_BYTES)

    calls, coverage, error = _run(omit_cues(*omitted), repair)
    assert error is None and len(_reconciles(calls, "repair")) == 1 and coverage


@pytest.mark.parametrize("omitted", [("m0", "m2"), ("m3",)], ids=["two-parts", "one-part"])
def test_a_whitespace_inflated_repair_over_the_cap_aborts(omitted):
    def repair(prompt):  # structurally valid; only one byte too large
        return _padded(reconcile_repair_reply(prompt), MAX_RECONCILE_OUTPUT_BYTES + 1)

    calls, coverage, error = _run(omit_cues(*omitted), repair)
    assert error["stage"] == "reconcile" and error["attempt"] == "repair"
    assert error["validation_reason"] == "size"
    assert error["repair_state"] == "already_consumed"
    assert error["output_bytes"] == MAX_RECONCILE_OUTPUT_BYTES + 1
    assert len(_reconciles(calls, "repair")) == 1  # no second repair or recovery
    assert [c for c in calls if c["attempt"] == "recovery"] == []
    _assert_aborted(calls, coverage)


@pytest.mark.parametrize("parts", [1, 2])
def test_the_bundle_cap_counts_utf8_bytes_not_characters(parts):
    ledger = ReconcileLedger.build(_snapshot(5), _empty(), [_doc_for(i) for i in range(5)])
    views = ledger.partitions()
    bundle = ReconcileRepairBundle.plan(ledger, [(views[i], "x") for i in range(parts)])
    # Under the cap in characters, over it in UTF-8 bytes (two bytes each).
    raw = '{"part_0":"' + "é" * (MAX_RECONCILE_OUTPUT_BYTES // 2) + '"}'
    assert len(raw) <= MAX_RECONCILE_OUTPUT_BYTES < len(raw.encode())
    with pytest.raises(nd.NotesDocumentError) as failure:
        bundle.parse(raw)
    assert failure.value.reason_code == "size"
    with pytest.raises(nd.NotesDocumentError) as failure:
        bundle.parse("\ud800")  # a lone surrogate is not UTF-8
    assert failure.value.reason_code == "json"
