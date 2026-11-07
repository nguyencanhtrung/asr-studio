from __future__ import annotations

import json

import pytest

from asr_studio.notes_audit import AuditCatalog, build_audit_prompt
from asr_studio.notes_document import (
    SECTIONS,
    NotesDocumentError,
    notes_budget,
    provider_source,
)


def _empty_document():
    return {"schema_version": 1, **{section: [] for section, _ in SECTIONS}}


def _source():
    return {
        "meeting_date": "2026-11-07",
        "segments": [
            {
                "id": "seg-0",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "text": "The team did not approve the proposed rollout.",
            },
            {
                "id": "seg-1",
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "text": "Mai will send the report tomorrow.",
            },
        ],
    }


def _draft():
    document = _empty_document()
    document["overview"] = [{"text": "Meeting purpose", "source_ids": []}]
    document["key_topics"] = [
        {
            "text": "The team did not approve the proposed rollout.",
            "source_ids": ["seg-0"],
        }
    ]
    document["decisions"] = [{"text": "Ship the update next Tuesday.", "source_ids": ["seg-0"]}]
    document["action_items"] = [
        {
            "text": "Mai will send the report tomorrow.",
            "source_ids": ["seg-1"],
            "owner": "Mai",
            "deadline_text": "tomorrow",
            "deadline_date": "2026-11-08",
        }
    ]
    return document


def _ref(catalog: AuditCatalog, section: str) -> str:
    return next(entry.ref for entry in catalog.draft_entries if entry.section == section)


def test_catalog_uses_offsets_and_preserves_the_actual_draft_payload():
    source = _source()
    draft = _draft()
    catalog = AuditCatalog.from_documents(source, draft)
    payload = catalog.prompt_payload()

    assert payload["draft_candidates"]
    assert all("text" not in entry for entry in payload["draft_candidates"])
    key_topic = next(
        entry for entry in payload["draft_candidates"] if entry["section"] == "key_topics"
    )
    assert key_topic["prefix_end_offsets"]
    assert catalog.resolve(key_topic["ref"]) == draft["key_topics"][0]["text"]

    prompt = build_audit_prompt(
        source,
        draft,
        budget=notes_budget("chatgpt", "gpt-5.6-sol"),
    )
    captured = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
    assert captured["summaries"][0]["notes"] == draft
    assert captured["source"] == provider_source(source)
    assert captured["audit_catalog"] == payload


def test_prefixes_cannot_drop_polarity_or_uncertainty_markers():
    source = _source()
    draft = _draft()
    draft["decisions"] = [{"text": "Do not ship the release immediately.", "source_ids": ["seg-0"]}]
    catalog = AuditCatalog.from_documents(source, draft)
    entry = next(
        item
        for item in catalog.prompt_payload()["draft_candidates"]
        if item["section"] == "decisions"
    )
    not_end = draft["decisions"][0]["text"].index("not") + len("not")

    assert all(end >= not_end for end in entry["prefix_end_offsets"])
    with pytest.raises(NotesDocumentError, match="unknown reference|allowed reference"):
        catalog.resolve(f"{entry['ref']}p2")


@pytest.mark.parametrize("marker", ["dự kiến", "có thể", "đề xuất"])
def test_vietnamese_compound_markers_protect_both_words_and_keep_prior_prefixes(marker):
    source = _source()
    draft = _draft()
    text = f"Ra mắt ngày 5/12, {marker} vào tuần sau"
    draft["decisions"] = [{"text": text, "source_ids": ["seg-0"]}]
    catalog = AuditCatalog.from_documents(source, draft)
    entry = next(
        item
        for item in catalog.prompt_payload()["draft_candidates"]
        if item["section"] == "decisions"
    )
    first_word_end = text.index(marker) + len(marker.split()[0])
    second_word_end = text.index(marker) + len(marker)
    assert first_word_end not in entry["prefix_end_offsets"]
    assert second_word_end in entry["prefix_end_offsets"]

    selected = _empty_document()
    selected["decisions"] = [{"text": f"{entry['ref']}p{first_word_end}", "source_ids": ["seg-0"]}]
    with pytest.raises(NotesDocumentError, match="unknown reference|allowed reference"):
        catalog.apply(selected)


