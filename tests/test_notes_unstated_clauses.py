"""Advisory ``attention.unstated_clauses`` cues from an item's own cited segments (opt-in)."""

from __future__ import annotations

import json

import pytest
from notes_helpers import reconcile_disposition, reconcile_keep_patch, reconcile_payload

from asr_studio import notes_document as nd
from asr_studio import notes_reconcile as nr
from asr_studio.notes_pipeline import generate_notes_document
from asr_studio.notes_reconcile import ReconcileLedger

_TEXTS = {
    "date": "Three departments are preparing the impact report. They will report back in October.",
    "trigger": "Once the study is finished, staff will draft a tenant policy with residents.",
    "cause": "The extension is needed because the review has not been completed yet.",
    "attribution": "I am speaking on behalf of the Riverside Tenants Union to ask for a delay.",
    "stance": "Neighbors and local businesses support the rezoning of the corner lot.",
    "approval": "The final design must come back to the board for approval next spring.",
    "filler": "Yes, thank you so much. Okay.",
    "vi": "Sau khi có báo cáo vào tháng 9, nhóm sẽ soạn chính sách mới cho cư dân.",
}
_IDS = {name: f"s{index + 1}" for index, name in enumerate(_TEXTS)}
SOURCE = {
    "meeting_date": "2026-01-05",
    "segments": [
        {
            "id": _IDS[name],
            "start_seconds": 5.0 * index,
            "end_seconds": 5.0 * index + 5.0,
            "text": text,
            "speaker_name": f"SPEAKER_{index:02d}",
        }
        for index, (name, text) in enumerate(_TEXTS.items())
    ],
}
_ITEM_TEXTS = {
    "date": "Impact report being prepared by three departments",
    "trigger": "Housing program reviewed",
    "cause": "Extension requested",
    "attribution": "Delay requested",
    "stance": "Rezoning approved",
    "approval": "Design concept presented",
    "filler": "Meeting closed",
    "vi": "Nhóm dự án họp hằng tuần",
}


def _doc(**sections):
    return {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}, **sections}


def _item(text, *ids):
    return {"text": text, "source_ids": list(ids)}


def _maps(*names):
    return [_doc(key_topics=[_item(_ITEM_TEXTS[name], _IDS[name]) for name in names])]


def _ledger(maps, source=SOURCE, **flags):
    return ReconcileLedger.build(source, _doc(), maps, **flags)


def _cues(name):
    return _ledger(_maps(name), include_clause_cues=True).clause_cues().get("m0", [])


def _payload_bytes(ledger):
    return len(
        json.dumps(ledger.prompt_payload(), ensure_ascii=False, separators=(",", ":")).encode()
    )


# ----- gating ---------------------------------------------------------------------------------


def test_flag_off_payload_schema_and_prompt_are_byte_identical_to_the_default():
    maps = _maps(*_TEXTS)
    default, off = _ledger(maps), _ledger(maps, include_clause_cues=False)
    assert off.clause_cues() == {}
    assert off.prompt_payload() == default.prompt_payload()
    assert off.schema() == default.schema()
    prompt = off.build_prompt()
    assert prompt == default.build_prompt()
    assert "unstated_clauses" not in prompt
    assert nr._CLAUSE_CUE_RULE["en"] not in prompt


def test_flag_on_adds_the_conditional_rule_once_and_leaves_the_schema_unchanged():
    on = _ledger(_maps("cause"), include_clause_cues=True)
    prompt = on.build_prompt(output_language="vi")
    assert prompt.count(nr._CLAUSE_CUE_RULE["vi"]) == 1
    assert nr._CLAUSE_CUE_RULE["en"] not in prompt
    assert on.schema() == _ledger(_maps("cause")).schema()
    assert "unstated_clauses" in reconcile_payload(prompt)["map_items"][0]["attention"]


def test_repair_prompt_carries_no_clause_cues_or_rule():
    ledger = _ledger(_maps("cause"), include_clause_cues=True)
    bad = reconcile_disposition("m0", "update", text="Extension requested", source_ids=["s3"])
    bad["evidence"] = [{"source_id": "s3", "quote": "an invented quote"}]
    repair = ledger.plan_repair(
        json.dumps({"ledger_version": 2, "dispositions": [bad], "additions": []})
    )
    assert repair is not None
    prompt = ledger.build_repair_prompt(repair)
    assert "unstated_clauses" not in prompt
    assert nr._CLAUSE_CUE_RULE["en"] not in prompt


