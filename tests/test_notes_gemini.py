"""Registered Gemini notes path: long fact inventory + reconcile, transport schema, short rules."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from notes_helpers import SEMANTIC_KEEP_PATCH, reconcile_keep_patch

from asr_studio import antigravity as antigravity_module
from asr_studio import notes_document as nd
from asr_studio.antigravity import AntigravityError, AntigravityProvider
from asr_studio.notes_pipeline import generate_notes_document

GEMINI = ("antigravity", "gemini-3.8-flash")
SUPERSESSION = "is a decision stating old -> new"
SUPERSESSION_VI = "là một quyết định nêu cũ -> mới"


def _empty() -> dict:
    return {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}


def _segment(index: int, start: float, text: str) -> dict:
    return {
        "id": f"{index:032x}",
        "start_seconds": start,
        "end_seconds": start + 1,
        "text": text,
        "speaker_name": "Morgan",
    }


def _long_snapshot() -> dict:
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            _segment(1, 0.0, "Launch plan is approved."),
            _segment(2, 3_601.0, "Vendor contract renewal is still open."),
        ],
    }


def _source(prompt: str) -> dict:
    return json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]


def _map_reply(prompt: str) -> str:
    owned = [s for s in _source(prompt)["segments"] if not s.get("overlap")]
    document = _empty()
    document["key_topics"] = [{"text": owned[0]["text"], "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


def _run(transport, *, model: str, snapshot: dict, coverage: list) -> dict:
    return generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        provider="antigravity",
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=coverage.append,
    )


def test_gemini_pipeline_links_short_record_id_and_outcome_after_motion():
    snapshot = {
        "meeting_date": "2026-01-05",
        "segments": [
            _segment(1, 0.0, "Record 315 has a term ending April 11."),
            _segment(2, 5.0, "I move to approve this record."),
            _segment(3, 10.0, "The motion carries."),
            _segment(4, 3_601.0, "Meeting adjourned."),
        ],
    }
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        if kwargs["stage"] == "reconcile":
            return reconcile_keep_patch(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        assert kwargs["stage"] == "chunk"
        document = _empty()
        for segment in _source(prompt)["segments"]:
            if not segment.get("overlap") and segment["text"] == "I move to approve this record.":
                document["decisions"] = [
                    {"text": "Record 315 approved", "source_ids": [segment["id"]]}
                ]
        return json.dumps(document)

    result = _run(transport, model=GEMINI[1], snapshot=snapshot, coverage=coverage)

    assert result["decisions"] == [
        {"text": "Record 315 approved", "source_ids": [f"{i:032x}" for i in (1, 2, 3)]}
    ]
    assert coverage[0]["source_context"] == {"status": "applied", "items": 1, "added_ids": 2}


def test_long_known_gemini_runs_fact_inventory_then_one_reconcile_and_context_link():
    snapshot = _long_snapshot()
    # A role anchor just before the cited claim: the bounded context linker adds it. (A person
    # name must already be cited: the strict Gemini ledger rejects an uncited one.)
    snapshot["segments"].insert(1, _segment(9, 3_590.0, "Thank you, councilmember, go ahead."))
    calls: list[dict] = []
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        assert kwargs["stage"] == "reconcile"
        source = json.JSONDecoder().raw_decode(prompt.split("RECONCILE_INPUT_JSON:\n", 1)[1])[0]
        claim = source["source"]["segments"][2]["id"]
        patch = json.loads(reconcile_keep_patch(prompt))
        patch["additions"] = [
            {
                "section": "key_topics",
                "text": "Councilmember: renewal open",
                "source_ids": [claim],
                "evidence": [{"source_id": claim, "quote": "still open"}],
                "owner": None,
                "deadline_text": None,
            }
        ]
        return json.dumps(patch)

    result = _run(transport, model=GEMINI[1], snapshot=snapshot, coverage=coverage)

    stages = [call["stage"] for call in calls]
    # parts + reconcile + one semantic pass over two disjoint item parts, no free-form final
    assert stages == ["chunk", "chunk", "reconcile", "semantic_audit", "semantic_audit"]
    for call in calls[:2]:
        assert "Fact inventory:" in call["prompt"]
        assert SUPERSESSION in call["prompt"]
        assert nd._GEMINI_FACT_INVENTORY_RULE["en"] in call["prompt"]
        assert "response_schema" not in call
    for call in calls[-2:]:
        assert call["response_schema"]["required"] == [
            "audit_version",
            "changes",
            "unresolved_refs",
        ]
    assert calls[2]["response_schema"]["required"] == [
        "ledger_version",
        "dispositions",
        "additions",
    ]
    assert coverage[0]["source_audit"]["mode"] == "reconcile"
    assert coverage[0]["source_context"] == {"status": "applied", "items": 1, "added_ids": 1}
    assert {"text": "Councilmember: renewal open", "source_ids": [f"{9:032x}", f"{2:032x}"]} in (
        result["key_topics"]
    )
    assert coverage[0]["provider_calls"] == len(calls) <= nd.MAX_PROVIDER_CALLS


def test_long_unknown_antigravity_model_keeps_the_previous_free_form_policy():
    calls: list[dict] = []
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        assert kwargs["stage"] == "final"
        document = _empty()
        document["overview"] = [
            {"text": "Launch plan approved.", "source_ids": [kwargs["allowed_source_ids"][0]]}
        ]
        return json.dumps(document)

    _run(transport, model="gemini-test", snapshot=_long_snapshot(), coverage=coverage)

    assert "reconcile" not in [call["stage"] for call in calls]
    assert all("Fact inventory:" not in call["prompt"] for call in calls)
    assert all(SUPERSESSION not in call["prompt"] for call in calls)
    assert "source_context" not in coverage[0]


def test_long_gemini_invalid_map_and_reconcile_share_the_one_repair():
    calls: list[dict] = []
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "chunk":
            return "not json" if len(calls) == 1 else _map_reply(prompt)
        return json.dumps({"ledger_version": 1, "dispositions": [], "additions": []})

    with pytest.raises(AssertionError) as failure:
        _run(transport, model=GEMINI[1], snapshot=_long_snapshot(), coverage=coverage)
    assert [c["attempt"] for c in calls].count("repair") == 1
    assert [c["stage"] for c in calls].count("reconcile") == 1
    assert failure.value.args[0]["repair_state"] == "already_consumed"
    assert coverage == []  # nothing published


def _short_snapshot() -> dict:
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            _segment(1, 0.0, "The demo was planned for Thursday."),
            _segment(2, 5.0, "We agree to move the demo to Wednesday noon."),
            _segment(3, 9.0, "Lan, please prepare the demo by Wednesday noon."),
        ],
    }


def test_short_gemini_keeps_an_old_to_new_decision_apart_from_its_task():
    snapshot = _short_snapshot()
    calls: list[dict] = []
    coverage: list[dict] = []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        ids = kwargs["allowed_source_ids"]
        if kwargs["stage"] == "single":
            document = _empty()
            document["decisions"] = [
                {"text": "Demo: Thursday -> Wednesday noon", "source_ids": [ids[0], ids[1]]}
            ]
            document["action_items"] = [
                {
                    "text": "Prepare demo",
                    "source_ids": [ids[2]],
                    "owner": None,
                    "deadline_text": "Wednesday noon",
                    "deadline_date": None,
                }
            ]
            return json.dumps(document)
        assert kwargs["stage"] == "audit"
        payload = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
        refs = {c["section"]: c["ref"] for c in payload["audit_catalog"]["draft_candidates"]}
        document = _empty()
        document["decisions"] = [{"text": refs["decisions"], "source_ids": [ids[0], ids[1]]}]
        document["action_items"] = [
            {
                "text": refs["action_items"],
                "source_ids": [ids[2]],
                "owner": None,
                "deadline_text": "Wednesday noon",
                "deadline_date": None,
            }
        ]
        return json.dumps(document)

    result = _run(transport, model=GEMINI[1], snapshot=snapshot, coverage=coverage)

    assert [call["stage"] for call in calls] == ["single", "audit"]
    assert SUPERSESSION in calls[0]["prompt"]
    assert coverage[0]["source_audit"]["mode"] == "references"
    assert [item["text"] for item in result["decisions"]] == ["Demo: Thursday -> Wednesday noon"]
    assert [item["text"] for item in result["action_items"]] == ["Prepare demo"]


@pytest.mark.parametrize("language", ["en", "vi"])
def test_gemini_rules_reach_only_registered_gemini_source_prompts(language):
    snapshot = _short_snapshot()
    marker = SUPERSESSION if language == "en" else SUPERSESSION_VI
    gemini = nd.notes_budget(*GEMINI)

    def single(budget):
        return nd.build_prompt(snapshot, output_language=language, budget=budget)

    def chunk(budget):
        return nd.build_chunk_prompt(
            snapshot, chunk_index=1, chunk_count=2, output_language=language, budget=budget
        )

    for build in (single, chunk):
        assert marker in build(gemini)
        for other in (
            nd.notes_budget("chatgpt", "gpt-5.6-sol"),
            nd.notes_budget("antigravity", "gemini-test"),
            None,
        ):
            assert marker not in build(other)


class _GeminiAdapter:
    def __init__(self) -> None:
        self.last_notes_usage: dict[str, int] = {}
        self.messages: list[str] = []

    def generate(self, _model: str, message: str, _thinking_level: str = "high") -> str:
        self.messages.append(message)
        if "RECONCILE_INPUT_JSON:\n" in message:
            return reconcile_keep_patch(message)
        if "AUDIT_INPUT_JSON:\n" in message:
            return SEMANTIC_KEEP_PATCH
        return _map_reply(message)


def _provider(tmp_path, adapter) -> AntigravityProvider:
    provider = AntigravityProvider(tmp_path / "antigravity", adapter=adapter)
    provider.ensure()
    with provider.store.locked(write=True) as state:
        state["connection_status"] = "connected"
        state["connection_version"] = 0
    return provider


def _schema_of(message: str) -> dict:
    return json.loads(message.rsplit("use only the source IDs the schema allows:\n", 1)[1])


def test_antigravity_long_run_sends_the_reconcile_ledger_schema(tmp_path):
    adapter = _GeminiAdapter()
    coverage: list[dict] = []
    result = _provider(tmp_path, adapter).generate_notes_document(
        snapshot=_long_snapshot(),
        connection_version=0,
        model=GEMINI[1],
        reasoning_effort="medium",
        on_coverage=coverage.append,
    )

    assert len(adapter.messages) == 5  # 2 maps, reconcile, 2 disjoint semantic-audit parts
    assert [_schema_of(m)["properties"]["schema_version"] for m in adapter.messages[:2]]
    for message in adapter.messages[-2:]:
        assert _schema_of(message)["required"] == [
            "audit_version",
            "changes",
            "unresolved_refs",
        ]
    assert _schema_of(adapter.messages[2])["required"] == [
        "ledger_version",
        "dispositions",
        "additions",
    ]
    assert coverage[0]["source_audit"]["mode"] == "reconcile"
    assert len(result["key_topics"]) == 2


def test_antigravity_transport_counts_the_custom_schema_and_keeps_schema1_bytes(
    tmp_path, monkeypatch
):
    adapter = _GeminiAdapter()
    adapter.generate = lambda _m, message, _l="high": adapter.messages.append(message) or "{}"
    provider = _provider(tmp_path, adapter)
    budget = nd.notes_budget(*GEMINI)
    prompt = "frame payload"
    # This limit fits schema 1 exactly, so only the larger custom schema can exceed it.
    schema1_limit = replace(
        budget, request_guard_bytes=nd.notes_request_body_bytes(prompt, (), budget=budget)
    )
    custom = {"type": "object", "description": "x" * schema1_limit.request_guard_bytes}
    roomy = replace(budget, request_guard_bytes=2 * schema1_limit.request_guard_bytes)

    def call(selected_budget, **extra):
        def fake_pipeline(**kwargs):
            return kwargs["transport"](
                prompt,
                allowed_source_ids=(),
                max_output_bytes=nd.MAX_OUTPUT_BYTES,
                stage="reconcile" if extra else "single",
                attempt="primary",
                provider_call_index=1,
                prompt_bytes=len(prompt),
                budget=selected_budget,
                **extra,
            )

        monkeypatch.setattr(antigravity_module, "generate_notes_pipeline", fake_pipeline)
        return provider.generate_notes_document(
            snapshot=_long_snapshot(),
            connection_version=0,
            model=GEMINI[1],
            reasoning_effort="high",
        )

    call(schema1_limit)
    assert adapter.messages == [nd.build_notes_provider_message(prompt, nd.build_notes_schema(()))]

    with pytest.raises(AntigravityError) as failure:
        call(schema1_limit, response_schema=custom)
    assert failure.value.code == "provider_request_too_large"
    assert len(adapter.messages) == 1  # rejected before the adapter

    call(roomy, response_schema=custom)
    assert adapter.messages[-1] == nd.build_notes_provider_message(prompt, custom)


# ----- strict-name grounding and bounded context (registered Gemini only) ------------------

GROUND_SOURCE = {
    "meeting_date": "2026-01-05",
    "speakers": [],
    "segments": [
        {
            "id": "g1",
            "start_seconds": 0.0,
            "end_seconds": 4.0,
            "speaker_name": "SPEAKER_00",
            "text": "Thank you, councilmember Lindqvist, for the report.",
        },
        {
            "id": "g2",
            "start_seconds": 4.0,
            "end_seconds": 9.0,
            "speaker_name": "SPEAKER_00",
            "text": "The committee vote was Tarrow and Okonjo in favor.",
        },
        {
            "id": "g3",
            "start_seconds": 9.0,
            "end_seconds": 14.0,
            "speaker_name": "SPEAKER_01",
            "text": "I support the extension of the garden lease.",
        },
        {
            "id": "g4",
            "start_seconds": 14.0,
            "end_seconds": 18.0,
            "speaker_name": "SPEAKER_01",
            "text": "Residents met in the lobby to discuss it.",
        },
    ],
}


def _ground_ledger(text: str, ids: list[str], *, strict: bool, extra: str | None = None):
    from asr_studio.notes_reconcile import ReconcileLedger

    document = _empty()
    document["key_topics"] = [{"text": text, "source_ids": ids}]
    if extra is not None:  # another draft note in the same ledger
        document["overview"] = [{"text": extra, "source_ids": ids}]
    return ReconcileLedger.build(GROUND_SOURCE, _empty(), [document], strict_names=strict)


def _keep(ledger) -> str:
    return json.dumps({"ledger_version": 2, "dispositions": [], "additions": []})


@pytest.mark.parametrize(
    ("text", "ids", "cue"),
    [
        # Single name stated only in an uncited segment: cue its candidate.
        ("Lindqvist supports garden lease extension", ["g3"], ["g1"]),
        # Invented member in a vote list: no segment states it, so there is no candidate.
        ("Committee vote: Tarrow, Mbeki, Okonjo in favor", ["g2"], []),
        ("Committee recommended passage with Mbeki in favor", ["g2"], []),
        # Similar spellings are not proof: a common cited word or a near different name.
        ("Residents met Bobby in the lobby", ["g4"], []),
        ("Committee vote: Tarow and Okonjo in favor", ["g2"], []),
    ],
)
def test_strict_ledger_flags_single_and_absent_names_and_never_keeps_them(text, ids, cue):
    strict = _ground_ledger(text, ids, strict=True)
    names = strict.prompt_payload()["map_items"][0]["attention"]["uncited_names"]
    assert [entry["candidate_ids"] for entry in names] == [cue]
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        strict.parse(_keep(strict))  # an unlisted flagged ref is never silently kept
    plan = strict.plan_repair(_keep(strict))
    assert plan is not None and plan.refs == ("m0",)
    repair = strict.build_repair_prompt(plan)
    assert "Strict names:" in repair and '"names":[' in repair
    # The same item on a ChatGPT (default) ledger: unchanged behavior.
    loose = _ground_ledger(text, ids, strict=False)
    assert "attention" not in loose.prompt_payload()["map_items"][0]
    loose.parse(_keep(loose))


def test_draft_notes_never_exempt_a_name_through_their_own_lower_case_words():
    strict = _ground_ledger(
        "Committee vote: Tarrow, Mbeki, Okonjo in favor",
        ["g2"],
        strict=True,
        extra="committee thanked mbeki for the report",
    )
    flagged = [m["ref"] for m in strict.prompt_payload()["map_items"] if "attention" in m]
    assert flagged == ["m1"]  # the vote-list name stays flagged
    with pytest.raises(nd.NotesDocumentError, match="named span"):
        strict.parse(_keep(strict))


def test_strict_ledger_accepts_exact_cited_names_common_words_acronyms_and_unnamed_fix():
    strict = _ground_ledger(
        "Garden lease: Tarrow and Okonjo favor; MHA review", ["g2"], strict=True
    )
    assert "attention" not in strict.prompt_payload()["map_items"][0]
    strict.parse(_keep(strict))
    # Sentence-initial ordinary word absent from the source is not a name.
    initial = _ground_ledger("Deliverables pending for garden lease", ["g3"], strict=True)
    assert "attention" not in initial.prompt_payload()["map_items"][0]
    # Anonymising the invented member, with every other fact kept, passes validation.
    flagged = _ground_ledger("Committee vote: Tarrow, Mbeki, Okonjo in favor", ["g2"], strict=True)
    fixed = {
        "ref": "m0",
        "action": "update",
        "reason": "none",
        "target": None,
        "text": "Committee vote: Tarrow, one other member, Okonjo in favor",
        "source_ids": ["g2"],
        "evidence": [{"source_id": "g2", "quote": "Tarrow and Okonjo"}],
        "owner": None,
        "deadline_text": None,
    }
    patch = {"ledger_version": 2, "dispositions": [fixed], "additions": []}
    assert flagged.parse(json.dumps(patch)).document["key_topics"][0]["text"] == fixed["text"]


@pytest.mark.parametrize("language", ["en", "vi"])
def test_strict_rule_reaches_only_strict_reconcile_prompts(language):
    budget = nd.notes_budget(*GEMINI)
    strict = _ground_ledger("Garden lease", ["g3"], strict=True)
    loose = _ground_ledger("Garden lease", ["g3"], strict=False)
    marker = "Strict names:" if language == "en" else "Tên nghiêm ngặt:"
    assert marker in strict.build_prompt(output_language=language, budget=budget)
    assert marker not in loose.build_prompt(output_language=language, budget=budget)


def _ctx_snapshot(*texts: str) -> dict:
    return {
        "segments": [
            {
                "id": f"{i:032x}",
                "start_seconds": i * 5.0,
                "end_seconds": i * 5.0 + 4,
                "text": text,
                "speaker_id": "A",
            }
            for i, text in enumerate(texts)
        ]
    }


def _linked(snapshot: dict, section: str, text: str, cited: list[int], *, strict: bool):
    from asr_studio.notes_context import link_antecedent_context

    document = _empty()
    document[section] = [{"text": text, "source_ids": [f"{i:032x}" for i in cited]}]
    linked, _counts = link_antecedent_context(
        document, snapshot, max_refs_per_item=200, strict=strict
    )
    return [int(ref, 16) for ref in linked[section][0]["source_ids"]]


def test_strict_context_links_a_short_labelled_record_and_its_date():
    snapshot = _ctx_snapshot(
        "Item four. Appointment 2217 of Dana Ruiz, term to May 3, 2027.",
        "Thank you.",
        "We are pleased to confirm her for the panel.",
    )
    text = "Appointment 2217 confirming Dana Ruiz, term to May 3, 2027"
    assert _linked(snapshot, "decisions", text, [2], strict=True) == [0, 1, 2]
    assert _linked(snapshot, "decisions", text, [2], strict=False) == [2]  # ChatGPT unchanged


def test_strict_context_links_the_outcome_after_a_cited_motion_only():
    snapshot = _ctx_snapshot(
        "I move to amend the resolution to add the letter of expectations. Second.",
        "All those in favor of the amendment vote aye. The ayes have it.",
        "Next agenda item. The motion carried last week.",
    )
    text = "Letter of expectations approved in amendment"
    assert _linked(snapshot, "confirmation_items", text, [0], strict=True) == [0, 1]
    assert _linked(snapshot, "confirmation_items", text, [0], strict=False) == [0]
    # No outcome claimed, or the outcome only after a new agenda item: nothing is added.
    assert _linked(snapshot, "key_topics", "Letter of expectations proposed", [0], strict=True) == [
        0
    ]
    blocked = _ctx_snapshot(snapshot["segments"][0]["text"], "Next agenda item. It passed.")
    assert _linked(blocked, "decisions", text, [0], strict=True) == [0]


def test_long_gemini_reconcile_is_strict_and_chatgpt_reconcile_is_not():
    snapshot = _long_snapshot()
    prompts: dict[str, str] = {}

    def transport(prompt, **kwargs):
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            return SEMANTIC_KEEP_PATCH
        prompts[kwargs.get("provider_model", "x")] = prompt
        return reconcile_keep_patch(prompt)

    _run(transport, model=GEMINI[1], snapshot=snapshot, coverage=[])
    gemini_prompt = next(iter(prompts.values()))
    assert "Strict names:" in gemini_prompt
    prompts.clear()
    generate_notes_document(
        snapshot=_long_snapshot(),
        output_language="en",
        provider="chatgpt",
        model="gpt-5.6-sol",
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=lambda _c: None,
    )
    assert "Strict names:" not in next(iter(prompts.values()))


@pytest.mark.parametrize("language", ["en", "vi"])
def test_anti_inference_map_rule_reaches_only_registered_gemini_fact_inventory_maps(language):
    snapshot = _short_snapshot()
    rule = nd._GEMINI_FACT_INVENTORY_RULE[language]
    gemini = nd.notes_budget(*GEMINI)
    others = (
        nd.notes_budget("chatgpt", "gpt-5.6-sol"),
        nd.notes_budget("antigravity", "gemini-test"),
        None,
    )

    def chunk(budget, fact_inventory):
        return nd.build_chunk_prompt(
            snapshot,
            chunk_index=1,
            chunk_count=2,
            output_language=language,
            budget=budget,
            fact_inventory=fact_inventory,
        )

    long_map = chunk(gemini, True)
    assert long_map.count(rule) == 1
    # It follows the shared fact-inventory rule, which every provider's long map keeps.
    assert nd._FACT_INVENTORY_RULE[language] + rule in long_map
    assert rule not in chunk(gemini, False)
    assert rule not in nd.build_prompt(snapshot, output_language=language, budget=gemini)
    for other in others:
        assert rule not in chunk(other, True)
        assert rule not in nd.build_prompt(snapshot, output_language=language, budget=other)