def test_marker_guard_preserves_english_and_valid_prefixes():
    source = _source()
    draft = _draft()
    draft["decisions"] = [{"text": "The team may ship the release.", "source_ids": ["seg-0"]}]
    catalog = AuditCatalog.from_documents(source, draft)
    entry = next(
        item
        for item in catalog.prompt_payload()["draft_candidates"]
        if item["section"] == "decisions"
    )
    text = draft["decisions"][0]["text"]
    may_end = text.index("may") + len("may")
    before_may_end = text.index("may") - 1
    assert may_end in entry["prefix_end_offsets"]
    assert before_may_end not in entry["prefix_end_offsets"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda selected, catalog: selected["overview"].append(
                {"text": "new prose", "source_ids": []}
            ),
            "unknown reference",
        ),
        (
            lambda selected, catalog: selected["decisions"].append(
                {"text": _ref(catalog, "key_topics"), "source_ids": ["seg-0"]}
            ),
            "promote",
        ),
        (
            lambda selected, catalog: selected["key_topics"].extend(
                [
                    {"text": _ref(catalog, "key_topics"), "source_ids": ["seg-0"]},
                    {"text": _ref(catalog, "key_topics"), "source_ids": ["seg-0"]},
                ]
            ),
            "more than once",
        ),
        (
            lambda selected, catalog: selected["action_items"].append(
                {
                    "text": _ref(catalog, "action_items"),
                    "source_ids": ["seg-1"],
                    "owner": "Invented",
                    "deadline_text": "tomorrow",
                    "deadline_date": "2026-11-08",
                }
            ),
            "invent action metadata",
        ),
    ],
)
def test_catalog_guards_reject_bypass_attempts(mutate, message):
    source = _source()
    catalog = AuditCatalog.from_documents(source, _draft())
    selected = _empty_document()
    mutate(selected, catalog)

    with pytest.raises(NotesDocumentError, match=message):
        catalog.apply(selected)


def test_valid_prefix_quote_and_action_metadata_resolve_to_public_notes():
    source = _source()
    draft = _draft()
    catalog = AuditCatalog.from_documents(source, draft)
    key_topic_entry = next(
        entry
        for entry in catalog.prompt_payload()["draft_candidates"]
        if entry["section"] == "key_topics"
    )
    prefix = key_topic_entry["prefix_end_offsets"][-1]
    selected = _empty_document()
    selected["key_topics"] = [
        {"text": f"{key_topic_entry['ref']}p{prefix}", "source_ids": ["seg-0"]}
    ]
    selected["action_items"] = [
        {
            "text": _ref(catalog, "action_items"),
            "source_ids": ["seg-1"],
            "owner": "Mai",
            "deadline_text": "tomorrow",
            "deadline_date": "2026-11-08",
        }
    ]
    selected["confirmation_items"] = [{"text": "s1", "source_ids": []}]

    result = catalog.apply(selected)
    assert result["key_topics"][0]["text"] == draft["key_topics"][0]["text"][:prefix]
    assert result["action_items"][0]["owner"] == "Mai"
    assert result["action_items"][0]["deadline_text"] == "tomorrow"
    assert result["confirmation_items"][0]["text"] == (
        "Source excerpt (original language; output language: English): "
        "“Mai will send the report tomorrow.”"
    )
    assert result["confirmation_items"][0]["source_ids"] == ["seg-1"]


def test_source_quote_identity_uses_position_not_a_grouped_alias():
    source = {
        "meeting_date": None,
        "segments": [
            {"id": "same", "start_seconds": 0.0, "end_seconds": 1.0, "text": "first view"},
            {"id": "same", "start_seconds": 1.0, "end_seconds": 2.0, "text": "second view"},
        ],
    }
    draft = _empty_document()
    catalog = AuditCatalog.from_documents(source, draft)
    selected = _empty_document()
    selected["key_topics"] = [{"text": "s0", "source_ids": []}]
    selected["confirmation_items"] = [{"text": "s1", "source_ids": []}]

    result = catalog.apply(selected)
    assert [item["text"] for item in result["key_topics"] + result["confirmation_items"]] == [
        "Source excerpt (original language; output language: English): “first view”",
        "Source excerpt (original language; output language: English): “second view”",
    ]


def test_source_quote_keeps_original_language_and_labels_vietnamese_output():
    source = _source()
    catalog = AuditCatalog.from_documents(source, _draft())
    selected = _empty_document()
    selected["confirmation_items"] = [{"text": "s1", "source_ids": []}]

    result = catalog.apply(selected, output_language="vi")

    assert result["confirmation_items"][0]["text"] == (
        "Trích nguyên văn (ngôn ngữ gốc; ngôn ngữ đầu ra: tiếng Việt): "
        "“Mai will send the report tomorrow.”"
    )