# ----- cue selection --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason", "excerpt"),
    [
        ("date", "date", "They will report back in October."),
        ("trigger", "sequence", _TEXTS["trigger"]),
        ("cause", "cause", _TEXTS["cause"]),
        ("attribution", "attribution", _TEXTS["attribution"]),
        ("stance", "stance", _TEXTS["stance"]),
        ("approval", "sequence", _TEXTS["approval"]),
        ("vi", "sequence", _TEXTS["vi"]),
    ],
)
def test_an_unstated_clause_of_the_items_own_cited_segment_is_cued(name, reason, excerpt):
    cues = _cues(name)
    assert cues and cues[0]["source_id"] == _IDS[name]
    assert cues[0]["excerpt"] == excerpt
    assert reason in cues[0]["reasons"]
    assert set(cues[0]) == {"source_id", "excerpt", "reasons"}  # no speaker, identity or fact


def test_the_excerpt_keeps_negation_and_certainty_words_exactly():
    (cue,) = _cues("cause")
    assert "has not been completed yet" in cue["excerpt"]
    assert cue["excerpt"] in _TEXTS["cause"]


@pytest.mark.parametrize(
    "item_text",
    [
        _TEXTS["cause"],  # already covered
        "Extension needed because review not completed",  # relation and facts already worded
    ],
)
def test_no_cue_for_an_already_expressed_clause(item_text):
    ledger = _ledger([_doc(key_topics=[_item(item_text, "s3")])], include_clause_cues=True)
    assert ledger.clause_cues() == {}


def test_no_cue_when_the_item_already_words_the_relation_class():
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": "a",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": "Because, honestly, everyone is tired.",
                "speaker_name": None,
            }
        ],
    }
    maps = [_doc(key_topics=[_item("Because of staffing gaps, delay requested", "a")])]
    assert _ledger(maps, source=source, include_clause_cues=True).clause_cues() == {}


def test_no_cue_for_filler_or_an_uncited_segment():
    assert _cues("filler") == []
    # The cause sentence exists in the source but this item does not cite it.
    maps = [_doc(key_topics=[_item("Extension requested", _IDS["filler"])])]
    assert _ledger(maps, include_clause_cues=True).clause_cues() == {}


def test_speaker_labels_are_never_cue_material():
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": "a",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": "Thanks everyone for coming today.",
                "speaker_name": "SPEAKER_07 Jordan Vale",
            }
        ],
    }
    maps = [_doc(key_topics=[_item("Welcome", "a")])]
    assert _ledger(maps, source=source, include_clause_cues=True).clause_cues() == {}


def test_relation_cues_rank_before_material_only_cues_and_order_is_stable():
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": "a",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": "Item 42 was read into the record. The vote waits until the audit ends.",
                "speaker_name": None,
            }
        ],
    }
    ledger = _ledger(
        [_doc(key_topics=[_item("Budget hearing held", "a")])],
        source=source,
        include_clause_cues=True,
    )
    cues = ledger.clause_cues()["m0"]
    assert [cue["reasons"][0] for cue in cues] == ["sequence", "number"]
    assert all(ledger.clause_cues() == {"m0": cues} for _ in range(3))


@pytest.mark.parametrize(
    "text",
    [
        "Once again, thanks to everyone here tonight.",
        "We will put them all at once tonight.",
        "October. Great.",  # a one-word clause is never a cue
        "We may revisit it tonight with everyone.",  # modal 'may' is not a month
    ],
)
def test_idioms_one_word_clauses_and_modal_may_are_not_cued(text):
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": "a",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": text,
                "speaker_name": None,
            }
        ],
    }
    maps = [_doc(key_topics=[_item("Plan", "a")])]
    assert _ledger(maps, source=source, include_clause_cues=True).clause_cues() == {}


def _one(text, item_text):
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": "a",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": text,
                "speaker_name": None,
            }
        ],
    }
    maps = [_doc(key_topics=[_item(item_text, "a")])]
    return _ledger(maps, source=source, include_clause_cues=True).clause_cues().get("m0", [])


@pytest.mark.parametrize(
    "text",
    [
        "Tonight's vote on the site plan is only the first step before the full plan.",
        "The site plan approval is the second stage of a longer review.",
        "Phase two of the site plan approval starts after funding.",
        "The site plan approval is part of a three step process.",
        "Việc phê duyệt kế hoạch là bước đầu tiên của quy trình.",
        "Phê duyệt kế hoạch thuộc giai đoạn thứ hai.",
    ],
)
def test_an_unworded_explicit_phase_is_cued_even_with_high_noun_overlap(text):
    item = (
        "Kế hoạch phê duyệt quy trình"
        if "ạ" in text or "ệ" in text
        else ("Site plan approval vote held before full plan review")
    )
    cues = _one(text, item)
    assert cues and cues[0]["reasons"][0] == "phase" and cues[0]["excerpt"] == text


@pytest.mark.parametrize(
    "item_text",
    [
        "Site plan approval vote held as first step",
        "Site plan approval is one phase of the review",
    ],
)
def test_no_phase_cue_when_the_item_already_names_a_step_or_phase(item_text):
    text = "Tonight's site plan approval vote is the second step of the review."
    assert all("phase" not in cue["reasons"] for cue in _one(text, item_text))


@pytest.mark.parametrize(
    "text",
    [
        "We took a step back to look at the site plan approval vote.",
        "The site plan approval vote held at the main stage hall.",
    ],
)
def test_non_ordinal_step_or_stage_words_are_not_phase_cues(text):
    assert _one(text, "Site plan approval vote held at hall") == []


@pytest.mark.parametrize(
    ("text", "item_text"),
    [
        ("Staff will send the final numbers by the end of the month.", "Final figures pending"),
        ("The working group reports back on Thursday.", "Working committee formed"),
        ("Nhóm sẽ gửi bản cuối vào cuối tháng.", "Đội soạn bản thảo"),
    ],
)
def test_deadline_phrases_and_weekdays_are_date_cues(text, item_text):
    (cue,) = _one(text, item_text)
    assert cue["reasons"][0] == "date"


@pytest.mark.parametrize(
    "text",
    [
        "The pilot covers 3 regions and starts with the northern office.",
        "The pilot covers regions and starts with the northern office because of demand.",
    ],
)
def test_a_high_overlap_clause_is_not_cued_for_a_number_or_ordinary_relation(text):
    assert _one(text, "Pilot covers regions, starts with northern office") == []


def test_dates_rank_before_ordinary_cause_and_stance_cues(monkeypatch):
    monkeypatch.setattr(nr, "MAX_CLAUSE_CUES_PER_VIEW", 1)
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": sid,
                "start_seconds": float(n),
                "end_seconds": float(n) + 1,
                "text": text,
                "speaker_name": None,
            }
            for n, (sid, text) in enumerate(
                [
                    ("a", "Residents support it because rents keep rising quickly."),
                    ("b", "The final report arrives in September."),
                ]
            )
        ],
    }
    maps = [_doc(key_topics=[_item("Rent topic", "a"), _item("Study underway", "b")])]
    cues = _ledger(maps, source=source, include_clause_cues=True).clause_cues()
    assert list(cues) == ["m1"] and cues["m1"][0]["reasons"] == ["date"]


# ----- caps -----------------------------------------------------------------------------------


def _many(count, text):
    source = {
        "meeting_date": None,
        "segments": [
            {
                "id": f"x{index}",
                "start_seconds": float(index),
                "end_seconds": float(index) + 1,
                "text": text,
                "speaker_name": None,
            }
            for index in range(count)
        ],
    }
    maps = [_doc(key_topics=[_item(f"Topic {index}", f"x{index}") for index in range(count)])]
    return source, maps


def test_at_most_two_cues_per_item():
    text = " ".join(
        f"Work stops until phase {n} review because of grant rule {n}." for n in range(1, 7)
    )
    source, maps = _many(1, text)
    maps[0]["key_topics"][0]["text"] = "Grant overview"
    cues = _ledger(maps, source=source, include_clause_cues=True).clause_cues()
    assert len(cues["m0"]) == nr.MAX_CLAUSE_CUES_PER_ITEM


def test_view_count_cap_breadth_first_and_payload_byte_bound():
    source, maps = _many(70, "Work pauses until the inspection because of safety rules.")
    on = _ledger(maps, source=source, include_clause_cues=True)
    cues = on.clause_cues()
    assert sum(map(len, cues.values())) == nr.MAX_CLAUSE_CUES_PER_VIEW
    assert len(cues) == nr.MAX_CLAUSE_CUES_PER_VIEW  # one per item before any second cue
    assert list(cues) == [f"m{index}" for index in range(nr.MAX_CLAUSE_CUES_PER_VIEW)]
    delta = _payload_bytes(on) - _payload_bytes(_ledger(maps, source=source))
    assert 0 < delta <= nr.MAX_CLAUSE_CUE_BYTES


def test_every_items_best_cue_ranks_before_any_second_cue():
    # 30 items with two date cues each and 20 with one cause cue: 50 best cues compete for 48.
    rich, rich_maps = _many(30, "Work pauses until March. Costs rise in October.")
    plain, plain_maps = _many(20, "Costs rose because of new rules.")
    for index, segment in enumerate(plain["segments"]):
        segment["id"] = f"y{index}"
        plain_maps[0]["key_topics"][index]["source_ids"] = [f"y{index}"]
        plain_maps[0]["key_topics"][index]["text"] = f"Item {index}"
    source = {"meeting_date": None, "segments": rich["segments"] + plain["segments"]}
    maps = [_doc(key_topics=rich_maps[0]["key_topics"] + plain_maps[0]["key_topics"])]
    cues = _ledger(maps, source=source, include_clause_cues=True).clause_cues()
    assert sum(map(len, cues.values())) == nr.MAX_CLAUSE_CUES_PER_VIEW
    assert len(cues) == nr.MAX_CLAUSE_CUES_PER_VIEW and all(len(c) == 1 for c in cues.values())
    assert sum(1 for ref in cues if int(ref[1:]) >= 30) == 18


def test_unicode_byte_cap_holds_on_the_actual_payload(monkeypatch):
    monkeypatch.setattr(nr, "MAX_CLAUSE_CUE_BYTES", 2_000)
    text = "Sau khi kiểm tra xong, đội ngũ sẽ trình bày kế hoạch điều chỉnh cho cư dân khu phố."
    source, maps = _many(40, text)
    on = _ledger(maps, source=source, include_clause_cues=True)
    cues = on.clause_cues()
    assert 0 < sum(map(len, cues.values())) < nr.MAX_CLAUSE_CUES_PER_VIEW
    delta = _payload_bytes(on) - _payload_bytes(_ledger(maps, source=source))
    assert 0 < delta <= 2_000


def test_long_clause_is_cut_to_exact_bounded_windows_marked_truncated():
    run_on = "and the residents who live near the park " * 8 + "will wait until the survey ends"
    source, maps = _many(1, "The board met. " + run_on.strip() + ".")
    cues = _ledger(maps, source=source, include_clause_cues=True).clause_cues()["m0"]
    assert cues and all(len(cue["excerpt"]) <= nr.MAX_CLAUSE_CUE_CHARS for cue in cues)
    assert all(cue["excerpt"] in source["segments"][0]["text"] for cue in cues)
    assert cues[0]["truncated"] is True and "until" in cues[0]["excerpt"]


def test_source_ids_and_item_text_are_never_altered_by_cues():
    maps = _maps(*_TEXTS)
    on, off = _ledger(maps, include_clause_cues=True), _ledger(maps)
    for with_cues, without in zip(
        on.prompt_payload()["map_items"], off.prompt_payload()["map_items"], strict=True
    ):
        with_cues.pop("attention", None)
        without.pop("attention", None)
        assert with_cues == without


# ----- partition views ------------------------------------------------------------------------


def test_each_view_cues_only_its_owned_entries_and_read_only_items_carry_none():
    ledger = _ledger(_maps(*_TEXTS), include_clause_cues=True)
    views = ledger.partitions()
    assert len(views) == 2 and all(view.include_clause_cues for view in views)
    for view in views:
        owned = {entry.ref for entry in view.entries}
        assert set(view.clause_cues()) <= owned
        payload = view.prompt_payload()
        assert all("attention" not in item for item in payload["read_only_items"])
    assert views[1].schema()["properties"]["additions"]["maxItems"] == 0


def test_view_budgets_are_per_owned_view():
    source, maps = _many(120, "Work pauses until the inspection because of safety rules.")
    views = _ledger(maps, source=source, include_clause_cues=True).partitions()
    counts = [sum(map(len, view.clause_cues().values())) for view in views]
    assert counts == [nr.MAX_CLAUSE_CUES_PER_VIEW, nr.MAX_CLAUSE_CUES_PER_VIEW]


# ----- response validation is unchanged -------------------------------------------------------


def _patch(*dispositions):
    return json.dumps({"ledger_version": 2, "dispositions": list(dispositions), "additions": []})


def test_an_unlisted_cued_ref_stays_valid_and_unchanged():
    ledger = _ledger(_maps("cause"), include_clause_cues=True)
    assert ledger.clause_cues()
    result = ledger.parse(_patch())
    assert [item["text"] for item in result.document["key_topics"]] == ["Extension requested"]


def test_a_cue_guided_exact_quote_update_applies():
    ledger = _ledger(_maps("cause"), include_clause_cues=True)
    (cue,) = ledger.clause_cues()["m0"]
    update = reconcile_disposition(
        "m0",
        "update",
        text="Extension requested because review not completed yet",
        source_ids=[cue["source_id"]],
        evidence=[
            {"source_id": cue["source_id"], "quote": "because the review has not been completed"}
        ],
    )
    result = ledger.parse(_patch(update))
    assert result.document["key_topics"][0]["text"].endswith("review not completed yet")


@pytest.mark.parametrize(
    ("source_id", "quote"),
    [
        ("s3", "because the audit was cancelled"),  # invented quote
        ("s4", "on behalf of the Riverside Tenants Union"),  # segment the item does not cite
    ],
)
def test_existing_validators_still_reject_invented_or_uncited_evidence(source_id, quote):
    ledger = _ledger(_maps("cause"), include_clause_cues=True)
    update = reconcile_disposition(
        "m0",
        "update",
        text="Extension requested because review not completed yet",
        source_ids=["s3"],
        evidence=[{"source_id": source_id, "quote": quote}],
    )
    with pytest.raises(nd.NotesDocumentError):
        ledger.parse(_patch(update))


def test_an_update_that_drops_a_negation_is_still_rejected():
    maps = [_doc(key_topics=[_item("Review not completed", "s3")])]
    ledger = _ledger(maps, include_clause_cues=True)
    update = reconcile_disposition(
        "m0",
        "update",
        text="Review completed; extension needed",
        source_ids=["s3"],
        evidence=[{"source_id": "s3", "quote": "The extension is needed"}],
    )
    with pytest.raises(nd.NotesDocumentError, match="marker"):
        ledger.parse(_patch(update))


# ----- pipeline gating ------------------------------------------------------------------------


def _snapshot():
    texts = [
        "Launch plan is approved. The rollout waits until the safety audit ends in March.",
        "Vendor contract renewal is still open because pricing changed.",
    ]
    return {
        "meeting_date": "2026-01-05",
        "speakers": [],
        "segments": [
            {
                "id": f"{index + 1:032x}",
                "start_seconds": [0.0, 3_601.0][index],
                "end_seconds": [1.0, 3_602.0][index],
                "text": text,
                "speaker_name": "Morgan",
            }
            for index, text in enumerate(texts)
        ],
    }


def _map_reply(prompt):
    source = json.JSONDecoder().raw_decode(prompt.split("SOURCE_JSON:\n", 1)[1])[0]
    owned = [s for s in source["segments"] if not s.get("overlap")]
    document = _doc()
    first = owned[0]["text"].split(". ")[0].split(" is ")[0]
    document["key_topics"] = [{"text": first, "source_ids": [owned[0]["id"]]}]
    return json.dumps(document)


def _reconcile_prompts(provider, model):
    calls = []

    def transport(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "chunk":
            return _map_reply(prompt)
        if kwargs["stage"] == "semantic_audit":
            from notes_helpers import semantic_keep_patch

            return semantic_keep_patch(prompt)
        return reconcile_keep_patch(prompt)

    generate_notes_document(
        snapshot=_snapshot(),
        output_language="en",
        provider=provider,
        model=model,
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=lambda _coverage: None,
    )
    return [call["prompt"] for call in calls if call["stage"] == "reconcile"]


def test_registered_gemini_fact_inventory_views_carry_cues_and_the_rule():
    prompts = _reconcile_prompts("antigravity", "gemini-3.8-flash")
    assert len(prompts) == 2
    assert all(prompt.count(nr._CLAUSE_CUE_RULE["en"]) == 1 for prompt in prompts)
    cued = [
        item["attention"]["unstated_clauses"]
        for prompt in prompts
        for item in reconcile_payload(prompt)["map_items"]
        if "unstated_clauses" in item.get("attention", {})
    ]
    assert cued


def test_chatgpt_long_reconcile_has_no_clause_cues_or_rule():
    (prompt,) = _reconcile_prompts("chatgpt", "gpt-5.6-sol")
    assert "unstated_clauses" not in prompt
    assert nr._CLAUSE_CUE_RULE["en"] not in prompt


def test_a_high_overlap_phase_cue_names_only_the_phase_not_incidental_material():
    (cue,) = _one(
        "The site plan approval is the second stage of 3 reviews.",
        "Site plan approval reviews held",
    )
    assert cue["reasons"] == ["phase"]
